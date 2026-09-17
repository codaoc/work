"""集成测试：内省 / 试执行 / runner / 台账 / 漂移（真实 PostgreSQL）。"""

from __future__ import annotations

import pytest

from pgagent.config import Config, LLMConfig
from pgagent.db.executor import Executor
from pgagent.db.introspect import inspect_tables, list_tables, schema_fingerprint
from pgagent.db.session import Database
from pgagent.errors import GateRejected, RunnerError, SqlValidationError
from pgagent.migration.runner import MigrationRunner
from pgagent.safety.confirm import NonInteractiveGate
from pgagent.safety.parser import parse_statements
from pgagent.safety.risk import assess

from ..conftest import TEST_URL, requires_pg

WL = ("CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE")

USERS_SQL = (
    "CREATE TABLE users ("
    " id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    " email text NOT NULL UNIQUE,"
    " created_at timestamptz NOT NULL DEFAULT now())"
)


def make_cfg(tmp_path, allow="ask", max_preview=5) -> Config:
    return Config(llm=LLMConfig(max_preview_iterations=max_preview), project_root=tmp_path)


def make_runner(cfg, db, approved=True) -> MigrationRunner:
    ex = Executor(db)
    runner = MigrationRunner(cfg, db, ex, NonInteractiveGate(approved), session_id="test")
    runner.ensure_ready()
    return runner


def _risk(db, sql):
    return assess(parse_statements(sql, WL), list_tables(db.conn), 100_000)


def _table_exists(db, name) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relname=%s AND n.nspname='public'", (name,)
    ).fetchone()
    return row is not None


@requires_pg
def test_preview_rolls_back(db):
    ex = Executor(db)
    assert ex.preview(parse_statements(USERS_SQL, WL)).ok
    assert not _table_exists(db, "users"), "preview 之后表不应存在"


@requires_pg
def test_preview_reports_db_error(db):
    ex = Executor(db)
    bad = "ALTER TABLE nonexistent ADD COLUMN c int"
    result = ex.preview(parse_statements(bad, WL))
    assert not result.ok
    assert "42P01" in (result.error or "")  # undefined_table


@requires_pg
def test_inspect_renders_ddl_and_flags_missing(db):
    db.conn.execute(USERS_SQL)
    parts, missing = inspect_tables(db.conn, ["users", "ghost"])
    assert len(parts) >= 1 and "users" in parts[0] and "GENERATED ALWAYS AS IDENTITY" in parts[0]
    assert missing == ["ghost"]


@requires_pg
def test_apply_writes_file_ledger_and_design_log(db, tmp_path):
    cfg = make_cfg(tmp_path)
    runner = make_runner(cfg, db)
    out = runner.apply(
        name="create-users", sql=USERS_SQL, request_summary="创建用户表",
        tables=["users"], risk=_risk(db, USERS_SQL),
    )
    assert _table_exists(db, "users")
    assert (tmp_path / "migrations" / f"{out.version}_create-users.sql").exists()
    log_text = (tmp_path / "SCHEMA_DESIGN.md").read_text(encoding="utf-8")
    assert "create-users" in log_text and "创建用户表" in log_text
    history = runner.history()
    assert history and history[0]["name"] == "create-users"
    assert history[0]["request"] == "创建用户表"


@requires_pg
def test_gate_rejection_leaves_no_trace(db, tmp_path):
    cfg = make_cfg(tmp_path)
    runner = make_runner(cfg, db, approved=False)
    sql = "DROP TABLE users"
    db.conn.execute("CREATE TABLE users(id int)")
    with pytest.raises(GateRejected):
        runner.apply(name="drop-users", sql=sql, request_summary="删表",
                     tables=["users"], risk=_risk(db, sql))
    assert _table_exists(db, "users"), "拒绝后表必须还在"
    assert not list((tmp_path / "migrations").glob("*.sql"))
    assert runner.history() == []


@requires_pg
def test_deny_policy_blocks_high_risk_without_asking(db, tmp_path):
    """allow_destructive=deny 时高危变更直接拒绝，即使用户愿意批准。"""
    import pgagent.config as C

    cfg = Config(
        llm=LLMConfig(max_preview_iterations=5), project_root=tmp_path,
        safety=C.SafetyConfig(allow_destructive="deny"),
    )
    runner2 = MigrationRunner(cfg, db, Executor(db), NonInteractiveGate(True), "test")
    db.conn.execute("CREATE TABLE users(id int)")
    with pytest.raises(GateRejected):
        runner2.apply(name="drop-users", sql="DROP TABLE users", request_summary="",
                      tables=["users"], risk=_risk(db, "DROP TABLE users"))


@requires_pg
def test_low_risk_auto_applies_without_gate(db, tmp_path):
    runner = make_runner(make_cfg(tmp_path), db, approved=False)
    out = runner.apply(name="create-users", sql=USERS_SQL, request_summary="",
                       tables=["users"], risk=_risk(db, USERS_SQL))
    assert out.gate == "skip"
    assert _table_exists(db, "users")


@requires_pg
def test_ledger_atomic_with_ddl(db, tmp_path):
    """DDL 与台账同事务：模拟台账写入失败时 DDL 必须回滚。"""
    from pgagent.db.executor import Executor

    ex = Executor(db)
    sql = USERS_SQL
    stmts = parse_statements(sql, WL)

    def broken_insert(_conn):
        raise RuntimeError("模拟台账写入失败")

    with pytest.raises(RuntimeError):
        ex.apply(stmts, broken_insert)
    assert not _table_exists(db, "users"), "台账失败时 DDL 必须一起回滚"


@requires_pg
def test_drift_detection(db, tmp_path):
    runner = make_runner(make_cfg(tmp_path), db)
    runner.apply(name="create-users", sql=USERS_SQL, request_summary="",
                 tables=["users"], risk=_risk(db, USERS_SQL))
    report = runner.status()
    assert report["issues"] == []

    # 绕过 agent 手工改库 → 漂移告警
    db.conn.execute("ALTER TABLE users ADD COLUMN sneaky int")
    report = runner.status()
    assert any("漂移" in str(i) for i in report["issues"])


@requires_pg
def test_checksum_detects_manual_edit(db, tmp_path):
    runner = make_runner(make_cfg(tmp_path), db)
    out = runner.apply(name="create-users", sql=USERS_SQL, request_summary="",
                       tables=["users"], risk=_risk(db, USERS_SQL))
    path = tmp_path / "migrations" / f"{out.version}_create-users.sql"
    path.write_text(path.read_text(encoding="utf-8") + "-- 手改\n", encoding="utf-8")
    issues = runner.status()["issues"]
    assert any("checksum" in str(i) for i in issues)


@requires_pg
def test_invalid_name_rejected(db, tmp_path):
    runner = make_runner(make_cfg(tmp_path), db)
    with pytest.raises(RunnerError):
        runner.apply(name="Bad Name!", sql=USERS_SQL, request_summary="",
                     tables=[], risk=_risk(db, USERS_SQL))


@requires_pg
def test_down_sql_validated_by_preview(db, tmp_path):
    runner = make_runner(make_cfg(tmp_path), db)
    out = runner.apply(
        name="create-users", sql=USERS_SQL, request_summary="",
        tables=["users"], risk=_risk(db, USERS_SQL),
        down_sql="DROP TABLE users",
    )
    assert out.down_valid is True
    assert (tmp_path / "migrations" / f"{out.version}_create-users.down.sql").exists()


@requires_pg
def test_schema_fingerprint_changes_with_structure(db):
    f1 = schema_fingerprint(db.conn)
    db.conn.execute(USERS_SQL)
    f2 = schema_fingerprint(db.conn)
    assert f1 != f2
    assert schema_fingerprint(db.conn) == f2  # 稳定


@requires_pg
def test_tool_dispatch_end_to_end(db, tmp_path):
    """不经 LLM，直接驱动工具层：list → preview → apply → history。"""
    from rich.console import Console

    from pgagent.agent.tools import ToolContext, dispatch

    cfg = make_cfg(tmp_path)
    ex = Executor(db)
    runner = MigrationRunner(cfg, db, ex, NonInteractiveGate(True), "sess")
    runner.ensure_ready()
    ctx = ToolContext(cfg=cfg, db=db, executor=ex, runner=runner,
                      console=Console(), session_id="sess")

    r1 = dispatch(ctx, "list_tables", {})
    assert r1.ok and "全新" in r1.content

    r2 = dispatch(ctx, "inspect_schema", {"tables": ["nope"]})
    assert not r2.ok and "不存在" in r2.content

    r3 = dispatch(ctx, "preview_sql", {"sql": USERS_SQL})
    assert r3.ok and "低危" in r3.content

    r4 = dispatch(ctx, "apply_migration", {
        "name": "create-users", "sql": USERS_SQL, "request_summary": "测试",
    })
    assert r4.ok and "已应用" in r4.content

    r5 = dispatch(ctx, "list_migrations", {})
    assert r5.ok and "create-users" in r5.content

    # 危险 DDL 被白名单拦截
    r6 = dispatch(ctx, "preview_sql", {"sql": "SELECT 1"})
    assert not r6.ok and "白名单" in r6.content


@requires_pg
def test_apply_visible_to_other_connections(db, tmp_path):
    """回归：台账与 DDL 必须真正提交——另一个连接立即可见（防隐式事务吞噬提交）。"""
    import psycopg

    runner = make_runner(make_cfg(tmp_path), db)
    out = runner.apply(name="create-users", sql=USERS_SQL, request_summary="",
                       tables=["users"], risk=_risk(db, USERS_SQL))
    with psycopg.connect(TEST_URL, autocommit=True) as other:
        assert other.execute(
            "SELECT count(*) FROM agent_schema_migrations WHERE version = %s",
            (out.version,),
        ).fetchone()[0] == 1
        assert other.execute("SELECT to_regclass('public.users')").fetchone()[0] == "users"


@requires_pg
def test_preview_visible_to_no_one(db, tmp_path):
    """回归：preview 在任何连接上都不可见。"""
    import psycopg

    ex = Executor(db)
    ex.preview(parse_statements(USERS_SQL, WL))
    with psycopg.connect(TEST_URL, autocommit=True) as other:
        assert other.execute("SELECT to_regclass('public.users')").fetchone()[0] is None


@requires_pg
def test_apply_validates_sql_again(db, tmp_path):
    """runner 对工具层传入的 SQL 做防御性二次校验：DML 在 L1 被拒。"""
    runner = make_runner(make_cfg(tmp_path), db)
    with pytest.raises(SqlValidationError):
        runner.apply(name="x", sql="INSERT INTO users VALUES (1);", request_summary="",
                     tables=[], risk=_risk(db, "CREATE TABLE a(id int)"))


@requires_pg
def test_apply_reports_execution_error(db, tmp_path):
    """白名单内但数据库拒绝的 SQL：错误以 RunnerError 报告（模型可据此自愈）。"""
    runner = make_runner(make_cfg(tmp_path), db)
    with pytest.raises(RunnerError) as e:
        runner.apply(name="drop-missing", sql="DROP TABLE missing_table_xyz", request_summary="",
                     tables=[], risk=_risk(db, "DROP TABLE missing_table_xyz"))
    assert "42P01" in str(e.value)
