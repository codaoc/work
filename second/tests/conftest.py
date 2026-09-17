"""pytest 共享夹具。

集成测试依赖真实 PostgreSQL：默认连 postgresql://pgagent:pgagent_pass@127.0.0.1/pgagent_test，
可用环境变量 PGAGENT_TEST_URL 覆盖；不可达时自动跳过。
"""

from __future__ import annotations

import os
import socket

import pytest

TEST_URL = os.environ.get(
    "PGAGENT_TEST_URL", "postgresql://pgagent:pgagent_pass@127.0.0.1:5432/pgagent_test"
)


def _pg_reachable(url: str) -> bool:
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(url)
    host = info.get("host") or "/var/run/postgresql"
    port = int(info.get("port") or 5432)
    if host.startswith("/"):
        return os.path.exists(host)  # unix socket 目录
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


HAS_PG = _pg_reachable(TEST_URL)
if HAS_PG:
    # 供 DatabaseConfig.resolve_url 在测试中解析
    os.environ.setdefault("PGAGENT_TEST_URL", TEST_URL)

requires_pg = pytest.mark.skipif(not HAS_PG, reason="需要可达的 PostgreSQL 测试库（PGAGENT_TEST_URL）")


@pytest.fixture()
def pg_clean():
    """一个已清空 public schema 的连接；每个测试前后都重置。"""
    import psycopg

    conn = psycopg.connect(TEST_URL, autocommit=True)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    yield conn
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.close()


@pytest.fixture()
def db(pg_clean):
    from pgagent.db.session import Database

    d = Database(TEST_URL)
    yield d
    d.close()
