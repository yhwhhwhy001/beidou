from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from beidou_core.store import PersistentStore
from beidou_safety.execution.ledger import (
    AccountType,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, VenueId


def _posting(posting_id: str, side: PostingSide) -> Posting:
    return Posting(
        posting_id=posting_id,
        account_id=AccountId("account"),
        account_type=AccountType.CASH,
        venue_id=VenueId("BINANCE"),
        instrument_id=InstrumentId("BTCUSDT"),
        amount=MonetaryValue(amount="10"),
        side=side,
        description="posting",
    )


def _transaction(source: str = "fill-1") -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id="tx-1",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id=source,
        postings=(
            _posting("p1", PostingSide.DEBIT),
            _posting("p2", PostingSide.CREDIT),
        ),
        correlation_id="corr",
        timestamp=datetime(2026, 8, 12, tzinfo=timezone.utc),
        metadata={"strategy": "test"},
    )


def _money(amount: str) -> SimpleNamespace:
    return SimpleNamespace(amount=Decimal(amount), currency="USDT", decimals=8)


def _snapshot(*, timestamp: datetime | None = None) -> SimpleNamespace:
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


def test_core_store_singleton_binding_and_ledger_recovery(tmp_path) -> None:
    PersistentStore._instance = None
    first = PersistentStore.get_instance(str(tmp_path / "one.db"))
    assert PersistentStore.get_instance(str(tmp_path / "one.db")) is first
    with pytest.raises(RuntimeError, match="already bound"):
        PersistentStore.get_instance(str(tmp_path / "two.db"))

    first.save_ledger_entry("entry", "account", "BINANCE", "BTCUSDT", "10", "2", "trade", "corr", "t")
    assert first.restore_ledger_entries()[0]["entry_id"] == "entry"
    assert first.get_account_balance("account", "BINANCE") == 8.0
    assert first.get_account_balance("missing", "BINANCE") == 0.0

    transaction = _transaction()
    first.save_ledger_transaction(transaction)
    first._get_conn().execute("DELETE FROM ledger_postings WHERE posting_id='p2'")
    first._get_conn().commit()
    first.save_ledger_transaction(transaction)
    assert len(first.restore_ledger_transactions()[0]["postings"]) == 2

    first._get_conn().execute("UPDATE ledger_transactions SET source_event_id='other' WHERE transaction_id='tx-1'")
    first._get_conn().commit()
    with pytest.raises(RuntimeError, match="different source_event_id"):
        first.save_ledger_transaction(transaction)
    first.close()
    PersistentStore._instance = None


def test_core_store_reconciliation_user_stream_and_fill_identity(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "facts.db"))
    assert store.restore_latest_reconciliation_snapshot("missing", "BINANCE", "SYSTEM") is None
    snapshot = _snapshot()
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", snapshot)
    latest = store.restore_latest_reconciliation_snapshot("account", "BINANCE", "SYSTEM")
    assert latest is not None and latest["positions"] == {"BTCUSDT": "0.1"}
    result = SimpleNamespace(
        system_facts=None,
        status=SimpleNamespace(value="MATCHED"),
        matched=True,
        differences=[],
        checked_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    store.save_reconciliation_result("result", result)
    assert store.restore_user_stream_projection("account", "BINANCE") is None
    store.save_user_stream_projection(snapshot, last_sequence=5)
    projection = store.restore_user_stream_projection("account", "BINANCE")
    assert projection is not None and projection["last_sequence"] == 5

    assert store.get_user_stream_event("missing") is None
    update = _user_update()
    assert store.save_user_stream_event(update, continuity_status="CONTIGUOUS")
    assert not store.save_user_stream_event(update, continuity_status="CONTIGUOUS")
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_user_stream_event(_user_update({"e": "DIFFERENT"}), continuity_status="CONTIGUOUS")
    store.mark_user_stream_event_applied("event-1", "applied")
    store.mark_user_stream_event_applied("event-1", "again")
    assert store.get_user_stream_event("event-1")["raw_event"] == {"e": "ORDER_TRADE_UPDATE"}
    assert len(store.restore_user_stream_events(applied_only=True)) == 1
    store._get_conn().execute("UPDATE user_stream_events SET applied_state='BROKEN' WHERE event_id='event-1'")
    store._get_conn().commit()
    with pytest.raises(RuntimeError, match="unexpected state"):
        store.mark_user_stream_event_applied("event-1")
    with pytest.raises(RuntimeError, match="disappeared"):
        store.mark_user_stream_event_applied("missing")

    assert store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "60000", "FILLED", "t1")
    assert not store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "60000", "FILLED", "t1")
    store.mark_fill_event_committed("fill-1", "committed")
    store.mark_fill_event_committed("fill-1", "again")
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.2", "0.2", "60000", "FILLED", "t1")
    assert store.restore_fill_events()[0]["processing_state"] == "COMMITTED"
    store._get_conn().execute("UPDATE fill_events SET processing_state='BROKEN' WHERE fill_event_id='fill-1'")
    store._get_conn().commit()
    with pytest.raises(RuntimeError, match="unexpected state"):
        store.mark_fill_event_committed("fill-1")
    with pytest.raises(RuntimeError, match="disappeared"):
        store.mark_fill_event_committed("missing")
    store.close()


def test_core_store_opening_risk_position_order_and_protection(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "runtime.db"))
    assert store.restore_account_opening_projection("missing", "BINANCE") is None
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

    conn = store._get_conn()
    conn.execute(
        "CREATE TRIGGER fail_opening BEFORE INSERT ON account_opening_projections "
        "BEGIN SELECT RAISE(ABORT, 'opening failure'); END"
    )
    with pytest.raises(Exception, match="opening failure"):
        store.save_account_opening_projection(
            "other", "other", "BINANCE", "1", "USDT", 8, {}, [], "t", "REST", "1", "hash", "approval"
        )

    store.save_strategy_risk_state("strategy", {"status": "NORMAL"})
    assert store.restore_strategy_risk_states()["strategy"]["status"] == "NORMAL"
    store.save_position_projection("BTCUSDT", "0.1", "60000", 2, "fill-1", "t")
    assert store.restore_position_projection()[0]["position_generation"] == 2

    store.save_order_state("order", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    created_at = store.restore_order_states()[0]["created_at"]
    store.save_order_state("order", "BTCUSDT", "BUY", "LIMIT", "1", "1", "PARTIALLY_FILLED")
    assert store.restore_order_states()[0]["created_at"] == created_at
    assert store.get_active_orders()[0]["status"] == "PARTIALLY_FILLED"

    store.save_protection("p1", "position", "BTCUSDT", "SELL", "59000", None, "0.1", "STOP", "ACTIVE")
    store.remove_protection("position")
    assert store.restore_protections() == []
    store.close()


def test_core_store_order_state_never_regresses_from_terminal_or_partial_fill(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "order-state.db"))
    # 竞态回归:成交先持久化为 FILLED 后,迟到的下单响应(status=NEW)
    # 不得把终态回写成 NEW(实测 14:07 XRP/DOGE/ATOM 三单因此恒
    # MISMATCH → recon BLOCKED → 引擎停摆)。
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "1", "1", "FILLED", "1", "60000")
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["raced"]["status"] == "FILLED"
    assert rows["raced"]["filled_qty"] == "1"
    assert store.get_active_orders() == []
    # PARTIALLY_FILLED 也不得被 NEW 回写(同一竞态的中间态)
    store.save_order_state("partial-raced", "BTCUSDT", "BUY", "LIMIT", "1", "1", "PARTIALLY_FILLED", "0.4")
    store.save_order_state("partial-raced", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["partial-raced"]["status"] == "PARTIALLY_FILLED"
    assert rows["partial-raced"]["filled_qty"] == "0.4"
    # UNKNOWN 歧义标记不得抹掉已证实的终态 FILLED
    store.save_order_state("raced", "BTCUSDT", "BUY", "LIMIT", "1", "1", "UNKNOWN")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["raced"]["status"] == "FILLED"
    # 正常生命周期推进不受守卫影响
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "1", "1", "PARTIALLY_FILLED", "0.5")
    store.save_order_state("normal", "BTCUSDT", "BUY", "LIMIT", "1", "1", "FILLED", "1", "60000")
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["normal"]["status"] == "FILLED"
    store.close()


def test_core_store_operational_views_and_fail_closed_maintenance(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "ops.db"))
    for sequence in range(12):
        store.save_checkpoint(f"cp-{sequence}", "engine", {"sequence": sequence}, sequence)
    checkpoints = store.restore_checkpoints("engine")
    assert len(checkpoints) == 10 and checkpoints[0]["state_snapshot"] == {"sequence": 11}

    store.save_trading_pool_event({"instrument_id": "BTCUSDT", "status": "ACTIVE", "score": 0.8})
    store.save_trading_pool_event({"instrument_id": "BTCUSDT", "status": "PAUSED", "score": 0.2})
    assert store.restore_trading_pool_state()[0]["status"] == "PAUSED"

    store.save_report("daily", "DAILY", "Daily", "hash", "PASS", "{}")
    store.save_report("weekly", "WEEKLY", "Weekly", "hash", "PASS", "{}")
    assert len(store.get_reports()) == 2
    assert [row["report_id"] for row in store.get_reports("DAILY")] == ["daily"]
    store.save_market_snapshot("BTCUSDT", 60000, 59999, 60001, 0.3, 1000)
    assert len(store.get_recent_snapshots("BTCUSDT", 1)) == 1

    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    store.save_order_state("stale", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    store.save_order_state("recent", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    store.save_order_state("partial", "BTCUSDT", "BUY", "LIMIT", "1", "1", "PARTIALLY_FILLED")
    store.save_order_state("bad-date", "BTCUSDT", "BUY", "LIMIT", "1", "1", "NEW")
    conn = store._get_conn()
    conn.execute("UPDATE order_states SET updated_at=? WHERE order_id='stale'", (old,))
    conn.execute("UPDATE order_states SET updated_at='bad' WHERE order_id='bad-date'")
    conn.commit()
    assert store.clean_stale_new_orders(1) == 0
    assert store.clean_stale_new_orders(1, venue_terminal_statuses={"stale": "UNKNOWN"}) == 0
    assert store.clean_stale_new_orders(1, venue_terminal_statuses={"stale": "CANCELED"}) == 1
    rows = {row["order_id"]: row for row in store.restore_order_states()}
    assert rows["stale"]["status"] == "CANCELED"

    conn.execute("UPDATE market_snapshots SET timestamp=?", (old,))
    conn.execute("UPDATE reports SET generated_at=?", (old,))
    conn.commit()
    assert store.cleanup_old_data(retention_days=1) == 2
    assert [row["report_id"] for row in store.get_reports()] == ["weekly"]

    with store as entered:
        assert entered is store
    assert store._local.conn is None
    store.__exit__(None, None, None)


def test_core_store_close_suppresses_driver_failure(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "close.db"))
    store.close()

    class BrokenConnection:
        def close(self) -> None:
            raise RuntimeError("close failed")

    store._local.conn = BrokenConnection()
    store.close()
    assert store._local.conn is None


def test_sqlite_reconciliation_snapshot_persists_order_detail(tmp_path) -> None:
    """M16-F01: SQLite 版快照持久化参数级明细(与 PG 版 M13-R2 镜像对称)。"""
    store = PersistentStore(str(tmp_path / "detail.db"))
    snapshot = _snapshot()
    snapshot.open_orders_detail = {"order-1": {"symbol": "BTCUSDT", "qty": "0.1", "price": "50000"}}
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", snapshot)
    latest = store.restore_latest_reconciliation_snapshot("account", "BINANCE", "SYSTEM")
    assert latest is not None
    assert latest["open_orders_detail"] == {"order-1": {"symbol": "BTCUSDT", "qty": "0.1", "price": "50000"}}


def test_sqlite_reconciliation_snapshot_without_detail_defaults_empty(tmp_path) -> None:
    """旧快照/无明细快照恢复时 detail 缺省空 dict(向后兼容)。"""
    store = PersistentStore(str(tmp_path / "nodetail.db"))
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", _snapshot())
    latest = store.restore_latest_reconciliation_snapshot("account", "BINANCE", "SYSTEM")
    assert latest is not None
    assert latest["open_orders_detail"] == {}
