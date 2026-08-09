from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_core.store import PersistentStore
from beidou_safety.execution.ledger import (
    AccountType,
    ImmutableLedger,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId


def _facts(*, timestamp: datetime, complete: bool = True, balance: str = "100") -> AccountFactSnapshot:
    return AccountFactSnapshot(
        account_id=AccountId("acct"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount=balance),
        positions={InstrumentId("BTCUSDT"): Quantity(amount="0.25")},
        open_orders=["order-1"],
        timestamp=timestamp,
        source="test",
        fact_version="v1",
        complete=complete,
    )


def test_compare_is_pure_and_requires_fresh_complete_facts() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    matched = ReconciliationEngine.compare(_facts(timestamp=now), _facts(timestamp=now), now=now)
    assert matched.status is ReconciliationStatus.MATCHED
    assert matched.matched is True

    stale = ReconciliationEngine.compare(
        _facts(timestamp=now - timedelta(seconds=31)),
        _facts(timestamp=now),
        now=now,
    )
    assert stale.status is ReconciliationStatus.STALE
    assert stale.should_block_new_risk is True

    incomplete = ReconciliationEngine.compare(
        _facts(timestamp=now, complete=False),
        _facts(timestamp=now),
        now=now,
    )
    assert incomplete.status is ReconciliationStatus.INCOMPLETE


def test_compare_treats_missing_side_as_unknown() -> None:
    now = datetime.now(timezone.utc)
    result = ReconciliationEngine.compare(_facts(timestamp=now), None, now=now)
    assert result.status is ReconciliationStatus.ONE_SIDE_MISSING
    assert result.is_unknown is True


def test_compare_three_way_requires_event_stream_and_compares_all_pairs() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    system = _facts(timestamp=now)
    exchange = _facts(timestamp=now)
    event = _facts(timestamp=now)

    matched = ReconciliationEngine.compare_three_way(system, exchange, event, now=now)
    assert matched.status is ReconciliationStatus.MATCHED
    assert matched.event_facts is event

    missing = ReconciliationEngine.compare_three_way(system, exchange, None, now=now)
    assert missing.status is ReconciliationStatus.ONE_SIDE_MISSING
    assert missing.should_block_new_risk is True

    event.positions[InstrumentId("BTCUSDT")] = Quantity(amount="0.20")
    mismatch = ReconciliationEngine.compare_three_way(system, exchange, event, now=now)
    assert mismatch.status is ReconciliationStatus.MISMATCHED
    assert any("event_stream" in difference for difference in mismatch.differences)

    stale_event = _facts(timestamp=now - timedelta(seconds=31))
    stale = ReconciliationEngine.compare_three_way(system, exchange, stale_event, now=now)
    assert stale.status is ReconciliationStatus.STALE


def test_compare_rejects_non_finite_numeric_facts() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    invalid_balance = _facts(timestamp=now, balance="NaN")
    result = ReconciliationEngine.compare(invalid_balance, _facts(timestamp=now), now=now)
    assert result.status is ReconciliationStatus.ERROR
    assert "INVALID_BALANCE_FACT" in result.differences[0]

    invalid_position = _facts(timestamp=now)
    invalid_position.positions[InstrumentId("BTCUSDT")] = Quantity(amount="Infinity")
    result = ReconciliationEngine.compare(invalid_position, _facts(timestamp=now), now=now)
    assert result.status is ReconciliationStatus.ERROR
    assert "INVALID_POSITION_FACT" in result.differences[0]


def test_compare_does_not_collapse_long_and_short_positions() -> None:
    now = datetime.now(timezone.utc)
    long_facts = _facts(timestamp=now)
    short_facts = _facts(timestamp=now)
    short_facts.positions[InstrumentId("BTCUSDT")] = Quantity(amount="-0.25")
    result = ReconciliationEngine.compare(long_facts, short_facts, now=now)
    assert result.status is ReconciliationStatus.MISMATCHED


def test_store_reconciliation_and_fill_projections_are_idempotent(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "state.db"))
    now = datetime.now(timezone.utc)
    facts = _facts(timestamp=now)
    store.save_reconciliation_snapshot("snap-1", "SYSTEM", facts)
    assert store.restore_latest_reconciliation_snapshot("acct", "BINANCE", "SYSTEM")["snapshot_id"] == "snap-1"
    result = ReconciliationEngine.compare(facts, facts, now=now)
    store.save_reconciliation_result("result-1", result, system_snapshot_id="snap-1")
    with store._get_conn() as conn:
        persisted = conn.execute(
            "SELECT status, matched FROM reconciliation_results WHERE result_id='result-1'"
        ).fetchone()
    assert tuple(persisted) == ("MATCHED", 1)

    assert store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "100", "PARTIAL")
    assert not store.save_fill_event("fill-1", "order-1", "BTCUSDT", "BUY", "0.1", "0.1", "100", "PARTIAL")
    store.save_position_projection("BTCUSDT", "0.1", "100", 2, "fill-1")
    row = store.restore_position_projection()[0]
    assert row["signed_quantity"] == "0.1"
    assert row["position_generation"] == 2

    with store._get_conn() as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(protection_orders)").fetchall()}
    assert {"owner_id", "position_generation", "session_id", "exchange_order_id"} <= columns


def test_opening_projection_is_consumed_without_exchange_self_heal(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "opening-engine.db"))
    store.save_account_opening_projection(
        "opening-1",
        "default",
        "BINANCE",
        "1000",
        "USDT",
        8,
        {"BTCUSDT": "0.25"},
        [],
        "2026-08-09T00:00:00+00:00",
        "AUTHORIZED_READ_SNAPSHOT",
        "account-v1",
        "sha256:evidence",
        "approval-1",
    )
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._position_projection = {}
    facts = engine._build_system_reconciliation_facts()
    assert facts.complete is True
    assert facts.balance.amount == "1000"
    assert facts.positions[InstrumentId("BTCUSDT")].amount == "0.25"
    assert facts.source.endswith("AUTHORIZED_OPENING")


def test_ledger_persistence_failure_freezes_and_closes_gate() -> None:
    tx = LedgerTransaction(
        transaction_id="tx-failure",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id="fill-failure",
        postings=(
            Posting(
                "p1",
                AccountId("default"),
                AccountType.CASH,
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                MonetaryValue(amount="10"),
                PostingSide.DEBIT,
            ),
            Posting(
                "p2",
                AccountId("default"),
                AccountType.POSITION_COST,
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                MonetaryValue(amount="10"),
                PostingSide.CREDIT,
            ),
        ),
    )

    class FailingStore:
        def save_ledger_transaction(self, _tx):
            raise OSError("disk full")

    engine = object.__new__(AutonomousEngine)
    engine._ledger = ImmutableLedger()
    engine._store = FailingStore()
    control = SimpleNamespace(action=ControlAction.RESUME)
    control.get_status = lambda: control.action
    control.execute_action = lambda action: setattr(control, "action", action)
    engine._control = control
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: None)

    with pytest.raises(OSError, match="disk full"):
        engine._post_ledger_transaction(tx)
    assert engine._ledger.is_frozen is True
    assert control.action is ControlAction.NO_NEW_RISK
    assert engine._ledger.transaction_count == 0


def test_runtime_reconciliation_has_no_automatic_self_heal_call() -> None:
    import inspect

    assert "_sync_exchange_state" not in inspect.getsource(AutonomousEngine._reconcile)


def test_cumulative_fill_observations_produce_one_delta_per_event(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "fills.db"))
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._filled_quantities_by_order = {}

    first = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.1", "avgPrice": "100", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    second = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.2", "avgPrice": "101", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    duplicate = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.2", "avgPrice": "101", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    assert first[0] == 0.1
    assert second[0] == 0.1
    assert duplicate[0] == 0.0
    assert len(store.restore_fill_events()) == 2

    engine._mark_fill_retryable("order-1", first[2])
    retry = engine._consume_cumulative_fill(
        "order-1",
        "BTCUSDT",
        {"executedQty": "0.1", "avgPrice": "100", "side": "BUY"},
        status="PARTIALLY_FILLED",
    )
    assert retry[0] == 0.1


@pytest.mark.asyncio
async def test_engine_reconciliation_failure_is_read_only_and_closes_gate(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "engine-recon.db"))
    engine = object.__new__(AutonomousEngine)
    engine._store = store
    engine._position_projection = {}
    engine._recon = ReconciliationEngine()
    engine._last_reconciliation_result = None
    calls: list[tuple[str, str]] = []

    async def api(path: str, *, signed: bool = False):
        del signed
        calls.append(("GET", path))
        if path == "/fapi/v2/account":
            return {"totalWalletBalance": "100", "positions": []}, True
        return [], True

    control = SimpleNamespace(
        action=None,
        get_status=lambda: control.action,
        execute_action=lambda action: setattr(control, "action", action),
    )
    engine._api_async_safe = api
    engine._control = control
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: None)
    engine._last_account = {}

    ok = await engine._reconcile()
    assert ok is False
    assert control.action.value == "NO_NEW_RISK"
    assert calls == [("GET", "/fapi/v2/account"), ("GET", "/fapi/v1/openOrders")]
    assert engine._last_reconciliation_result.status is ReconciliationStatus.INCOMPLETE
