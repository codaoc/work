"""SQL 执行器（设计文档 §4.4 L3）。

- preview：事务内执行后 ROLLBACK，由真实数据库裁决语法/依赖/约束。
- apply：DDL 与台账写入同事务提交（原子性，原则 P3）。
- CREATE INDEX CONCURRENTLY 等不能进事务块的语句走自动提交模式（§3.4 例外）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import psycopg

from ..logging import get_logger
from ..safety.parser import ParsedStatement, split_sql
from .session import Database

log = get_logger("db.executor")


@dataclass
class ExecResult:
    ok: bool
    duration_ms: int = 0
    error: str | None = None
    sqlstate: str | None = None
    warnings: list[str] = field(default_factory=list)


def _format_pg_error(e: psycopg.Error) -> str:
    diag = e.diag
    parts = [f"SQLSTATE {e.sqlstate}"]
    if diag.message_primary:
        parts.append(str(diag.message_primary))
    if diag.statement_position:
        parts.append(f"位置 {diag.statement_position}")
    if diag.message_hint:
        parts.append(f"提示: {diag.message_hint}")
    return " | ".join(parts)


class Executor:
    def __init__(self, db: Database) -> None:
        self.db = db

    def preview(self, statements: list[ParsedStatement]) -> ExecResult:
        """事务试执行后回滚，不产生任何持久变更。"""
        warnings: list[str] = []
        runnable: list[str] = []
        for st in statements:
            if st.nontransactional:
                warnings.append(
                    f"语句 {st.index + 1}（{st.verb} …CONCURRENTLY/非事务）无法在事务中试执行，"
                    "本次 preview 跳过该语句，应用时将在自动提交模式下真实执行"
                )
            else:
                runnable.append(st.sql)

        start = time.monotonic()
        try:
            if runnable:
                with self.db.rollback_transaction() as conn:
                    for sql in runnable:
                        conn.execute(sql)
        except psycopg.Error as e:
            return ExecResult(
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=_format_pg_error(e),
                sqlstate=e.sqlstate,
                warnings=warnings,
            )
        return ExecResult(ok=True, duration_ms=int((time.monotonic() - start) * 1000),
                          warnings=warnings)

    def apply(
        self,
        statements: list[ParsedStatement],
        ledger_insert: Callable[[psycopg.Connection], None] | None,
    ) -> ExecResult:
        """真实应用变更。

        常规路径：单事务内执行全部 DDL 并写台账（原子）。
        含非事务语句（CONCURRENTLY）时：自动提交逐条执行，台账在之后单独事务写入
        （此路径下中途失败需人工核对，status 命令会暴露不一致）。
        """
        start = time.monotonic()
        has_ntx = any(st.nontransactional for st in statements)
        try:
            if has_ntx:
                with self.db.autocommit() as conn:
                    for st in statements:
                        conn.execute(st.sql)
                if ledger_insert is not None:
                    with self.db.transaction() as tx_conn:
                        ledger_insert(tx_conn)
            else:
                with self.db.transaction() as conn:
                    for st in statements:
                        conn.execute(st.sql)
                    if ledger_insert is not None:
                        ledger_insert(conn)
        except psycopg.Error as e:
            return ExecResult(
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=_format_pg_error(e),
                sqlstate=e.sqlstate,
                warnings=["非事务语句执行失败时，先前语句可能已提交，请用 `pgagent status` 核对"] if has_ntx else [],
            )
        return ExecResult(ok=True, duration_ms=int((time.monotonic() - start) * 1000))

    def execute_admin(self, sql: str) -> None:
        """执行内部维护 SQL（如创建台账表）。"""
        with self.db.transaction() as conn:
            conn.execute(sql)
