"""Failure-first PostgreSQL runtime-store contract tests.

The fake connection models transaction commit/rollback and the small SQL
surface used by :class:`PostgresPersistentStore`; it does not certify a real
PostgreSQL instance or PITR/replay behavior.
"""

from __future__ import annotations

import copy
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from beidou_infra.postgres_store import (
    PostgresPersistentStore,
    _decode,
    _enum,
    _timestamp,
    _value,
)


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
            if len(values) >= 5:
                self.events.append((str(values[1]), str(values[2]), str(values[3]), json.loads(str(values[4]))))
            else:
                self.events.append((str(values[1]), str(values[2]), "DELETE", {}))
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


def test_store_reconnects_dead_connection_after_pg_restart() -> None:
    """引擎 fail-closed 根因修复: PG 重启后旧连接 closed → 重建。

    PG 重启后 store 持有的单连接变死连接,复用使所有事务永久抛
    "connection is closed"(对账永久失败 → trading_ready 永久 False,
    实测 14 次 segment failed)。修复后 _get_conn 检测 closed 非 0
    (psycopg3: 1=closed 2=broken)→ 关闭旧连接并重建。
    """
    conn1 = _Connection()
    conn2 = _Connection()
    factories = [conn1, conn2]
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: factories.pop(0),
    )
    assert store._get_conn() is conn1
    # PG 重启: 旧连接变死
    conn1.closed = 1
    conn1.close = lambda: None  # _discard_dead_conn 关闭调用
    assert store._get_conn() is conn2  # 检测 closed → 重建


def test_store_discards_dead_conn_on_transaction_failure() -> None:
    """事务异常路径: 事务期间连接变 closed → 异常后回收,下次重建。

    (事务开始前已 closed 的连接由 _get_conn 主动重建覆盖 —— 本用例
    覆盖 yield 期间服务端才关闭连接的竞态窗口。)
    """
    conn1 = _Connection()
    conn2 = _Connection()
    factories = [conn1, conn2]
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: factories.pop(0),
    )
    assert store._get_conn() is conn1

    class _Boom(Exception):
        pass

    conn1.close = lambda: None
    try:
        with store._transaction() as conn:
            assert conn is conn1
            conn1.closed = 1  # 事务期间服务端关闭连接
            raise _Boom("connection is closed")
    except _Boom:
        pass
    assert store._conn is None  # 事务异常 + closed → 已回收
    assert store._get_conn() is conn2  # 重建


def _store() -> tuple[PostgresPersistentStore, _Connection]:
    connection = _Connection()
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: connection,
    )
    return store, connection


def test_postgres_helpers_fail_closed_on_non_object_payloads_and_normalize_time() -> None:
    assert _value(None) is None
    assert _value(SimpleNamespace(amount=Decimal("1.25"))) == "1.25"
    assert _enum(None) is None
    assert _enum(SimpleNamespace(value="ACTIVE")) == "ACTIVE"
    assert _decode({"a": 1}) == {"a": 1}
    assert _decode('{"a": 1}') == {"a": 1}
    with pytest.raises(RuntimeError, match="PAYLOAD_NOT_OBJECT"):
        _decode("[]")
    assert "+00:00" in _timestamp(None)
    naive = datetime(2026, 8, 12, tzinfo=timezone.utc).replace(tzinfo=None)
    assert _timestamp(naive) == "2026-08-12T00:00:00+00:00"
    assert _timestamp("venue-time") == "venue-time"


def test_postgres_default_factory_and_instance_cache(monkeypatch) -> None:
    with pytest.raises(ValueError, match="DSN or connection_factory is required"):
        PostgresPersistentStore("")

    connection = _Connection()
    calls: list[tuple[str, bool]] = []

    def connect(dsn: str, *, autocommit: bool):
        calls.append((dsn, autocommit))
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    factory = PostgresPersistentStore._default_factory("postgresql://factory")
    assert factory() is connection
    assert calls == [("postgresql://factory", True)]

    PostgresPersistentStore._instances.clear()
    first = PostgresPersistentStore.get_instance("postgresql://factory")
    second = PostgresPersistentStore.get_instance("postgresql://factory")
    assert first is second
    first.close()
    PostgresPersistentStore._instances.clear()


class _FallbackConnection(_Connection):
    transaction = None

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1


def test_postgres_transaction_fallback_commits_and_rolls_back() -> None:
    connection = _FallbackConnection()
    store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=lambda: connection,
        validate_schema=False,
    )
    with store._transaction() as selected:
        assert selected is connection
    assert connection.commits == 1
    with pytest.raises(RuntimeError, match="rollback"), store._transaction():
        raise RuntimeError("rollback")
    assert connection.rollbacks == 1


def test_postgres_record_helpers_support_dict_rows_and_delete_audit() -> None:
    store, connection = _store()
    assert store._row_payload({"payload": '{"x": 1}'}) == {"x": 1}
    assert store._write_record("kind", "one", {"x": 1}, immutable=True)
    assert not store._write_record("kind", "one", {"x": 1}, immutable=True)
    store._delete_record("kind", "one")
    assert ("kind", "one") not in connection.records
    assert connection.events[-1][2] == "DELETE"

    class _DictRowsConnection(_Connection):
        def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
            if "FROM v3_runtime_records" in sql and "WHERE record_type=%s" in sql:
                return [{"record_id": "dict-id", "payload": '{"x": 2}'}]
            return super()._fetchall(sql, params)

    dict_store = PostgresPersistentStore(
        "postgresql://test",
        connection_factory=_DictRowsConnection,
        validate_schema=False,
    )
    assert dict_store._records("kind") == [{"record_id": "dict-id", "x": 2}]


def _posting(posting_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        posting_id=posting_id,
        account_id="account",
        account_type=SimpleNamespace(value="ASSET"),
        venue_id="BINANCE",
        instrument_id="BTCUSDT",
        amount=SimpleNamespace(amount=Decimal("10"), currency="USDT", decimals=8),
        side=SimpleNamespace(value="DEBIT"),
        description="posting",
    )


def _transaction(transaction_id: str, postings: list[SimpleNamespace], source: str = "fill-1") -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=transaction_id,
        source_event_id=source,
        postings=postings,
        transaction_type=SimpleNamespace(value="TRADE"),
        correlation_id="corr",
        timestamp=datetime(2026, 8, 12, tzinfo=timezone.utc),
        is_correction=False,
        reverses_transaction_id=None,
        metadata={"strategy": "test"},
    )


def test_postgres_ledger_transactions_merge_idempotently_and_reject_source_conflict() -> None:
    store, _connection = _store()
    store.save_ledger_entry("entry", "account", "BINANCE", "BTCUSDT", "10", "2", "trade", "corr", "t")
    assert store.get_account_balance("account", "BINANCE") == 8.0
    assert store.get_account_balance("other", "BINANCE") == 0.0

    store.save_ledger_transaction(_transaction("tx", [_posting("p1")]))
    store.save_ledger_transaction(_transaction("tx", [_posting("p1")]))
    store.save_ledger_transaction(_transaction("tx", [_posting("p1"), _posting("p2")]))
    restored = store.restore_ledger_transactions()
    assert [item["posting_id"] for item in restored[0]["postings"]] == ["p1", "p2"]
    with pytest.raises(RuntimeError, match="source conflict"):
        store.save_ledger_transaction(_transaction("tx", [_posting("p3")], source="fill-other"))


def _money(amount: str) -> SimpleNamespace:
    return SimpleNamespace(amount=Decimal(amount), currency="USDT", decimals=8)


def _account_snapshot(*, timestamp: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        account_id="account",
        venue_id="BINANCE",
        balance=_money("100"),
        positions={"BTCUSDT": SimpleNamespace(amount=Decimal("0.1"))},
        open_orders=["order-1"],
        margin_used=_money("10"),
        timestamp=timestamp or datetime(2026, 8, 12, tzinfo=timezone.utc),
        correlation_id="corr",
        source="REST",
        fact_version="1",
        complete=True,
    )


def test_postgres_reconciliation_and_user_projection_round_trip() -> None:
    store, _connection = _store()
    snapshot = _account_snapshot()
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", snapshot)
    older = _account_snapshot(timestamp=datetime(2026, 8, 11, tzinfo=timezone.utc))
    store.save_reconciliation_snapshot("snap-0", "SYSTEM", older)
    latest = store.restore_latest_reconciliation_snapshot("account", "BINANCE", "SYSTEM")
    assert latest is not None and latest["snapshot_id"] == "snap-1"
    assert store.restore_latest_reconciliation_snapshot("missing", "BINANCE", "SYSTEM") is None

    result = SimpleNamespace(
        system_facts=snapshot,
        status=SimpleNamespace(value="MATCHED"),
        matched=True,
        differences=[],
        checked_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    store.save_reconciliation_result("result-1", result, system_snapshot_id="snap-1")
    result.system_facts = None
    store.save_reconciliation_result("result-2", result)

    store.save_user_stream_projection(snapshot, last_sequence=5)
    projection = store.restore_user_stream_projection("account", "BINANCE")
    assert projection is not None and projection["last_sequence"] == 5


def _user_update(raw: dict | None = None) -> SimpleNamespace:
    event = SimpleNamespace(
        event_id="event-1",
        event_type="ORDER_TRADE_UPDATE",
        sequence=3,
        event_time_ms=1000,
        transaction_time_ms=999,
        raw_event=raw or {"e": "ORDER_TRADE_UPDATE"},
    )
    return SimpleNamespace(
        event=event,
        commission=_money("0.1"),
        realized_pnl=_money("1"),
        order_id="order-1",
        client_order_id="client-1",
        symbol="BTCUSDT",
        side=SimpleNamespace(value="BUY"),
        order_type=SimpleNamespace(value="MARKET"),
        order_status=SimpleNamespace(value="FILLED"),
        execution_type="TRADE",
        original_quantity=SimpleNamespace(amount=Decimal("0.1")),
        cumulative_quantity=SimpleNamespace(amount=Decimal("0.1")),
        last_quantity=SimpleNamespace(amount=Decimal("0.1")),
        last_price=SimpleNamespace(amount=Decimal("60000")),
        average_price=SimpleNamespace(amount=Decimal("60000")),
        trade_id="trade-1",
    )


def test_postgres_user_stream_event_identity_and_apply_state_machine() -> None:
    store, connection = _store()
    update = _user_update()
    assert store.save_user_stream_event(update, continuity_status="CONTIGUOUS")
    assert not store.save_user_stream_event(update, continuity_status="CONTIGUOUS")
    assert store.get_user_stream_event("event-1")["continuity_status"] == "CONTIGUOUS"
    conflicting = _user_update({"e": "DIFFERENT"})
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_user_stream_event(conflicting, continuity_status="CONTIGUOUS")

    store.mark_user_stream_event_applied("event-1", "applied")
    store.mark_user_stream_event_applied("event-1", "again")
    applied = store.restore_user_stream_events(applied_only=True)
    assert len(applied) == 1 and applied[0]["applied_at"] == "applied"
    connection.records[("user_stream_event", "event-1")]["applied_state"] = "BROKEN"
    with pytest.raises(RuntimeError, match="unexpected state"):
        store.mark_user_stream_event_applied("event-1")
    with pytest.raises(RuntimeError, match="disappeared"):
        store.mark_user_stream_event_applied("missing")


def test_postgres_fill_event_identity_and_commit_state_machine() -> None:
    store, connection = _store()
    args = ("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "60000", "FILLED", "t1")
    assert store.save_fill_event(*args)
    assert not store.save_fill_event(*args)
    store.mark_fill_event_committed("fill-1", "committed")
    assert not store.save_fill_event(*args)
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.2", "0.2", "60000", "FILLED", "t1")
    store.mark_fill_event_committed("fill-1", "again")
    assert store.restore_fill_events()[0]["processing_state"] == "COMMITTED"

    connection.records[("fill_event", "fill-1")]["processing_state"] = "BROKEN"
    with pytest.raises(RuntimeError, match="unexpected state"):
        store.mark_fill_event_committed("fill-1")
    with pytest.raises(RuntimeError, match="disappeared"):
        store.mark_fill_event_committed("missing")


def test_postgres_opening_risk_position_order_and_protection_contracts() -> None:
    store, _connection = _store()
    with pytest.raises(ValueError, match="durable provenance"):
        store.save_account_opening_projection(
            "", "account", "BINANCE", "1", "USDT", 8, {}, [], "t", "REST", "1", "h", "a"
        )
    store.save_account_opening_projection(
        "opening",
        "account",
        "BINANCE",
        "100",
        "USDT",
        8,
        {"BTCUSDT": "0"},
        [],
        "t",
        "REST",
        "1",
        "hash",
        "approval",
    )
    assert store.restore_account_opening_projection("account", "BINANCE")["projection_id"] == "opening"

    store.save_strategy_risk_state("strategy", {"status": "NORMAL"})
    assert store.restore_strategy_risk_states()["strategy"]["status"] == "NORMAL"
    store.save_position_projection("BTCUSDT", "0.1", "60000", 2, "fill-1", "t")
    assert store.restore_position_projection()[0]["position_generation"] == 2

    store.save_order_state("order-1", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "NEW")
    created_at = store.restore_order_states()[0]["created_at"]
    store.save_order_state("order-1", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "FILLED", "0.1")
    assert store.restore_order_states()[0]["created_at"] == created_at
    assert store.get_active_orders() == []

    store.save_protection("p1", "position", "BTCUSDT", "SELL", "59000", None, "0.1", "STOP", "ACTIVE")
    store.save_protection("p2", "position", "BTCUSDT", "SELL", "65000", None, "0.1", "TP", "PENDING")
    assert len(store.restore_protections()) == 2
    store.remove_protection("position")
    assert [row["status"] for row in store._records("protection")] == ["CANCELLED", "PENDING"]


def test_postgres_order_state_never_regresses_from_terminal_or_partial_fill() -> None:
    store, _connection = _store()
    # 竞态回归:成交先经 user stream 持久化为 FILLED 后,迟到的下单
    # 响应(status=NEW)不得把终态回写成 NEW(实测 14:07 XRP/DOGE/ATOM
    # 三单因此恒 MISMATCH → recon BLOCKED → 引擎停摆)。
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "FILLED", "0.1", "50000")
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "NEW")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["raced"]["status"] == "FILLED"
    assert rows["raced"]["filled_qty"] == "0.1"
    assert store.get_active_orders() == []
    # PARTIALLY_FILLED 也不得被 NEW 回写(同一竞态的中间态)
    store.save_order_state("partial-raced", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "PARTIALLY_FILLED", "0.04")
    store.save_order_state("partial-raced", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "NEW")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["partial-raced"]["status"] == "PARTIALLY_FILLED"
    assert rows["partial-raced"]["filled_qty"] == "0.04"
    # UNKNOWN 歧义标记不得抹掉已证实的终态 FILLED
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "UNKNOWN")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["raced"]["status"] == "FILLED"
    # 正常生命周期推进不受守卫影响
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "NEW")
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "PARTIALLY_FILLED", "0.05")
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "0.1", "50000", "FILLED", "0.1", "50000")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["normal"]["status"] == "FILLED"


def test_postgres_checkpoint_pool_report_market_and_maintenance() -> None:
    store, _connection = _store()
    for sequence in range(12):
        store.save_checkpoint(f"cp-{sequence}", "engine", {}, sequence)
    checkpoints = store.restore_checkpoints("engine")
    assert len(checkpoints) == 10 and checkpoints[0]["sequence_number"] == 11

    store.save_trading_pool_event({"instrument_id": "BTCUSDT", "status": "ACTIVE", "score": 0.8})
    assert store.restore_trading_pool_state()[0]["status"] == "ACTIVE"
    store.save_report("r1", "daily", "Daily", "hash", "PASS", "{}")
    store.save_report("r2", "weekly", "Weekly", "hash", "PASS", "{}")
    assert [row["report_id"] for row in store.get_reports("daily")] == ["r1"]

    store.save_market_snapshot("BTCUSDT", 60000, 59999, 60001, 0.3, 1000)
    store.save_market_snapshot("ETHUSDT", 3000, None, None, None, None)
    assert len(store.get_recent_snapshots("BTCUSDT", 1)) == 1

    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    store.save_order_state("stale-new", "OLDUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    store._records("order_state")[0]
    store._conn.records[("order_state", "stale-new")]["updated_at"] = old
    assert store.clean_stale_new_orders(1) == 0
    assert store.clean_stale_new_orders(1, venue_terminal_statuses={"stale-new": "CANCELED"}) == 1

    store.save_order_state("not-new", "BTCUSDT", "BUY", "LIMIT", "1", "1", "PARTIALLY_FILLED")
    store.save_order_state("bad-new", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    store._conn.records[("order_state", "bad-new")]["updated_at"] = "bad"
    assert store.clean_stale_new_orders(1) == 0

    store.save_order_state("outside", "OLDUSDT", "BUY", "LIMIT", "1", "1", "UNKNOWN")
    assert store.clean_orders_not_in_universe(["BTCUSDT"]) == 0
    assert store.clean_orders_not_in_universe(["BTCUSDT"], venue_terminal_statuses={"outside": "EXPIRED"}) == 1

    store.save_order_state("unknown", "BTCUSDT", "BUY", "LIMIT", "1", "1", "UNKNOWN")
    store._conn.records[("order_state", "unknown")]["updated_at"] = old
    assert store.expire_stale_unknown_orders(1) == 0
    assert store.expire_stale_unknown_orders(1, venue_terminal_statuses={"unknown": "REJECTED"}) == 1

    store.save_order_state("bad-unknown", "BTCUSDT", "BUY", "LIMIT", "1", "1", "UNKNOWN")
    store._conn.records[("order_state", "bad-unknown")]["updated_at"] = "bad"
    assert store.expire_stale_unknown_orders(1) == 0

    old_market_id = "old-market"
    old_report_id = "old-report"
    store._write_record(
        "market_snapshot",
        old_market_id,
        {"record_id": old_market_id, "symbol": "BTCUSDT", "timestamp": old},
    )
    store._write_record(
        "report",
        old_report_id,
        {"report_id": old_report_id, "generated_at": old},
    )
    store._write_record("report", "bad-date", {"report_id": "bad-date", "generated_at": "bad"})
    assert store.cleanup_old_data(retention_days=1) == 2
