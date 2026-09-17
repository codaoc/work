"""集成测试：Agent 循环全链路（FakeClient 脚本化 tool calls，真实 PostgreSQL）。

不调用真实 LLM API——脚本模拟模型的工具调用序列，验证循环、自愈、
自愈上限与确认门控拒绝路径的编排正确性（设计文档 §4.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from rich.console import Console

from pgagent.agent.loop import AgentLoop, Session
from pgagent.agent.tools import ToolContext
from pgagent.config import Config, LLMConfig
from pgagent.db.executor import Executor
from pgagent.migration.runner import MigrationRunner
from pgagent.safety.confirm import NonInteractiveGate

from ..conftest import requires_pg

USERS_SQL = (
    "CREATE TABLE users ("
    " id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    " email text NOT NULL,"
    " created_at timestamptz NOT NULL DEFAULT now())"
)


# ---- Fake Anthropic 客户端 --------------------------------------------------


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class FakeUsage:
    input_tokens: int = 1
    output_tokens: int = 1
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list
    stop_reason: str
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeMessages:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(kwargs)
        assert self._client.script, "脚本耗尽：循环未按预期终止"
        return self._client.script.pop(0)


class FakeClient:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.script = script
        self.calls: list[dict] = []
        self.messages = FakeMessages(self)


def make_loop(cfg: Config, db, tmp_path, script, approved=True) -> AgentLoop:
    ex = Executor(db)
    runner = MigrationRunner(cfg, db, ex, NonInteractiveGate(approved), "sess-loop")
    runner.ensure_ready()
    ctx = ToolContext(cfg=cfg, db=db, executor=ex, runner=runner,
                      console=Console(), session_id="sess-loop")
    session = Session.create(cfg.state_dir)
    return AgentLoop(FakeClient(script), cfg, ctx, session, Console(),
                     "system-prompt", stream=False)


def make_cfg(tmp_path, max_preview=5) -> Config:
    return Config(llm=LLMConfig(max_preview_iterations=max_preview), project_root=tmp_path)


def tool_call(cid: str, tool_name: str, **tool_input) -> FakeResponse:
    return FakeResponse(content=[FakeBlock(type="tool_use", id=cid, name=tool_name, input=tool_input)],
                        stop_reason="tool_use")


def end_turn(text: str) -> FakeResponse:
    return FakeResponse(content=[FakeBlock(type="text", text=text)], stop_reason="end_turn")


def _tool_results(turn_messages) -> dict[str, dict]:
    out = {}
    for m in turn_messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out[block["tool_use_id"]] = block
    return out


# ---- 测试 ------------------------------------------------------------------


@requires_pg
def test_happy_path_full_chain(db, tmp_path):
    script = [
        tool_call("t1", "list_tables"),
        tool_call("t2", "preview_sql", sql=USERS_SQL),
        tool_call("t3", "apply_migration", name="create-users",
                  sql=USERS_SQL, request_summary="创建用户表"),
        end_turn("已创建 users 表。"),
    ]
    loop = make_loop(make_cfg(tmp_path), db, tmp_path, script)
    reply = loop.run_turn("帮我建一个用户表")
    assert reply == "已创建 users 表。"

    # 数据库与文件系统副作用
    assert db.conn.execute(
        "SELECT to_regclass('public.users')"
    ).fetchone()[0] == "users"
    assert (tmp_path / "migrations").glob("*_create-users.sql")
    assert "create-users" in (tmp_path / "SCHEMA_DESIGN.md").read_text(encoding="utf-8")

    # 协议：assistant 的 tool_use 都有对应 tool_result
    results = _tool_results(loop.session.messages)
    assert set(results) == {"t1", "t2", "t3"}
    assert all(not r.get("is_error") for r in results.values())


@requires_pg
def test_self_heal_after_preview_failure(db, tmp_path):
    bad_sql = "ALTER TABLE ghost_table ADD COLUMN x int"  # 引用不存在的表
    script = [
        tool_call("t1", "preview_sql", sql=bad_sql),                # 失败 → is_error
        tool_call("t2", "preview_sql", sql=USERS_SQL),              # 自愈
        tool_call("t3", "apply_migration", name="create-users",
                  sql=USERS_SQL, request_summary="建表"),
        end_turn("修正后已应用。"),
    ]
    loop = make_loop(make_cfg(tmp_path), db, tmp_path, script)
    loop.run_turn("建个表")

    results = _tool_results(loop.session.messages)
    assert results["t1"].get("is_error") is True
    assert "42P01" in results["t1"]["content"]  # 数据库真实错误回传给模型
    assert not results["t2"].get("is_error")
    assert loop.ctx.preview_failures == 0  # 成功后计数清零
    assert db.conn.execute("SELECT to_regclass('public.users')").fetchone()[0] == "users"


@requires_pg
def test_preview_failure_cap_stops_loop(db, tmp_path):
    script = [
        tool_call(f"t{i}", "preview_sql", sql="ALTER TABLE ghost ADD COLUMN x int")
        for i in range(10)
    ]
    loop = make_loop(make_cfg(tmp_path, max_preview=3), db, tmp_path, script)
    reply = loop.run_turn("随便改点什么")
    assert "连续失败" in reply
    # 上限 3 次：第 3 次失败后终止，不再有第 4 次调用
    assert len(_tool_results(loop.session.messages)) == 3
    assert not list((tmp_path / "migrations").glob("*.sql"))


@requires_pg
def test_gate_rejection_feeds_back_to_model(db, tmp_path):
    db.conn.execute("CREATE TABLE legacy(id int)")
    script = [
        tool_call("t1", "preview_sql", sql="DROP TABLE legacy"),
        tool_call("t2", "apply_migration", name="drop-legacy",
                  sql="DROP TABLE legacy", request_summary="删掉旧表"),
        end_turn("用户拒绝了删除，保持现状。"),
    ]
    # NonInteractiveGate(approved=False)：高危(拒绝) / 低危(放行)
    loop = make_loop(make_cfg(tmp_path), db, tmp_path, script, approved=False)
    loop.run_turn("把 legacy 表删了")

    results = _tool_results(loop.session.messages)
    assert results["t2"].get("is_error") is True
    assert "用户拒绝" in results["t2"]["content"]
    # 表仍在
    assert db.conn.execute("SELECT to_regclass('public.legacy')").fetchone()[0] == "legacy"
    assert not list((tmp_path / "migrations").glob("*.sql"))


@requires_pg
def test_session_persists_messages(db, tmp_path):
    script = [end_turn("好的。")]
    loop = make_loop(make_cfg(tmp_path), db, tmp_path, script)
    loop.run_turn("你好")
    lines = loop.session.path.read_text(encoding="utf-8").strip().splitlines()
    roles = [__import__("json").loads(l)["role"] for l in lines]
    assert roles == ["user", "assistant"]
