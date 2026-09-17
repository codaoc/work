"""风险规则引擎（L2）单元测试：覆盖设计文档 §4.6 的规则表。"""

from __future__ import annotations

from pgagent.db.introspect import TableSummary
from pgagent.safety.parser import parse_statements
from pgagent.safety.risk import RiskLevel, assess

WL = ("CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE")


def level_of(sql: str, tables=(), large=100_000) -> int:
    stmts = parse_statements(sql, WL)
    return assess(stmts, list(tables), large).level


def rules_of(sql: str) -> set[str]:
    stmts = parse_statements(sql, WL)
    return {h.rule for h in assess(stmts, [], 100_000).hits}


def test_create_table_low():
    assert level_of("CREATE TABLE t(id bigint PRIMARY KEY)") == RiskLevel.LOW


def test_add_column_low():
    assert level_of("ALTER TABLE t ADD COLUMN c text") == RiskLevel.LOW


def test_add_not_null_column_medium():
    assert level_of("ALTER TABLE t ADD COLUMN c text NOT NULL DEFAULT 'x'") == RiskLevel.MEDIUM
    assert "R_ADD_NOT_NULL_COLUMN" in rules_of("ALTER TABLE t ADD COLUMN c text NOT NULL")


def test_drop_table_high():
    assert level_of("DROP TABLE t") == RiskLevel.HIGH


def test_drop_cascade_high():
    assert level_of("DROP TABLE t CASCADE") == RiskLevel.HIGH
    assert "R_DROP_CASCADE" in rules_of("DROP TABLE t CASCADE")


def test_truncate_high():
    assert level_of("TRUNCATE t") == RiskLevel.HIGH


def test_drop_column_and_constraint_high():
    assert level_of("ALTER TABLE t DROP COLUMN c") == RiskLevel.HIGH
    assert level_of("ALTER TABLE t DROP CONSTRAINT c") == RiskLevel.HIGH


def test_alter_type_high():
    assert level_of("ALTER TABLE t ALTER COLUMN c TYPE text") == RiskLevel.HIGH
    assert "R_ALTER_TYPE" in rules_of("ALTER TABLE t ALTER COLUMN c TYPE text")


def test_set_not_null_medium():
    assert level_of("ALTER TABLE t ALTER COLUMN c SET NOT NULL") == RiskLevel.MEDIUM


def test_add_fk_medium_with_notvalid_hint():
    rules = rules_of(
        "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES b(id)"
    )
    assert "R_ADD_FK" in rules
    assert level_of("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES b(id)") == RiskLevel.MEDIUM


def test_plain_index_medium_concurrent_low():
    assert level_of("CREATE INDEX i ON t(c)") == RiskLevel.MEDIUM
    assert level_of("CREATE INDEX CONCURRENTLY i ON t(c)") == RiskLevel.LOW


def test_rename_medium():
    assert level_of("ALTER TABLE t RENAME COLUMN a TO b") == RiskLevel.MEDIUM


def test_comment_low():
    assert level_of("COMMENT ON TABLE t IS 'hi'") == RiskLevel.LOW


def test_large_table_escalates_alter_to_medium():
    big = [TableSummary("public", "orders", 10, 500_000, 1)]
    # 即使子操作本身低危（加可空列），大表上的 ALTER 也是中危
    assert level_of("ALTER TABLE orders ADD COLUMN c text", big) == RiskLevel.MEDIUM
    stmts = parse_statements("ALTER TABLE orders ADD COLUMN c text", WL)
    hits = {h.rule for h in assess(stmts, big, 100_000).hits}
    assert "R_LARGE_TABLE" in hits


def test_multi_statement_takes_max():
    sql = "CREATE TABLE a(id int);\nDROP TABLE b;"
    assert level_of(sql) == RiskLevel.HIGH


def test_describe_lists_rules():
    stmts = parse_statements("DROP TABLE t CASCADE", WL)
    text = assess(stmts, [], 100_000).describe()
    assert "R_DROP_TABLE" in text and "R_DROP_CASCADE" in text
