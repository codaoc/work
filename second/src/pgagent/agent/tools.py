"""工具层（设计文档 §4.2）：LLM 可调用的全部 5 个能力 + dispatch。

模型不直接持有数据库连接；每个工具都是可门控、可审计的动作钩子。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pglast.ast as ast
from rich.console import Console

from ..config import Config
from ..db.executor import Executor
from ..db.introspect import TableSummary, inspect_tables, list_tables
from ..db.session import Database
from ..errors import PgAgentError, SqlValidationError
from ..logging import get_logger
from ..migration.runner import MigrationRunner
from ..safety.parser import ParsedStatement, parse_statements
from ..safety.risk import assess

log = get_logger("agent.tools")

# ---- 工具定义（发往 Claude API 的 JSON Schema） ---------------------------

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "列出数据库中所有用户表（含列数、行数估计、占用空间）。"
            "设计任何变更前先调用它获得全局视野。只读、无参数。"
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "inspect_schema",
        "description": (
            "获取指定表的完整结构：列、类型、默认值、NOT NULL、主外键、唯一/检查约束、索引，"
            "以精确 DDL 返回。引用任何表之前必须先用它确认真实结构。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "表名列表，如 [\"orders\"] 或 [\"public.orders\"]",
                }
            },
            "required": ["tables"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "preview_sql",
        "description": (
            "在事务中试执行 SQL 后回滚（dry-run，不产生持久变更），"
            "由真实数据库验证语法/依赖/约束。任何 DDL 在 apply_migration 之前必须先通过本工具。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "完整 DDL，可含多条语句"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "apply_migration",
        "description": (
            "将变更作为正式迁移应用：落盘迁移文件、事务内执行 DDL 并写台账（审计记录）。"
            "中高风险变更会请求用户确认。只能应用已通过 preview_sql 的 SQL。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "kebab-case 短名，如 add-refund-table"},
                "sql": {"type": "string", "description": "完整 DDL（不要包含 BEGIN/COMMIT）"},
                "request_summary": {
                    "type": "string",
                    "description": "用户原始需求的一句话概括（写入台账供审计）",
                },
                "down_sql": {
                    "type": "string",
                    "description": "可选：对应的回滚 DDL，应用后会先试执行验证",
                },
            },
            "required": ["name", "sql", "request_summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_migrations",
        "description": "列出已应用的迁移历史（版本、名称、需求摘要、应用时间）。只读、无参数。",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
]


# ---- 工具运行时上下文 -------------------------------------------------------


@dataclass
class ToolContext:
    cfg: Config
    db: Database
    executor: Executor
    runner: MigrationRunner
    console: Console
    session_id: str
    introspected: bool = False       # 本轮是否已内省（工具协议的软强化）
    preview_failures: int = 0        # 本轮 preview_sql 连续失败次数（自愈上限）


@dataclass
class ToolResult:
    ok: bool
    content: str
    is_error: bool = field(default=False)


def dispatch(ctx: ToolContext, name: str, tool_input: dict[str, Any]) -> ToolResult:
    """工具分发入口：循环层只调用这里。"""
    log.debug("工具调用 %s(%s)", name, _brief(tool_input))
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            return ToolResult(ok=False, content=f"未知工具：{name}", is_error=True)
        result = handler(ctx, tool_input)
    except SqlValidationError as e:
        result = ToolResult(ok=False, content=str(e), is_error=True)
    except PgAgentError as e:
        result = ToolResult(ok=False, content=str(e), is_error=True)
    except Exception as e:  # 防御性兜底：错误必须回传模型而非崩溃
        log.exception("工具 %s 未预期异常", name)
        result = ToolResult(ok=False, content=f"内部错误：{type(e).__name__}: {e}", is_error=True)
    if name == "preview_sql":
        ctx.preview_failures = ctx.preview_failures + 1 if not result.ok else 0
    return result


# ---- 各工具实现 ------------------------------------------------------------


def _list_tables(ctx: ToolContext, _input: dict[str, Any]) -> ToolResult:
    tables = list_tables(ctx.db.conn)
    ctx.introspected = True
    if not tables:
        return ToolResult(ok=True, content="数据库当前没有任何用户表（全新数据库）。")
    lines = [
        f"- {t.qualified}: {t.columns} 列, 约 {t.row_estimate} 行, {t.human_size}"
        for t in tables
    ]
    return ToolResult(ok=True, content="当前表清单（行数为估计值，用于风险判断）：\n" + "\n".join(lines))


def _inspect_schema(ctx: ToolContext, tool_input: dict[str, Any]) -> ToolResult:
    names = tool_input["tables"]
    if not names or not all(isinstance(n, str) and n for n in names):
        return ToolResult(ok=False, content="tables 必须是非空表名字符串数组", is_error=True)
    parts, missing = inspect_tables(ctx.db.conn, names)
    ctx.introspected = True
    if not parts:
        return ToolResult(
            ok=False,
            content=f"⚠️ 以下表不存在（注意：不要凭想象引用不存在的表）：{', '.join(missing)}",
            is_error=True,
        )
    chunks = ["\n\n".join(parts)]
    if missing:
        chunks.append(f"⚠️ 以下表不存在（注意：不要凭想象引用不存在的表）：{', '.join(missing)}")
    return ToolResult(ok=True, content="\n\n".join(chunks))


def _preview_sql(ctx: ToolContext, tool_input: dict[str, Any]) -> ToolResult:
    sql = tool_input["sql"]
    statements = parse_statements(sql, ctx.cfg.safety.statement_whitelist)
    tables = list_tables(ctx.db.conn)
    ctx.introspected = True
    risk = assess(statements, tables, ctx.cfg.safety.large_table_rows)
    result = ctx.executor.preview(statements)
    if not result.ok:
        return ToolResult(
            ok=False,
            content=f"试执行失败（未做任何变更）。\n{result.error}\n"
                    "请根据以上数据库错误修正 SQL 后重试。",
            is_error=True,
        )
    parts = [f"试执行通过（事务已回滚，无持久变更）。风险评级：{risk.label}"]
    if risk.hits:
        parts.append(risk.describe())
    parts.extend(f"注意：{w}" for w in result.warnings)
    return ToolResult(ok=True, content="\n".join(parts))


def _apply_migration(ctx: ToolContext, tool_input: dict[str, Any]) -> ToolResult:
    name = str(tool_input["name"])
    sql = str(tool_input["sql"])
    request_summary = str(tool_input["request_summary"])
    down_sql = tool_input.get("down_sql")

    statements = parse_statements(sql, ctx.cfg.safety.statement_whitelist)
    tables = list_tables(ctx.db.conn)
    risk = assess(statements, tables, ctx.cfg.safety.large_table_rows)
    outcome = ctx.runner.apply(
        name=name, sql=sql, request_summary=request_summary,
        tables=_tables_touched(statements), risk=risk, down_sql=down_sql or None,
    )
    lines = [
        f"迁移已应用：{outcome.version}（{outcome.gate == 'approved' and '经用户确认' or '低危自动应用'}）",
        f"文件：{outcome.path}",
        f"耗时：{outcome.duration_ms} ms，风险：{risk.label}",
    ]
    if down_sql:
        lines.append(
            "down migration 已落盘并通过试执行验证" if outcome.down_valid
            else "⚠️ down migration 已落盘，但试执行失败，回滚不可依赖"
        )
    return ToolResult(ok=True, content="\n".join(lines))


def _list_migrations(ctx: ToolContext, _input: dict[str, Any]) -> ToolResult:
    rows = ctx.runner.history()
    if not rows:
        return ToolResult(ok=True, content="尚无已应用的迁移。")
    lines = [
        f"- {r['version']} {r['name']}（{r['duration_ms'] or '?'} ms, {r['applied_at']}）"
        + (f" 需求：{r['request']}" if r["request"] else "")
        for r in rows
    ]
    return ToolResult(ok=True, content="迁移历史（旧→新）：\n" + "\n".join(lines))


_HANDLERS = {
    "list_tables": _list_tables,
    "inspect_schema": _inspect_schema,
    "preview_sql": _preview_sql,
    "apply_migration": _apply_migration,
    "list_migrations": _list_migrations,
}


# ---- 辅助 -------------------------------------------------------------------


def _tables_touched(statements: list[ParsedStatement]) -> list[str]:
    """粗提取语句涉及的表名（写入设计日志的“涉及表”）。"""
    names: list[str] = []
    for st in statements:
        node = st.node
        if isinstance(node, (ast.CreateStmt, ast.IndexStmt, ast.AlterTableStmt)):
            rv = getattr(node, "relation", None)
            if rv is not None and getattr(rv, "relname", None):
                names.append(rv.relname)
        elif isinstance(node, ast.DropStmt):
            names.extend(_drop_names(node))
        elif isinstance(node, ast.TruncateStmt):
            names.extend(rv.relname for rv in node.relations if getattr(rv, "relname", None))
        elif isinstance(node, ast.RenameStmt):
            rv = getattr(node, "relation", None)
            if rv is not None and getattr(rv, "relname", None):
                names.append(rv.relname)
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _drop_names(node: ast.DropStmt) -> list[str]:
    out = []
    for obj in getattr(node, "objects", ()):
        parts = [getattr(p, "sval", None) for p in obj]
        parts = [p for p in parts if p]
        if parts:
            out.append(parts[-1])
    return out


def _brief(tool_input: dict[str, Any]) -> str:
    s = str(tool_input)
    return s if len(s) <= 120 else s[:120] + "…"
