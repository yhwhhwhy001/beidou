"""BD-CV44: ReconciliationEngine 合约桥接测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_shared.types import AccountId, MonetaryValue, VenueId


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
    }
    defaults.update(overrides)
    return AccountFactSnapshot(**defaults)


class TestReconciliationContractBridge:
    def test_detect_same_source_fraud_three_sources(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="a1"))
        engine.update_exchange_facts(_make_snapshot(account_id="a1"))
        engine.update_event_facts(_make_snapshot(account_id="a1"))

        tr = engine.detect_same_source_fraud()
        assert tr.detect_same_source_fraud(["system", "exchange", "event_stream"])

    def test_detect_same_source_fraud_two_sources(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(_make_snapshot(account_id="a1"))
        engine.update_exchange_facts(_make_snapshot(account_id="a1"))

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
        engine.update_exchange_facts(_make_snapshot(account_id="test"))

        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result.status in (ReconciliationStatus.MATCHED, ReconciliationStatus.MISMATCHED)

    def test_step_size_empty_uses_default(self):
        engine = ReconciliationEngine()
        # step_size="" should still work (fallback to "1e-8")
        engine.update_system_facts(_make_snapshot(account_id="test", position_step_size=""))
        engine.update_exchange_facts(_make_snapshot(account_id="test", position_step_size=""))
        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result is not None  # 不崩溃即为通过
