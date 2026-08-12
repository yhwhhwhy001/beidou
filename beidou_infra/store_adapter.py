"""SQLite / PostgreSQL 接口隔离 — BD-03 item 7。

Paper 模式使用 SQLite（仅本地开发/无资金），
Testnet/Production 使用 PostgreSQL。
通过统一 StorageBackend 接口隔离。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import suppress
from typing import Any


class StorageBackend(ABC):
    """统一存储后端接口。

    SQLite（Paper）和 PostgreSQL（Testnet/Production）实现此接口。
    """

    @abstractmethod
    def connect(self, connection_string: str) -> bool: ...

    @abstractmethod
    def execute(self, sql: str, params: tuple | None = None) -> Any: ...

    @abstractmethod
    def append_event(
        self,
        stream_id: str,
        aggregate_type: str,
        sequence: int,
        event_type: str,
        payload: dict,
        metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> bool: ...

    @abstractmethod
    def get_events(self, stream_id: str) -> list[dict]: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class SQLiteBackend(StorageBackend):
    """SQLite 后端 — 仅限 Paper 模式。

    使用内存或文件数据库，单连接，无并发。
    禁止用于 Testnet 或 Production。
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = None

    def connect(self, connection_string: str = "") -> bool:
        candidate = None
        try:
            import sqlite3

            self.close()
            path = connection_string or self._db_path
            candidate = sqlite3.connect(path)
            candidate.execute("PRAGMA journal_mode=WAL")
            self._conn = candidate
            return True
        except Exception:
            if candidate is not None:
                with suppress(Exception):
                    candidate.close()
            self._conn = None
            return False

    def execute(self, sql: str, params: tuple | None = None) -> Any:
        if self._conn is None:
            raise RuntimeError("SQLiteBackend not connected")
        return self._conn.execute(sql, params or ())

    def append_event(
        self,
        stream_id: str,
        aggregate_type: str,
        sequence: int,
        event_type: str,
        payload: dict,
        metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        import hashlib
        import json

        try:
            if self._conn is None:
                return False
            content = json.dumps(payload, sort_keys=True, default=str)
            checksum = hashlib.sha256(content.encode()).hexdigest()
            meta = json.dumps(metadata or {})
            self._conn.execute(
                "INSERT INTO event_store (stream_id, aggregate_type, sequence, "
                "event_type, payload, metadata, correlation_id, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stream_id, aggregate_type, sequence, event_type, content, meta, correlation_id, checksum),
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def get_events(self, stream_id: str) -> list[dict]:
        import json

        if self._conn is None:
            raise RuntimeError("SQLiteBackend not connected")
        cur = self._conn.execute(
            "SELECT stream_id, sequence, event_type, payload FROM event_store WHERE stream_id=? ORDER BY sequence",
            (stream_id,),
        )
        return [
            {"stream_id": r[0], "sequence": r[1], "event_type": r[2], "payload": json.loads(r[3])}
            for r in cur.fetchall()
        ]

    def health_check(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()


class PostgresBackend(StorageBackend):
    """PostgreSQL 后端 — Testnet/Production。

    使用单个显式连接；事务支持由调用边界控制。

    ``psycopg`` 与 ``psycopg_pool`` 是不同发行包，运行时不能假设
    ``psycopg.ConnectionPool`` 存在。正式引擎使用
    ``PostgresPersistentStore``；这个兼容接口也必须保持可用且 fail-closed。
    """

    def __init__(self, dsn: str = ""):
        self._dsn = dsn
        self._conn = None

    def connect(self, connection_string: str = "") -> bool:
        candidate = None
        try:
            import psycopg

            self.close()
            self._dsn = connection_string or self._dsn
            if not self._dsn:
                return False
            candidate = psycopg.connect(self._dsn, autocommit=True)
            candidate.execute("SELECT 1")
            self._conn = candidate
            return True
        except Exception:
            if candidate is not None:
                with suppress(Exception):
                    candidate.close()
            self._conn = None
            return False

    def execute(self, sql: str, params: tuple | None = None) -> Any:
        if self._conn is None:
            raise RuntimeError("PostgresBackend not connected")
        return self._conn.execute(sql, params or ())

    def append_event(
        self,
        stream_id: str,
        aggregate_type: str,
        sequence: int,
        event_type: str,
        payload: dict,
        metadata: dict | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        import hashlib
        import json

        try:
            if self._conn is None:
                return False
            content = json.dumps(payload, sort_keys=True, default=str)
            checksum = hashlib.sha256(content.encode()).hexdigest()
            meta = json.dumps(metadata or {})
            with self._conn.transaction():
                self._conn.execute(
                    "INSERT INTO event_store (stream_id, aggregate_type, sequence, "
                    "event_type, payload, metadata, correlation_id, checksum) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (stream_id, aggregate_type, sequence, event_type, content, meta, correlation_id, checksum),
                )
            return True
        except Exception:
            return False

    def get_events(self, stream_id: str) -> list[dict]:
        import json

        if self._conn is None:
            raise RuntimeError("PostgresBackend not connected")
        rows = self._conn.execute(
            "SELECT stream_id, sequence, event_type, payload FROM event_store WHERE stream_id=%s ORDER BY sequence",
            (stream_id,),
        ).fetchall()
        return [{"stream_id": r[0], "sequence": r[1], "event_type": r[2], "payload": json.loads(r[3])} for r in rows]

    def health_check(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        connection, self._conn = self._conn, None
        if connection is not None:
            connection.close()


def create_backend(mode: str = "paper") -> StorageBackend:
    """根据模式创建对应的存储后端。

    paper → SQLite (仅本地，无资金)
    testnet/production → PostgreSQL
    """
    normalized_mode = mode.strip().lower()
    if normalized_mode in ("testnet", "production"):
        import os

        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise ValueError("DATABASE_URL required for testnet/production mode")
        return PostgresBackend(dsn)
    if normalized_mode == "paper":
        return SQLiteBackend()
    raise ValueError(f"unsupported storage mode: {mode!r}")
