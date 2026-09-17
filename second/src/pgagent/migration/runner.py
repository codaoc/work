"""迁移 runner（设计文档 §4.5）。

apply_migration 工具的执行主体：门控 → 落盘 → 事务执行（DDL+台账）→
状态基线更新 → 设计日志追加。幂等：version 冲突即视为已应用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from ..config import Config
from ..db.executor import Executor
from ..db.introspect import schema_fingerprint
from ..db.session import Database
from ..errors import GateRejected, RunnerError, SqlValidationError
from ..logging import get_logger
from ..safety.confirm import ConfirmGate, gate_decision
from ..safety.parser import parse_statements
from ..safety.risk import RiskAssessment
from . import design_log
from .ledger import ensure_ledger, record_migration

log = get_logger("migration.runner")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KNOWN_TABLES_HINT = "涉及表"  # 设计日志中用于粗提表名的标记（此处仅记录 SQL 全文，表名由 introspect 提供）


@dataclass(frozen=True)
class MigrationOutcome:
    version: str
    path: str
    duration_ms: int
    gate: str            # skip | approved
    down_valid: bool | None


class MigrationRunner:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        executor: Executor,
        gate: ConfirmGate,
        session_id: str = "unknown",
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.executor = executor
        self.gate = gate
        self.session_id = session_id
        self._last_tables: list[str] = []  # 最近一次 apply 涉及的表（由 tools 层写入）

    def ensure_ready(self) -> None:
        """创建台账表与目录结构（幂等）。"""
        self.cfg.migrations_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.executor.execute_admin("SELECT 1")  # 连通性检查
        with self.db.transaction() as conn:
            ensure_ledger(conn)

    def apply(
        self,
        *,
        name: str,
        sql: str,
        request_summary: str,
        tables: list[str],
        risk: RiskAssessment,
        down_sql: str | None = None,
    ) -> MigrationOutcome:
        if not _NAME_RE.match(name):
            raise RunnerError(f"迁移名「{name}」非法：需 kebab-case（小写字母/数字/连字符）")

        # L0/L1 兜底：工具层已校验，这里防御性再跑一次
        statements = parse_statements(sql, self.cfg.safety.statement_whitelist)

        # 门控前置：拒绝时不留任何文件痕迹
        decision = gate_decision(risk, self.cfg.safety)
        if decision == "denied":
            raise GateRejected(f"迁移「{name}」为{risk.label}，且配置 allow_destructive=deny，已拒绝")
        if decision == "required":
            confirmation = self.gate.confirm(risk, sql, name, request_summary)
            if not confirmation.approved:
                raise GateRejected(
                    f"用户拒绝迁移「{name}」" + (f"：{confirmation.note}" if confirmation.note else "")
                )
            if confirmation.sql != sql:
                # 用户手工编辑：以编辑后的内容应用
                sql = confirmation.sql
                statements = parse_statements(sql, self.cfg.safety.statement_whitelist)

        version = self._next_version()
        self.cfg.migrations_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.migrations_dir / f"{version}_{name}.sql"
        path.write_text(_migration_file(name, request_summary, sql), encoding="utf-8")
        checksum = _sha256(path.read_bytes())

        def ledger_insert(conn: psycopg.Connection) -> None:
            record_migration(
                conn, version=version, name=name, checksum=checksum, sql=sql,
                request=request_summary, session_id=self.session_id,
                down_sql=None, down_valid=None, duration_ms=0,
            )

        result = self.executor.apply(statements, ledger_insert)
        if not result.ok:
            raise RunnerError(f"迁移执行失败：{result.error}")

        # 回填真实耗时（version 主键更新，量小无锁风险）
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE agent_schema_migrations SET duration_ms = %s WHERE version = %s",
                (result.duration_ms, version),
            )

        # down migration（FR-9 / §4.10）：主 DDL 应用成功后再试执行验证
        # （此时变更已生效，down 才有可验证的对象）
        down_valid: bool | None = None
        if down_sql:
            down_stmts = parse_statements(down_sql, self.cfg.safety.statement_whitelist)
            preview = self.executor.preview(down_stmts)
            down_valid = preview.ok
            if not preview.ok:
                log.warning("down migration 试执行失败：%s", preview.error)
            (self.cfg.migrations_dir / f"{version}_{name}.down.sql").write_text(
                _migration_file(name, request_summary, down_sql), encoding="utf-8"
            )
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE agent_schema_migrations SET down_sql = %s, down_valid = %s "
                    "WHERE version = %s",
                    (down_sql, down_valid, version),
                )

        self._update_state(result.duration_ms)
        design_log.append_entry(
            self.cfg.design_log_path, version=version, name=name, request=request_summary,
            risk_label=risk.label, tables_touched=tables, sql=sql, duration_ms=result.duration_ms,
        )
        log.info("迁移 %s_%s 已应用（%s ms）", version, name, result.duration_ms)
        return MigrationOutcome(version=version, path=str(path), duration_ms=result.duration_ms,
                                gate="approved" if decision == "required" else "skip",
                                down_valid=down_valid)

    def history(self) -> list[dict[str, object]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT version, name, request, duration_ms, applied_at "
                "FROM agent_schema_migrations ORDER BY version"
            ).fetchall()
        return [
            {"version": r[0], "name": r[1], "request": r[2],
             "duration_ms": r[3], "applied_at": r[4]}
            for r in rows
        ]

    def status(self) -> dict[str, object]:
        """对账：文件 vs 台账 + 漂移检测（FR-10）。"""
        self.ensure_ready()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT version, checksum FROM agent_schema_migrations ORDER BY version"
            ).fetchall()
        ledger = {str(r[0]): str(r[1]) for r in rows}

        files: dict[str, str] = {}
        for f in sorted(self.cfg.migrations_dir.glob("*.sql")):
            if f.name.endswith(".down.sql"):
                continue
            version = f.name.split("_", 1)[0]
            files[version] = _sha256(f.read_bytes())

        issues: list[str] = []
        for version in sorted(set(files) | set(ledger)):
            in_files, in_ledger = version in files, version in ledger
            if in_files and not in_ledger:
                issues.append(f"{version}: 存在迁移文件但台账无记录（未应用或中途失败）")
            elif in_ledger and not in_files:
                issues.append(f"{version}: 台账有记录但迁移文件缺失")
            elif files[version] != ledger[version]:
                issues.append(f"{version}: 迁移文件与台账 checksum 不一致（文件被手改？）")

        drift = self._check_drift()
        if drift:
            issues.append(f"漂移：{drift}")

        return {"issues": issues, "applied": len(ledger), "files": len(files)}

    # ------------------------------------------------------------------

    def _next_version(self) -> str:
        base = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        version, suffix = base, 0
        existing = {p.name.split("_", 1)[0] for p in self.cfg.migrations_dir.glob("*.sql")}
        while version in existing:
            suffix += 1
            version = f"{base}{suffix:02d}"
        return version

    def _check_drift(self) -> str | None:
        state = self._load_state()
        baseline = state.get("schema_fingerprint")
        if not baseline:
            return None  # 尚无基线（首次使用），不告警
        current = schema_fingerprint(self.db.conn)
        if current != baseline:
            return ("数据库结构与上次 agent 应用后的基线不一致，"
                    "可能有人绕过 pg-agent 手工改库。请先 `inspect_schema` 确认现状。")
        return None

    def _update_state(self, duration_ms: int) -> None:
        state = self._load_state()
        state["schema_fingerprint"] = schema_fingerprint(self.db.conn)
        state["last_applied_at"] = datetime.now(timezone.utc).isoformat()
        state["last_duration_ms"] = duration_ms
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_state(self) -> dict[str, object]:
        path = self.cfg.state_dir / "state.json"
        if path.exists():
            try:
                return dict(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                log.warning("state.json 损坏，已重建")
        return {}


def _migration_file(name: str, request: str, sql: str) -> str:
    header = (
        f"-- migration: {name}\n"
        f"-- request: {request}\n"
        f"-- 由 pg-agent 生成；事务由 runner 包裹，本文件保持纯 SQL 可被标准工具重放\n\n"
    )
    return header + (sql if sql.endswith(";") else sql + ";") + "\n"


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()
