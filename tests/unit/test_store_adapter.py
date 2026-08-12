from __future__ import annotations

import sqlite3
import sys
from types import SimpleNamespace

import pytest

from beidou_infra.store_adapter import PostgresBackend, SQLiteBackend, create_backend


class _Result:
    def __init__(self, rows=None) -> None:
        self._rows = rows or []

    def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        self.connection.transactions += 1
        if self.connection.fail_transaction:
            raise RuntimeError("transaction failed")
        return self

    def __exit__(self, *_args):
        return False


class _Connection:
    def __init__(
        self,
        *,
        rows=None,
        fail_execute: bool = False,
        fail_transaction: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.rows = rows or []
        self.fail_execute = fail_execute
        self.fail_transaction = fail_transaction
        self.fail_close = fail_close
        self.statements = []
        self.transactions = 0
        self.closed = False

    def execute(self, sql, params=()):
        if self.fail_execute:
            raise RuntimeError("execute failed")
        self.statements.append((sql, params))
        return _Result(self.rows)

    def transaction(self):
        return _Transaction(self)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def _create_sqlite_event_store(backend: SQLiteBackend) -> None:
    backend.execute(
        """
        CREATE TABLE event_store (
            id INTEGER PRIMARY KEY,
            stream_id TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            metadata TEXT NOT NULL,
            correlation_id TEXT,
            checksum TEXT NOT NULL,
            UNIQUE(stream_id, sequence)
        )
        """
    )


def test_sqlite_backend_round_trip_failure_paths_and_reconnect() -> None:
    backend = SQLiteBackend()
    assert not backend.health_check()
    assert not backend.append_event("stream", "ORDER", 1, "CREATED", {})
    with pytest.raises(RuntimeError, match="not connected"):
        backend.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="not connected"):
        backend.get_events("stream")

    assert backend.connect()
    first_connection = backend._conn
    _create_sqlite_event_store(backend)
    assert backend.execute("SELECT 1").fetchone() == (1,)
    assert backend.append_event(
        "stream",
        "ORDER",
        1,
        "CREATED",
        {"id": "order-1"},
        {"source": "test"},
        "corr-1",
    )
    assert not backend.append_event("stream", "ORDER", 1, "DUPLICATE", {})
    assert not backend.append_event("stream", "ORDER", 2, "BROKEN", {}, {"bad": object()})
    assert backend.get_events("stream") == [
        {
            "stream_id": "stream",
            "sequence": 1,
            "event_type": "CREATED",
            "payload": {"id": "order-1"},
        }
    ]
    assert backend.health_check()

    assert backend.connect()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first_connection.execute("SELECT 1")
    backend.close()
    backend.close()
    assert not backend.health_check()


def test_sqlite_connect_and_close_fail_closed(monkeypatch) -> None:
    connection = _Connection(fail_execute=True, fail_close=True)
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)
    backend = SQLiteBackend()
    assert not backend.connect("broken.sqlite")
    assert connection.closed
    assert backend._conn is None

    backend._conn = _Connection(fail_execute=True)
    assert not backend.health_check()
    backend._conn = _Connection(fail_close=True)
    with pytest.raises(RuntimeError, match="close failed"):
        backend.close()
    assert backend._conn is None


def test_postgres_backend_contract_and_failure_paths(monkeypatch) -> None:
    connection = _Connection(rows=[("stream-1", 1, "CREATED", '{"id": "order-1"}')])
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *_args, **_kwargs: connection))
    backend = PostgresBackend("postgresql://test")

    assert backend.connect()
    assert backend.health_check()
    assert backend.execute("SELECT 2").fetchall() == connection.rows
    assert backend.append_event(
        "stream-1",
        "ORDER",
        1,
        "CREATED",
        {"id": "order-1"},
        correlation_id="corr-1",
    )
    assert backend.get_events("stream-1") == [
        {
            "stream_id": "stream-1",
            "sequence": 1,
            "event_type": "CREATED",
            "payload": {"id": "order-1"},
        }
    ]
    assert connection.transactions == 1
    assert any("INSERT INTO event_store" in sql for sql, _params in connection.statements)
    backend.close()
    assert connection.closed
    assert not backend.health_check()
    assert not backend.append_event("stream", "ORDER", 1, "CREATED", {})
    with pytest.raises(RuntimeError, match="not connected"):
        backend.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="not connected"):
        backend.get_events("stream")


def test_postgres_connect_health_append_and_close_fail_closed(monkeypatch) -> None:
    assert not PostgresBackend().connect()

    health_failure = _Connection(fail_execute=True)
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: health_failure),
    )
    backend = PostgresBackend()
    assert not backend.connect("postgresql://broken")
    assert health_failure.closed
    assert backend._conn is None

    close_failure = _Connection(fail_execute=True, fail_close=True)
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: close_failure),
    )
    assert not backend.connect("postgresql://broken-close")
    assert close_failure.closed
    assert backend._conn is None

    backend._conn = _Connection(fail_transaction=True)
    assert not backend.append_event("stream", "ORDER", 1, "CREATED", {})
    backend._conn = _Connection()
    assert not backend.append_event("stream", "ORDER", 1, "CREATED", {}, {"bad": object()})
    backend._conn = _Connection(fail_execute=True)
    assert not backend.health_check()
    backend._conn = _Connection(fail_close=True)
    with pytest.raises(RuntimeError, match="close failed"):
        backend.close()
    assert backend._conn is None


def test_create_backend_rejects_unknown_modes_and_requires_database_url(monkeypatch) -> None:
    assert isinstance(create_backend(), SQLiteBackend)
    assert isinstance(create_backend(" PAPER "), SQLiteBackend)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL required"):
        create_backend("testnet")
    with pytest.raises(ValueError, match="unsupported storage mode"):
        create_backend("prodution")

    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    testnet = create_backend("TESTNET")
    production = create_backend("production")
    assert isinstance(testnet, PostgresBackend) and testnet._dsn == "postgresql://configured"
    assert isinstance(production, PostgresBackend) and production._dsn == "postgresql://configured"
