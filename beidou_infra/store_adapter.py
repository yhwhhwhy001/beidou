"""SQLite / PostgreSQL 接口隔离 — BD-03 item 7。

Paper 模式使用 SQLite（仅本地开发/无资金），
Testnet/Production 使用 PostgreSQL。
通过统一 StorageBackend 接口隔离。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """统一存储后端接口。

    SQLite（Paper）和 PostgreSQL（Testnet/Production）实现此接口。
    """

    @abstractmethod
    def connect(self, connection_string: str) -> bool:
        ...

    @abstractmethod
    def execute(self, sql: str, params: tuple | None = None) -> Any:
        ...

    @abstractmethod
    def append_event(self, stream_id: str, aggregate_type: str, sequence: int,
                     event_type: str, payload: dict, metadata: dict | None = None,
                     correlation_id: str | None = None) -> bool:
        ...

    @abstractmethod
    def get_events(self, stream_id: str) -> list[dict]:
        ...

    @abstractmethod
    def health_check(self) -> bool:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class SQLiteBackend(StorageBackend):
    """SQLite 后端 — 仅限 Paper 模式。

    使用内存或文件数据库，单连接，无并发。
    禁止用于 Testnet 或 Production。
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn = None

    def connect(self, connection_string: str = "") -> bool:
        try:
            import sqlite3
            path = connection_string or self._db_path
            self._conn = sqlite3.connect(path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple | None = None) -> Any:
        if not self._conn:
            raise RuntimeError("SQLiteBackend not connected")
        return self._conn.execute(sql, params or ())

    def append_event(self, stream_id: str, aggregate_type: str, sequence: int,
                     event_type: str, payload: dict, metadata: dict | None = None,
                     correlation_id: str | None = None) -> bool:
        import json, hashlib
        content = json.dumps(payload, sort_keys=True, default=str)
        checksum = hashlib.sha256(content.encode()).hexdigest()
        meta = json.dumps(metadata or {})
        try:
            self._conn.execute(
                "INSERT INTO event_store (stream_id, aggregate_type, sequence, "
                "event_type, payload, metadata, correlation_id, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stream_id, aggregate_type, sequence, event_type,
                 content, meta, correlation_id, checksum),
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def get_events(self, stream_id: str) -> list[dict]:
        import json
        cur = self._conn.execute(
            "SELECT * FROM event_store WHERE stream_id=? ORDER BY sequence",
            (stream_id,),
        )
        return [
            {"stream_id": r[1], "sequence": r[3], "event_type": r[4],
             "payload": json.loads(r[5])}
            for r in cur.fetchall()
        ]

    def health_check(self) -> bool:
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class PostgresBackend(StorageBackend):
    """PostgreSQL 后端 — Testnet/Production。

    使用连接池，事务支持，advisory lock。
    """

    def __init__(self, dsn: str = ""):
        self._dsn = dsn
        self._pool = None

    def connect(self, connection_string: str = "") -> bool:
        try:
            import psycopg
            self._dsn = connection_string or self._dsn
            self._pool = psycopg.ConnectionPool(
                self._dsn, min_size=2, max_size=10,
            )
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple | None = None) -> Any:
        if not self._pool:
            raise RuntimeError("PostgresBackend not connected")
        with self._pool.connection() as conn:
            return conn.execute(sql, params or ())

    def append_event(self, stream_id: str, aggregate_type: str, sequence: int,
                     event_type: str, payload: dict, metadata: dict | None = None,
                     correlation_id: str | None = None) -> bool:
        import json, hashlib
        content = json.dumps(payload, sort_keys=True, default=str)
        checksum = hashlib.sha256(content.encode()).hexdigest()
        meta = json.dumps(metadata or {})
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO event_store (stream_id, aggregate_type, sequence, "
                    "event_type, payload, metadata, correlation_id, checksum) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (stream_id, aggregate_type, sequence, event_type,
                     content, meta, correlation_id, checksum),
                )
                conn.commit()
            return True
        except Exception:
            return False

    def get_events(self, stream_id: str) -> list[dict]:
        import json
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT stream_id, sequence, event_type, payload "
                "FROM event_store WHERE stream_id=%s ORDER BY sequence",
                (stream_id,),
            ).fetchall()
        return [
            {"stream_id": r[0], "sequence": r[1], "event_type": r[2],
             "payload": json.loads(r[3])}
            for r in rows
        ]

    def health_check(self) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._pool:
            self._pool.close()
            self._pool = None


def create_backend(mode: str = "paper") -> StorageBackend:
    """根据模式创建对应的存储后端。

    paper → SQLite (仅本地，无资金)
    testnet/production → PostgreSQL
    """
    if mode in ("testnet", "production"):
        import os
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise ValueError("DATABASE_URL required for testnet/production mode")
        return PostgresBackend(dsn)
    return SQLiteBackend()
