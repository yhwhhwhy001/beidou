
"""PKG-21~27: Execution Chain 测试。Intent、Executor、Ledger、Reconciliation。"""
from beidou_safety.execution.intent import IntentOutbox
from beidou_safety.execution import OrderIntent, ExecutorState
from beidou_safety.executor_impl import LeaseManager, FencingProtection
from beidou_safety.execution.order_state import (
    OrderStateTracker, OrderStatus, OrderEvent, UnknownRecoveryHandler,
)
from beidou_safety.execution.ledger import JournalEntry, ImmutableLedger
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot, ReconciliationEngine,
)
from beidou_shared.types import (
    AccountId, AccountRef, CorrelationId, InstrumentId, MonetaryValue,
    OrderId, OrderSide, OrderStatus, OrderType, Quantity, VenueId, ResultStatus,
)
from beidou_safety.execution.conditional import (
    EmergencyFlattenPolicy, ConditionalOrder, PositionManager,
)

class TestIntentOutbox:
    def test_commit_and_send(self):
        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-001", account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"), side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"), idempotency_key="idem-001",
        )
        ob.commit(intent)
        assert ob.pending_count() == 1

    def test_duplicate_rejected(self):
        import pytest
        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-001", account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"), side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"), idempotency_key="idem-001",
        )
        ob.commit(intent)
        with pytest.raises(ValueError, match="Duplicate"):
            ob.commit(intent)

class TestLeaseManager:
    def test_acquire_release(self):
        lm = LeaseManager(lease_timeout=1.0)
        assert lm.acquire("executor-1")
        assert not lm.acquire("executor-1")
        lm.release("executor-1")
        assert lm.acquire("executor-1")

class TestFencing:
    def test_fenced_executor_blocked(self):
        fp = FencingProtection()
        fp.fence("executor-old")
        assert fp.check("executor-old", 1) == ResultStatus.ERROR

    def test_active_executor_allowed(self):
        fp = FencingProtection()
        fp.promote("executor-1", 1)
        assert fp.check("executor-1", 1) == ResultStatus.SUCCESS

    def test_old_generation_blocked(self):
        fp = FencingProtection()
        fp.promote("executor-1", 5)
        assert fp.check("executor-1", 3) == ResultStatus.ERROR

class TestOrderStateMachine:
    def test_normal_flow(self):
        ts = OrderStateTracker(order_id=OrderId("order-001"))
        assert ts.apply(OrderEvent.ACKED)
        assert ts.apply(OrderEvent.PARTIALLY_FILLED)
        assert ts.apply(OrderEvent.FILLED)
        assert ts.status == OrderStatus.FILLED
        assert ts.is_terminal()

    def test_cancel_flow(self):
        ts = OrderStateTracker(order_id=OrderId("order-002"))
        ts.apply(OrderEvent.ACKED)
        ts.apply(OrderEvent.CANCEL_REQUESTED)
        assert ts.status == OrderStatus.PENDING_CANCEL
        ts.apply(OrderEvent.CANCELED)
        assert ts.status == OrderStatus.CANCELED

    def test_unknown_recovery(self):
        ts = OrderStateTracker(order_id=OrderId("order-003"))
        ts.apply(OrderEvent.UNKNOWN)
        assert ts.status == OrderStatus.UNKNOWN
        ts.apply(OrderEvent.RECOVERED)
        assert ts.status == OrderStatus.NEW

class TestImmutableLedger:
    def test_post_and_balance(self):
        ledger = ImmutableLedger()
        entry = JournalEntry(
            entry_id="je-001", account_id=AccountId("test"), venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId("BTCUSDT"), debit=MonetaryValue(amount="1000"),
            credit=MonetaryValue(amount="0"), description="Deposit",
            correlation_id=CorrelationId("corr-001"),
        )
        ledger.post(entry)
        bal = ledger.get_balance(AccountId("test"), VenueId("BINANCE"))
        assert float(bal.amount) == 1000.0

    def test_verify_attribution(self):
        ledger = ImmutableLedger()
        entry = JournalEntry(
            entry_id="je-002", account_id=AccountId("test"), venue_id=VenueId("BINANCE"),
            instrument_id=None, debit=MonetaryValue(amount="500"), credit=MonetaryValue(amount="0"),
            description="Trade PnL", correlation_id=CorrelationId("corr-trade"),
        )
        ledger.post(entry)
        assert ledger.verify_attribution(entry)

class TestReconciliation:
    def test_matched(self):
        engine = ReconciliationEngine()
        sf = AccountFactSnapshot(account_id=AccountId("test"), venue_id=VenueId("BINANCE"), balance=MonetaryValue(amount="10000"), positions={}, open_orders=[])
        engine.update_system_facts(sf)
        engine.update_exchange_facts(sf)
        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result.matched

    def test_balance_mismatch(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(AccountFactSnapshot(account_id=AccountId("test"), venue_id=VenueId("BINANCE"), balance=MonetaryValue(amount="10000"), positions={}, open_orders=[]))
        engine.update_exchange_facts(AccountFactSnapshot(account_id=AccountId("test"), venue_id=VenueId("BINANCE"), balance=MonetaryValue(amount="9999"), positions={}, open_orders=[]))
        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert not result.matched

class TestEmergencyFlatten:
    def test_policy_is_versioned(self):
        policy = EmergencyFlattenPolicy(policy_id="ef-001", version="2.0.0", signature="sig_abc")
        assert policy.post_flatten_lock

    def test_no_mechanical_add_from_funding(self):
        pm = PositionManager()
        new_size = pm.funding_rate_aware_position_size(1.0, -0.002, 0.0005)
        assert new_size < 1.0  # Should reduce, not increase
