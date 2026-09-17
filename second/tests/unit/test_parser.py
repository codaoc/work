"""验证管线 L0/L1（pglast 解析与白名单）单元测试。"""

from __future__ import annotations

import pytest

from pgagent.errors import SqlValidationError
from pgagent.safety.parser import parse_statements, split_sql

WL = ("CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE")


def test_parse_and_split():
    sql = "CREATE TABLE a(id int);\nCREATE INDEX i ON a(id);\n"
    stmts = parse_statements(sql, WL)
    assert [s.verb for s in stmts] == ["CREATE", "CREATE"]
    assert len(stmts) == 2
    assert stmts[0].sql == "CREATE TABLE a(id int);"
    assert not stmts[0].nontransactional


def test_syntax_error_is_l0():
    with pytest.raises(SqlValidationError) as e:
        parse_statements("CREAT TABLE x", WL)
    assert e.value.layer == "L0-syntax"


def test_empty_sql():
    with pytest.raises(SqlValidationError):
        parse_statements("   ;  ", WL)


def test_whitelist_rejects_dml_and_grant():
    for sql in (
        "INSERT INTO a VALUES (1)",
        "UPDATE a SET id = 1",
        "DELETE FROM a",
        "GRANT ALL ON a TO PUBLIC",
        "SELECT 1",
        "COPY a FROM '/tmp/x'",
        "SET search_path = public",
    ):
        with pytest.raises(SqlValidationError) as e:
            parse_statements(sql, WL)
        assert e.value.layer == "L1-whitelist", sql


def test_multibyte_sql_survives_slicing():
    sql = "-- 中文注释 🈶\nCREATE TABLE 用户(id int);"
    stmts = parse_statements(sql, WL)
    assert len(stmts) == 1
    assert "CREATE TABLE 用户(id int);" == stmts[0].sql


def test_concurrently_flagged_nontransactional():
    stmts = parse_statements("CREATE INDEX CONCURRENTLY i ON a(id);", WL)
    assert stmts[0].nontransactional
    stmts = parse_statements("CREATE INDEX i ON a(id);", WL)
    assert not stmts[0].nontransactional


def test_split_sql_matches_count():
    parts = split_sql("CREATE TABLE a(id int); COMMENT ON TABLE a IS 'x;y';")
    assert len(parts) == 2  # 注释字符串里的分号不应被切断
