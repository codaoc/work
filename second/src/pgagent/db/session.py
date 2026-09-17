"""psycopg 连接与事务管理（设计文档 §4.4/§4.9）。

所有事务自动应用 SET LOCAL lock_timeout / statement_timeout，
拿不到锁快速失败，防止 DDL 锁队列放大（挑战 C-4）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from ..logging import get_logger

log = get_logger("db.session")


class Database:
    """单个数据库连接；pgagent 的所有 DB 访问都经由此对象。"""

    def __init__(self, url: str, lock_timeout: str = "5s", statement_timeout: str = "60s") -> None:
        self._url = url
        self._lock_timeout = lock_timeout
        self._statement_timeout = statement_timeout
        self._conn: psycopg.Connection | None = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            # autocommit=True：普通读查询不遗留隐式事务；conn.transaction()
            # 因此始终开启真正的顶层事务（BEGIN...COMMIT/ROLLBACK），
            # 保证 DDL 与台账写入的原子提交对其他连接可见
            self._conn = psycopg.connect(self._url, autocommit=True)
            log.debug("已建立数据库连接（autocommit）")
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            log.debug("数据库连接已关闭")

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """开一个应用了超时参数的事务，正常退出时提交。"""
        with self.conn.transaction():
            conn = self.conn
            # SET LOCAL 仅作用于当前事务，无法参数绑定，值已在配置层校验过格式
            conn.execute(f"SET LOCAL lock_timeout = '{self._lock_timeout}'")
            conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout}'")
            yield conn

    @contextmanager
    def rollback_transaction(self) -> Iterator[psycopg.Connection]:
        """开一个必然回滚的事务（试执行/preview，设计文档 §4.4 L3）。"""
        class _RollbackSentinel(Exception):
            pass

        try:
            with self.transaction() as conn:
                yield conn
                raise _RollbackSentinel()
        except _RollbackSentinel:
            log.debug("事务已回滚（preview 模式）")

    @contextmanager
    def autocommit(self) -> Iterator[psycopg.Connection]:
        """自动提交模式：用于 CREATE INDEX CONCURRENTLY 等不能进事务块的语句。"""
        prev = self.conn.autocommit
        self.conn.autocommit = True
        try:
            yield self.conn
        finally:
            self.conn.autocommit = prev

    def ping(self) -> str:
        """连通性检查，返回 server version 描述。"""
        row = self.conn.execute("SELECT version()").fetchone()
        assert row is not None
        return str(row[0])
