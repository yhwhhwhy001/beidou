"""Adversarial reconciliation branches for lineage, rules and three-way truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationResult,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _facts(
    source: str,
    *,
    fact_version: str = "v1",
    timestamp: datetime = NOW,
    complete: bool = True,
    account: str = "account",
    venue: str = "BINANCE",
    balance: str = "100",
    positions: dict[str, str] | None = None,
    orders: list[str] | None = None,
    scalar_step: str = "0.001",
    steps: dict[str, str] | None = None,
) -> AccountFactSnapshot:
    raw_positions = {"BTCUSDT": "1"} if positions is None else positions
    return AccountFactSnapshot(
        account_id=AccountId(account),
        venue_id=VenueId(venue),
        balance=MonetaryValue(amount=balance),
        positions={InstrumentId(symbol): Quantity(amount=amount) for symbol, amount in raw_positions.items()},
        open_orders=["shared"] if orders is None else orders,
        timestamp=timestamp,
        source=source,
        fact_version=fact_version,
        complete=complete,
        position_step_size=scalar_step,
        position_step_sizes={} if steps is None else steps,
    )


def test_status_safety_and_repair_strategy_are_conservative() -> None:
    engine = ReconciliationEngine()
    matched = ReconciliationResult(True)
    mismatch = ReconciliationResult(False, ReconciliationStatus.MISMATCHED)
    assert ReconciliationStatus.MATCHED.is_safe
    assert not ReconciliationStatus.ERROR.is_safe
    assert engine.repair_strategy(matched) == "NO_ACTION"
    assert engine.repair_strategy(mismatch) == "MANUAL_REPAIR_REQUIRED"


def test_engine_three_way_entrypoint_updates_last_result() -> None:
    engine = ReconciliationEngine()
    assert engine.last_result is None
    current = datetime.now(timezone.utc)
    engine.update_system_facts(_facts("SYSTEM", timestamp=current))
    engine.update_exchange_facts(_facts("EXCHANGE", timestamp=current))
    engine.update_event_facts(_facts("EVENT_STREAM", timestamp=current))
    result = engine.reconcile_three_way(AccountId("account"), VenueId("BINANCE"))
    assert result.status is ReconciliationStatus.MATCHED
    assert engine.last_result is result


def test_compare_normalizes_naive_clock_and_handles_both_missing() -> None:
    naive_now = NOW.replace(tzinfo=None)
    result = ReconciliationEngine.compare(None, None, now=naive_now)
    assert result.status is ReconciliationStatus.BOTH_SIDES_MISSING
    assert result.checked_at.tzinfo is timezone.utc


def test_missing_or_copied_fact_lineage_is_not_authority() -> None:
    incomplete = ReconciliationEngine.compare(_facts("UNKNOWN"), _facts("EXCHANGE"), now=NOW)
    assert incomplete.status is ReconciliationStatus.INCOMPLETE
    assert "INCOMPLETE_FACT_LINEAGE" in incomplete.differences[0]

    copied = ReconciliationEngine.compare(_facts("COPY"), _facts("COPY"), now=NOW)
    assert copied.status is ReconciliationStatus.ERROR
    assert "SAME_SOURCE_FRAUD_RISK" in copied.differences[0]


def test_naive_future_and_wrong_key_facts_fail_closed() -> None:
    naive = _facts("SYSTEM", timestamp=NOW.replace(tzinfo=None))
    assert ReconciliationEngine.compare(naive, _facts("EXCHANGE"), now=NOW).status is ReconciliationStatus.MATCHED

    future = _facts("SYSTEM", timestamp=NOW + timedelta(seconds=1))
    assert ReconciliationEngine.compare(future, _facts("EXCHANGE"), now=NOW).status is ReconciliationStatus.ERROR

    wrong_account = _facts("EXCHANGE", account="other")
    result = ReconciliationEngine.compare(_facts("SYSTEM"), wrong_account, now=NOW)
    assert result.status is ReconciliationStatus.ERROR
    assert "FACT_KEY_MISMATCH" in result.differences[0]


def test_balance_and_both_open_order_directions_are_reported() -> None:
    system = _facts("SYSTEM", balance="100", orders=["system-only"])
    exchange = _facts("EXCHANGE", balance="200", orders=["exchange-only"])
    result = ReconciliationEngine.compare(system, exchange, now=NOW)
    assert result.status is ReconciliationStatus.MISMATCHED
    assert any("Balance mismatch" in difference for difference in result.differences)
    order_difference = next(difference for difference in result.differences if "Open orders mismatch" in difference)
    assert "system-only" in order_difference
    assert "exchange-only" in order_difference


def test_position_rule_binding_rejects_missing_invalid_and_mismatched_steps() -> None:
    multi_positions = {"BTCUSDT": "1", "ETHUSDT": "1"}
    unbound = ReconciliationEngine.compare(
        _facts("SYSTEM", positions=multi_positions, scalar_step=""),
        _facts("EXCHANGE", positions=multi_positions, scalar_step=""),
        now=NOW,
    )
    assert unbound.status is ReconciliationStatus.INCOMPLETE
    assert "POSITION_STEP_SIZE_UNBOUND" in unbound.differences[0]

    invalid = ReconciliationEngine.compare(
        _facts("SYSTEM", scalar_step="NaN"),
        _facts("EXCHANGE", scalar_step="NaN"),
        now=NOW,
    )
    assert invalid.status is ReconciliationStatus.ERROR
    assert "INVALID_POSITION_STEP_SIZE" in invalid.differences[0]

    mismatch = ReconciliationEngine.compare(
        _facts("SYSTEM", scalar_step="0.001"),
        _facts("EXCHANGE", scalar_step="0.01"),
        now=NOW,
    )
    assert mismatch.status is ReconciliationStatus.ERROR
    assert "POSITION_STEP_SIZE_MISMATCH" in mismatch.differences[0]

    steps = {"BTCUSDT": "0.001", "ETHUSDT": "0.01"}
    matched = ReconciliationEngine.compare(
        _facts("SYSTEM", positions=multi_positions, scalar_step="", steps=steps),
        _facts("EXCHANGE", positions=multi_positions, scalar_step="", steps=steps),
        now=NOW,
    )
    assert matched.status is ReconciliationStatus.MATCHED


def test_three_way_missing_incomplete_and_copied_sources_are_typed() -> None:
    naive_now = NOW.replace(tzinfo=None)
    missing_all = ReconciliationEngine.compare_three_way(None, None, None, now=naive_now)
    assert missing_all.status is ReconciliationStatus.BOTH_SIDES_MISSING
    assert missing_all.checked_at.tzinfo is timezone.utc

    incomplete = ReconciliationEngine.compare_three_way(_facts("SYSTEM", complete=False), None, None, now=NOW)
    assert incomplete.status is ReconciliationStatus.INCOMPLETE

    copied = ReconciliationEngine.compare_three_way(
        _facts("COPY"),
        _facts("COPY"),
        _facts("EVENT_STREAM"),
        now=NOW,
    )
    assert copied.status is ReconciliationStatus.ERROR
    assert "system/exchange" in copied.differences[0]


def test_same_source_detector_requires_common_key_and_declared_independence() -> None:
    engine = ReconciliationEngine()
    engine.update_system_facts(_facts("SYSTEM", account="system-account"))
    engine.update_exchange_facts(_facts("EXCHANGE", account="exchange-account"))
    engine.update_event_facts(_facts("EVENT_STREAM", account="event-account"))
    no_common = engine.detect_same_source_fraud()
    assert not no_common.is_matched
    assert "no account" in no_common.mismatches[0]

    copied = ReconciliationEngine()
    copied.update_system_facts(_facts("COPY"))
    copied.update_exchange_facts(_facts("COPY"))
    copied.update_event_facts(_facts("UNKNOWN"))
    result = copied.detect_same_source_fraud()
    assert not result.is_matched
    assert "lacks three declared independent sources" in result.mismatches[0]
