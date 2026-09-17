"""迁移台账（设计文档 §4.5）。

台账是迁移的唯一权威记录；DDL 与台账写入同事务提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS agent_schema_migrations (
    version       text PRIMARY KEY,
    name          text NOT NULL,
    checksum      text NOT NULL,
    sql           text NOT NULL,
    request       text,
    session_id    text,
    down_sql      text,
    down_valid    boolean,
    duration_ms   integer,
    applied_at    timestamptz NOT NULL DEFAULT now()
)
"""

# down_sql / down_valid 为设计文档 DDL 之外的字段扩展，用于 FR-9 回滚支持。


@dataclass(frozen=True)
class LedgerRow:
    version: str
    name: str
    checksum: str
    sql: str
    request: str | None
    session_id: str | None
    down_sql: str | None
    down_valid: bool | None
    duration_ms: int | None
    applied_at: datetime


def ensure_ledger(conn: psycopg.Connection) -> None:
    conn.execute(LEDGER_DDL)


def record_migration(
    conn: psycopg.Connection,
    *,
    version: str,
    name: str,
    checksum: str,
    sql: str,
    request: str | None,
    session_id: str | None,
    down_sql: str | None,
    down_valid: bool | None,
    duration_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_schema_migrations
              (version, name, checksum, sql, request, session_id, down_sql, down_valid, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (version, name, checksum, sql, request, session_id, down_sql, down_valid, duration_ms),
    )


def applied_versions(conn: psycopg.Connection) -> dict[str, LedgerRow]:
    rows = conn.execute(
        """
        SELECT version, name, checksum, sql, request, session_id,
               down_sql, down_valid, duration_ms, applied_at
        FROM agent_schema_migrations ORDER BY version
        """
    ).fetchall()
    return {
        str(r[0]): LedgerRow(
            version=str(r[0]), name=str(r[1]), checksum=str(r[2]), sql=str(r[3]),
            request=r[4] and str(r[4]), session_id=r[5] and str(r[5]),
            down_sql=r[6] and str(r[6]),
            down_valid=None if r[7] is None else bool(r[7]),
            duration_ms=None if r[8] is None else int(r[8]), applied_at=r[9],
        )
        for r in rows
    }
