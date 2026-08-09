"""Failure-first PostgreSQL runtime-store contract tests.

The fake connection models transaction commit/rollback and the small SQL
surface used by :class:`PostgresPersistentStore`; it does not certify a real
PostgreSQL instance or PITR/replay behavior.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from beidou_infra.postgres_store import PostgresPersistentStore


class _Cursor:
    def __init__(self, connection: _Connection, sql: str, params: tuple[Any, ...]) -> None:
        self._connection = connection
        self._sql = sql
        self._params = params
        self.rowcount = 1

    def fetchone(self) -> Any:
        return self._connection._fetchone(self._sql, self._params)

    def fetchall(self) -> list[Any]:
        return self._connection._fetchall(self._sql, self._params)


class _Connection:
    def __init__(
        self,
        *,
        schema: str | None = "v3_runtime_records",
        migration_versions: tuple[str, ...] | None = None,
    ) -> None:
        self.schema = schema
        self.migration_versions = migration_versions or PostgresPersistentStore._required_migration_versions
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_event = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cursor:
        normalized = " ".join(sql.split())
        values = tuple(params)
        self.statements.append((normalized, values))
        cursor = _Cursor(self, normalized, values)
        if normalized.startswith("INSERT INTO v3_runtime_records"):
            record_type, record_id, payload = str(values[0]), str(values[1]), json.loads(str(values[2]))
            key = (record_type, record_id)
            immutable = "DO NOTHING" in normalized
            if immutable and key in self.records:
                cursor.rowcount = 0
            else:
                self.records[key] = payload
        elif normalized.startswith("INSERT INTO v3_runtime_events"):
            if self.fail_on_event:
                raise RuntimeError("injected runtime event failure")
            self.events.append((str(values[1]), str(values[2]), str(values[3]), json.loads(str(values[4]))))
        elif normalized.startswith("DELETE FROM v3_runtime_records"):
            key = (str(values[0]), str(values[1]))
            cursor.rowcount = int(self.records.pop(key, None) is not None)
        return cursor

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        if "to_regclass" in sql:
            return (self.schema,)
        if "FROM v3_runtime_records" in sql and "record_type=%s AND record_id=%s" in sql:
            payload = self.records.get((str(params[0]), str(params[1])))
            return (json.dumps(payload),) if payload is not None else None
        return None

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        if "FROM schema_migrations" in sql:
            requested = set(params[0]) if params else set()
            return [(version,) for version in self.migration_versions if version in requested]
        if "FROM v3_runtime_records" in sql and "WHERE record_type=%s" in sql:
            record_type = str(params[0])
            return [
                (record_id, json.dumps(payload))
                for (kind, record_id), payload in self.records.items()
                if kind == record_type
            ]
        return []

    @contextmanager
    def transaction(self) -> Iterator[_Connection]:
        records = copy.deepcopy(self.records)
        events = copy.deepcopy(self.events)
        try:
            yield self
        except Exception:
            self.records = records
            self.events = events
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def close(self) -> None:
        return None


def test_postgres_store_requires_forward_migration_without_creating_schema() -> None:
    connection = _Connection(schema=None)

    with pytest.raises(RuntimeError, match="POSTGRES_SCHEMA_NOT_READY"):
        PostgresPersistentStore(
            "postgresql://test",
            connection_factory=lambda: connection,
        )

    assert not any("CREATE TABLE" in sql for sql, _params in connection.statements)


def test_postgres_store_rejects_missing_migration_head() -> None:
    connection = _Connection(migration_versions=("001_initial_schema.up", "002_v3_fact_chain.up"))

    with pytest.raises(RuntimeError, match="POSTGRES_SCHEMA_MIGRATIONS_NOT_READY"):
        PostgresPersistentStore(
            "postgresql://test",
            connection_factory=lambda: connection,
        )


def test_postgres_store_writes_projection_and_audit_event_in_one_transaction() -> None:
    connection = _Connection()
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: connection,
    )

    store.save_position_projection("BTCUSDT", "0.1", "60000", 3, "fill-1")

    assert connection.commits == 2  # schema validation + the projection write
    assert connection.rollbacks == 0
    assert connection.events[0][0:3] == ("position_projection", "BTCUSDT", "POSITION_PROJECTION")
    assert store.restore_position_projection()[0]["source_event_id"] == "fill-1"


def test_postgres_store_rollback_removes_partial_runtime_record() -> None:
    connection = _Connection()
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: connection,
    )
    connection.fail_on_event = True

    with pytest.raises(RuntimeError, match="injected runtime event failure"):
        store.save_position_projection("BTCUSDT", "0.1", "60000", 3, "fill-1")

    assert connection.rollbacks == 1
    assert connection.records == {}
    assert connection.events == []


def test_postgres_store_immutable_fill_event_is_replay_idempotent() -> None:
    connection = _Connection()
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: connection,
    )

    first = store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "60000", "FILLED", "t1")
    second = store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "60000", "FILLED", "t1")

    assert first is True
    assert second is False
    assert len(connection.events) == 1
