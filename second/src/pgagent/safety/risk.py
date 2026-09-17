"""验证管线 L2：风险规则引擎（设计文档 §4.6）。

基于 pglast 语句树做确定性分级：HIGH / MEDIUM / LOW，
并输出命中规则清单供确认门控向用户解释后果。

注意：pglast 枚举（ObjectType/AlterTableType/ConstrType 等）的 str() 输出是
数字，必须用 .name 取可读名称。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

import pglast.ast as ast

from ..db.introspect import TableSummary
from .parser import ParsedStatement


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @property
    def label(self) -> str:
        return _LABEL[self]


_LABEL = {RiskLevel.HIGH: "🔴 高危", RiskLevel.MEDIUM: "🟡 中危", RiskLevel.LOW: "🟢 低危"}

HIGH, MEDIUM, LOW = RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW


@dataclass(frozen=True)
class RuleHit:
    rule: str
    detail: str


@dataclass
class RiskAssessment:
    level: int
    hits: list[RuleHit] = field(default_factory=list)

    @property
    def label(self) -> str:
        return RiskLevel(self.level).label

    def describe(self) -> str:
        if not self.hits:
            return self.label
        lines = [f"  • [{h.rule}] {h.detail}" for h in hits_unique(self.hits)]
        return self.label + "\n" + "\n".join(lines)


def hits_unique(hits: list[RuleHit]) -> list[RuleHit]:
    seen: set[tuple[str, str]] = set()
    out = []
    for h in hits:
        key = (h.rule, h.detail)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _enum_name(value: object) -> str:
    """安全取枚举 .name（pglast 枚举 str() 是数字）。"""
    return getattr(value, "name", "") or ""


def assess(
    statements: list[ParsedStatement],
    tables: list[TableSummary],
    large_table_rows: int,
) -> RiskAssessment:
    """对一组语句做风险分级，取所有规则中的最高级别。

    tables: 当前行数估计（来自内省），用于大表规则。
    """
    estimates = {t.name.lower(): t.row_estimate for t in tables}
    level = LOW
    hits: list[RuleHit] = []

    def raise_to(new: int, rule: str, detail: str) -> None:
        nonlocal level
        level = max(level, new)
        hits.append(RuleHit(rule, detail))

    for st in statements:
        node = st.node
        pos = f"语句 {st.index + 1}"

        if isinstance(node, ast.TruncateStmt):
            raise_to(HIGH, "R_TRUNCATE", f"{pos}: TRUNCATE 会清空表数据且不可回滚恢复")

        elif isinstance(node, ast.DropStmt):
            type_name = _enum_name(getattr(node, "removeType", None))
            cascade = _enum_name(getattr(node, "behavior", None)) == "DROP_CASCADE"
            names = _drop_object_names(node)
            if type_name == "OBJECT_TABLE":
                raise_to(HIGH, "R_DROP_TABLE", f"{pos}: DROP TABLE {', '.join(names)} 不可逆删除表与数据")
            else:
                raise_to(MEDIUM, "R_DROP_OBJECT", f"{pos}: DROP {type_name} {', '.join(names)}")
            if cascade:
                raise_to(HIGH, "R_DROP_CASCADE", f"{pos}: CASCADE 会级联删除依赖对象")

        elif isinstance(node, ast.AlterTableStmt):
            table = _rel_name(node.relation)
            rows = estimates.get(table.lower())
            large = rows is not None and rows > large_table_rows
            for cmd in getattr(node, "cmds", ()):
                subtype = _enum_name(getattr(cmd, "subtype", None))
                col = getattr(cmd, "name", None)
                payload = getattr(cmd, "def_", None)
                if subtype == "AT_DropColumn":
                    raise_to(HIGH, "R_DROP_COLUMN",
                             f"{pos}: ALTER TABLE {table} DROP COLUMN {col} 不可逆删除该列数据")
                elif subtype == "AT_DropConstraint":
                    raise_to(HIGH, "R_DROP_CONSTRAINT",
                             f"{pos}: 删除约束 {col} 可能破坏数据完整性假设")
                elif subtype == "AT_AlterColumnType":
                    raise_to(HIGH, "R_ALTER_TYPE",
                             f"{pos}: 修改列 {col} 类型通常需要重写整表"
                             + (f"（约 {rows} 行）" if rows is not None else "")
                             + "，建议 expand/contract 模式")
                elif subtype == "AT_SetNotNull":
                    raise_to(MEDIUM, "R_SET_NOT_NULL",
                             f"{pos}: SET NOT NULL 需全表扫描校验存量行"
                             + (f"（约 {rows} 行，建议 NOT VALID + VALIDATE 拆分）" if large else ""))
                elif subtype == "AT_AddColumn":
                    if _column_def_has_notnull(payload):
                        raise_to(MEDIUM, "R_ADD_NOT_NULL_COLUMN",
                                 f"{pos}: 为存量表新增 NOT NULL 列 {col} 需回填默认值"
                                 + (f"（约 {rows} 行）" if rows is not None else ""))
                    else:
                        raise_to(LOW, "R_ADD_COLUMN", f"{pos}: 新增可空列 {col}")
                elif subtype == "AT_AddConstraint":
                    contype = _enum_name(getattr(payload, "contype", None)) if payload else ""
                    if contype == "CONSTR_FOREIGN":
                        skip = bool(getattr(payload, "skip_validation", False))
                        raise_to(MEDIUM, "R_ADD_FK",
                                 f"{pos}: 新增外键需扫描校验存量行"
                                 + ("（已声明 NOT VALID，校验将延后）" if skip else
                                    "，大表建议 NOT VALID + VALIDATE 拆分"))
                    else:
                        raise_to(MEDIUM, "R_ADD_CONSTRAINT",
                                 f"{pos}: 新增约束 {getattr(payload, 'conname', '') or col or ''}")
                else:
                    raise_to(MEDIUM, "R_ALTER_OTHER",
                             f"{pos}: ALTER TABLE {table} 的子操作 {subtype or '?'} 需要确认")
            if large:
                raise_to(MEDIUM, "R_LARGE_TABLE",
                         f"{pos}: 表 {table} 约 {rows} 行，任何 ALTER 都会短暂持有 ACCESS EXCLUSIVE 锁")

        elif isinstance(node, ast.IndexStmt):
            table = _rel_name(node.relation)
            if getattr(node, "concurrent", False):
                raise_to(LOW, "R_CREATE_INDEX_CONCURRENTLY",
                         f"{pos}: CONCURRENTLY 建索引不阻塞写入（不能进事务，将自动提交执行）")
            else:
                rows = estimates.get(table.lower())
                raise_to(MEDIUM, "R_CREATE_INDEX",
                         f"{pos}: 普通 CREATE INDEX 会阻塞写入"
                         + (f"（表 {table} 约 {rows} 行，建议 CONCURRENTLY）" if rows is not None else ""))

        elif isinstance(node, ast.RenameStmt):
            raise_to(MEDIUM, "R_RENAME",
                     f"{pos}: 重命名（{getattr(node, 'subname', '')} → {getattr(node, 'newname', '')}）"
                     "会破坏所有引用旧名的查询与应用代码")

        elif isinstance(node, (ast.CreateStmt, ast.CreateEnumStmt, ast.CreateSeqStmt,
                               ast.CreateSchemaStmt, ast.CreateTrigStmt)):
            raise_to(LOW, "R_CREATE_OBJECT", f"{pos}: 创建新对象（{type(node).__name__}）")

        elif isinstance(node, ast.CommentStmt):
            raise_to(LOW, "R_COMMENT", f"{pos}: 注释")

        else:
            # 白名单内但规则引擎未覆盖的形态：保守按中危处理
            raise_to(MEDIUM, "R_UNCLASSIFIED",
                     f"{pos}: 未识别的语句形态 {type(node).__name__}，保守按中危处理")

    return RiskAssessment(level=level, hits=hits)


def _rel_name(rangevar: object) -> str:
    schema = getattr(rangevar, "schemaname", None)
    name = getattr(rangevar, "relname", "?")
    return f"{schema}.{name}" if schema else str(name)


def _drop_object_names(node: ast.DropStmt) -> list[str]:
    names = []
    for obj in getattr(node, "objects", ()):
        # objects 元素为名称路径（单段或 schema.限定的节点元组）
        parts = [getattr(p, "sval", None) for p in obj]
        parts = [p for p in parts if p]
        names.append(".".join(parts) if parts else "?")
    return names


def _column_def_has_notnull(columndef: object) -> bool:
    if columndef is None:
        return False
    if bool(getattr(columndef, "is_not_null", False)):
        return True
    for con in getattr(columndef, "constraints", ()) or ():
        if _enum_name(getattr(con, "contype", None)) == "CONSTR_NOTNULL":
            return True
    return False
