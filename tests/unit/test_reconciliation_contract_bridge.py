"""BD-CV44: ReconciliationEngine 合约桥接测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId


def _make_snapshot(account_id: str = "test", venue_id: str = "BINANCE", **overrides) -> AccountFactSnapshot:
    defaults: dict = {
        "account_id": AccountId(account_id),
        "venue_id": VenueId(venue_id),
        "timestamp": datetime.now(timezone.utc),
        "balance": MonetaryValue(amount="10000"),
        "positions": {},
        "open_orders": [],
        "complete": True,
        "position_step_size": "0.001",
        "source": "SYSTEM",
        "fact_version": "v1",
    }
    defaults.update(overrides)
    return AccountFactSnapshot(**defaults)


class TestReconciliationContractBridge:
    def test_detect_same_source_fraud_three_sources(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="a1"))
        engine.update_exchange_facts(_make_snapshot(account_id="a1", source="EXCHANGE"))
        engine.update_event_facts(_make_snapshot(account_id="a1", source="EVENT_STREAM"))

        tr = engine.detect_same_source_fraud()
        assert tr.detect_same_source_fraud(["system", "exchange", "event_stream"])

    def test_detect_same_source_fraud_two_sources(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="a1"))
        engine.update_exchange_facts(_make_snapshot(account_id="a1", source="EXCHANGE"))

        tr = engine.detect_same_source_fraud()
        assert not tr.detect_same_source_fraud(["system", "exchange"])

    def test_detect_same_source_fraud_one_source(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="a1"))

        tr = engine.detect_same_source_fraud()
        assert not tr.detect_same_source_fraud(["system"])

    def test_reconcile_both_sides_present(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="test"))
        engine.update_exchange_facts(_make_snapshot(account_id="test", source="EXCHANGE"))

        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result.status in (ReconciliationStatus.MATCHED, ReconciliationStatus.MISMATCHED)

    def test_step_size_empty_is_not_verifiable(self):
        engine = ReconciliationEngine()
        positions = {InstrumentId("BTCUSDT"): Quantity(amount="1")}
        engine.update_system_facts(_make_snapshot(account_id="test", position_step_size="", positions=positions))
        engine.update_exchange_facts(
            _make_snapshot(account_id="test", position_step_size="", positions=positions, source="EXCHANGE")
        )
        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result.status is ReconciliationStatus.INCOMPLETE
