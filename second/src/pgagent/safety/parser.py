"""验证管线 L0（语法解析）/ L1（语句白名单），基于 pglast（PostgreSQL 真实 parser）。

同时负责按语句切分 SQL（供执行器逐条执行）与识别不能进事务块的语句。
"""

from __future__ import annotations

from dataclasses import dataclass

import pglast
import pglast.ast as ast
from pglast.parser import ParseError

from ..errors import SqlValidationError

# 语句 AST 类 → 白名单动词。未映射的类按 "OTHER" 处理（默认被拒）。
_VERB_MAP: dict[type, str] = {
    ast.CreateStmt: "CREATE",
    ast.CreateEnumStmt: "CREATE",
    ast.CreateSeqStmt: "CREATE",
    ast.CreateSchemaStmt: "CREATE",
    ast.CreateTrigStmt: "CREATE",
    ast.CreateFunctionStmt: "CREATE",
    ast.CreateDomainStmt: "CREATE",
    ast.CreateConversionStmt: "CREATE",
    ast.CreateStatsStmt: "CREATE",
    ast.IndexStmt: "CREATE",
    ast.AlterTableStmt: "ALTER",
    ast.AlterObjectSchemaStmt: "ALTER",
    ast.AlterSeqStmt: "ALTER",
    ast.AlterOwnerStmt: "ALTER",
    ast.RenameStmt: "ALTER",
    ast.DropStmt: "DROP",
    ast.TruncateStmt: "TRUNCATE",
    ast.CommentStmt: "COMMENT",
}


@dataclass(frozen=True)
class ParsedStatement:
    verb: str
    node: object  # pglast.ast 节点
    sql: str      # 归一化后的单条 SQL（可供执行器直接执行）
    index: int
    nontransactional: bool  # 不能进事务块（如 CREATE INDEX CONCURRENTLY）


def parse_statements(sql: str, whitelist: tuple[str, ...] | list[str]) -> list[ParsedStatement]:
    """L0 语法解析 + L1 白名单校验，返回逐语句列表。"""
    if not sql or not sql.strip():
        raise SqlValidationError("L0-syntax", "SQL 为空")
    try:
        raws = pglast.parse_sql(sql)
    except ParseError as e:
        loc = getattr(e, "location", None)
        where = f"（位置 {loc}）" if loc is not None else ""
        raise SqlValidationError("L0-syntax", f"PostgreSQL 语法错误{where}: {e}") from None

    if not raws:
        raise SqlValidationError("L0-syntax", "SQL 中没有可执行的语句")

    stmts: list[ParsedStatement] = []
    for i, raw in enumerate(raws):
        node = raw.stmt
        verb = _VERB_MAP.get(type(node), f"OTHER:{type(node).__name__}")
        if verb not in whitelist:
            raise SqlValidationError(
                "L1-whitelist",
                f"语句 {i + 1} 的类型「{verb}」（{type(node).__name__}）不在白名单内，"
                f"允许的语句类型: {', '.join(whitelist)}。"
                "pg-agent 只管理表结构（DDL），数据操作与权限操作被禁止。",
            )
        stmts.append(
            ParsedStatement(
                verb=verb,
                node=node,
                sql=_slice_statement(sql, raw.stmt_location, raw.stmt_len),
                index=i,
                nontransactional=_is_nontransactional(node),
            )
        )
    return stmts


def split_sql(sql: str) -> list[str]:
    """按语句切分（带白名单兜底校验供执行器内部使用会重复解析，这里仅切分）。"""
    raws = pglast.parse_sql(sql)
    return [_slice_statement(sql, r.stmt_location, r.stmt_len) for r in raws]


def _slice_statement(sql: str, location: int, length: int) -> str:
    """按字符偏移切片（pglast v8 的 stmt_location/stmt_len 为字符索引），去掉尾部分号。"""
    start = max(int(location), 0)
    end = start + int(length) if length and int(length) > 0 else None
    chunk = sql[start:end].strip()
    if not chunk:
        return chunk
    return chunk.rstrip(";").strip() + ";"


def _is_nontransactional(node: object) -> bool:
    if isinstance(node, ast.IndexStmt):
        return bool(getattr(node, "concurrent", False))
    if isinstance(node, ast.DropStmt):
        return bool(getattr(node, "concurrent", False))
    return False
