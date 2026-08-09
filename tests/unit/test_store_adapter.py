from __future__ import annotations

import sys
from types import SimpleNamespace

from beidou_infra.store_adapter import PostgresBackend


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
        return self

    def __exit__(self, *_args):
        return False


class _Connection:
    def __init__(self) -> None:
        self.statements = []
        self.transactions = 0
        self.closed = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        return _Result()

    def transaction(self):
        return _Transaction(self)

    def close(self):
        self.closed = True


def test_postgres_backend_uses_psycopg_connection_not_missing_pool_api(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *_args, **_kwargs: connection))

    backend = PostgresBackend("postgresql://test")

    assert backend.connect() is True
    assert backend.health_check() is True
    assert backend.append_event("stream-1", "ORDER", 1, "CREATED", {"id": "order-1"}) is True
    assert connection.transactions == 1
    assert any("INSERT INTO event_store" in sql for sql, _params in connection.statements)

    backend.close()
    assert connection.closed is True
    assert backend.health_check() is False


def test_postgres_backend_without_dsn_fails_closed() -> None:
    assert PostgresBackend().connect() is False
