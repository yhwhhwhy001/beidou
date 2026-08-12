"""PKG-21~27: Execution Chain 测试。Intent、Executor、Ledger、Reconciliation。"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest

from beidou_safety.execution import OrderIntent
from beidou_safety.execution.conditional import (
    EmergencyFlattenPolicy,
    PositionManager,
)
from beidou_safety.execution.intent import IntentOutbox, OutboxState
from beidou_safety.execution.ledger import (
    AccountType,
    ImmutableLedger,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_safety.execution.order_state import (
    OrderEvent,
    OrderStateTracker,
    OrderStatus,
)
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
)
from beidou_safety.executor_impl import FencingProtection, LeaseManager
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    ResultStatus,
    VenueId,
)


class TestIntentOutbox:
    def test_commit_and_send(self):
        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-001",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            idempotency_key="idem-001",
        )
        ob.commit(intent)
        assert ob.pending_count() == 1

    def test_duplicate_rejected(self):
        import pytest

        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-001",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            idempotency_key="idem-001",
        )
        ob.commit(intent)
        with pytest.raises(ValueError, match="Duplicate"):
            ob.commit(intent)

    def test_memory_unknown_intent_is_not_blindly_reclaimed(self):
        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-memory-unknown",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            idempotency_key="idem-memory-unknown",
        )
        ob.commit(intent)
        assert ob.claim("worker-a") is not None
        ob.mark_unknown(intent.intent_id, "TRANSPORT_UNKNOWN")

        assert ob.claim("worker-a") is None
        assert ob.unacked() == []
        assert ob.stats["state_counts"][OutboxState.UNKNOWN.value] == 1
        assert ob.get_unknown_intents() == [
            {
                "intent_id": "int-memory-unknown",
                "symbol": "BTCUSDT",
                "client_order_id": "",
            }
        ]

    def test_memory_unknown_positive_exchange_fact_closes_the_exact_identity(self):
        ob = IntentOutbox()
        intent = OrderIntent(
            intent_id="int-memory-found",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            client_order_id="cid-memory-found",
            idempotency_key="idem-memory-found",
        )
        ob.commit(intent)
        assert ob.claim("worker-a") is not None
        ob.mark_unknown(intent.intent_id, "TRANSPORT_UNKNOWN")

        ob.resolve_unknown(intent.intent_id, exchange_order_found=True)

        assert ob.get_unknown_intents() == []
        assert ob.pending_count() == 0
        assert intent.intent_id in ob._processed
        assert intent.idempotency_key in ob._processed
        assert ob.stats["state_counts"] == {OutboxState.ACKED.value: 1}

    def test_sqlite_outbox_survives_restart_and_preserves_unknown(self, tmp_path):
        db_path = str(tmp_path / "intent-outbox.db")
        intent = OrderIntent(
            intent_id="int-durable-001",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            client_order_id="cid-durable-001",
            idempotency_key="idem-durable-001",
            reduce_only=True,
            net_alpha_bps=12.5,
            predicted_cost_bps=4.25,
        )

        first = IntentOutbox(db_path)
        first.commit(intent)
        claimed = first.claim("worker-a", lease_seconds=60)
        assert claimed is not None
        assert claimed.intent_id == intent.intent_id
        assert claimed.net_alpha_bps == pytest.approx(12.5)
        assert claimed.predicted_cost_bps == pytest.approx(4.25)
        assert first.stats["state_counts"][OutboxState.SENDING.value] == 1

        # A new process must not blindly retry an ambiguous in-flight send.
        second = IntentOutbox(db_path)
        assert [i.intent_id for i in second.unacked()] == [intent.intent_id]
        assert second.claim("worker-b") is None
        assert second.get_unknown_intents() == [
            {
                "intent_id": "int-durable-001",
                "symbol": "BTCUSDT",
                "client_order_id": "cid-durable-001",
            }
        ]
        second.resolve_unknown(intent.intent_id, exchange_order_found=False)
        assert second.claim("worker-b") is not None
        assert second.stats["state_counts"][OutboxState.SENDING.value] == 1

        second.ack(intent.intent_id, idempotency_key=intent.idempotency_key or "")
        third = IntentOutbox(db_path)
        assert third.pending_count() == 0
        with pytest.raises(ValueError, match="Duplicate"):
            third.commit(intent)

    def test_sqlite_outbox_marks_sending_unknown_on_restart(self, tmp_path):
        db_path = str(tmp_path / "intent-outbox-unknown.db")
        intent = OrderIntent(
            intent_id="int-durable-002",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            idempotency_key="idem-durable-002",
        )

        first = IntentOutbox(db_path)
        first.commit(intent)
        assert first.claim("worker-a") is not None
        second = IntentOutbox(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute("SELECT state FROM intent_outbox WHERE intent_id=?", (intent.intent_id,)).fetchone()
        assert row[0] == OutboxState.UNKNOWN.value
        assert second.claim("worker-b") is None

    def test_sqlite_outbox_commits_approval_and_intent_together(self, tmp_path):
        db_path = str(tmp_path / "intent-approval.db")
        intent = OrderIntent(
            intent_id="int-approved-001",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            idempotency_key="idem-approved-001",
            risk_approval_id="approval-001",
            risk_approval_signature="signature-001",
            risk_proposal_hash="proposal-hash",
            risk_intent_hash="intent-hash",
            risk_account_snapshot_hash="account-hash",
            risk_snapshot_hash="risk-hash",
            risk_policy_version="policy-v1",
            risk_nonce="nonce-001",
            risk_expires_at=4102444800.0,
        )

        first = IntentOutbox(db_path)
        first.commit(intent)
        restored = first.restore_pending_approvals()

        assert len(restored) == 1
        assert restored[0]["approval_id"] == "approval-001"
        assert restored[0]["signature"] == "signature-001"
        assert restored[0]["decision"] == "APPROVED"

        first.ack(intent.intent_id, intent.idempotency_key or "")
        second = IntentOutbox(db_path)
        assert second.restore_pending_approvals() == []

        duplicate = replace(intent, intent_id="int-approved-duplicate", idempotency_key="idem-approved-duplicate")
        with pytest.raises(ValueError, match="Duplicate intent"):
            second.commit(duplicate)

    def test_persistent_duplicate_identity_probe_is_fail_closed_and_counts_reuse(self, tmp_path):
        db_path = str(tmp_path / "intent-duplicate-probe.db")
        first_intent = OrderIntent(
            intent_id="int-duplicate-001",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.1"),
            client_order_id="cid-reused",
            idempotency_key="idem-duplicate-001",
        )
        second_intent = replace(
            first_intent,
            intent_id="int-duplicate-002",
            idempotency_key="idem-duplicate-002",
        )

        outbox = IntentOutbox(db_path)
        outbox.commit(first_intent)
        assert outbox.duplicate_order_count_24h() == 0
        outbox.commit(second_intent)
        assert outbox.duplicate_order_count_24h() == 1

        # A row without a stable client identity cannot certify zero
        # duplicates; it must remain UNKNOWN.
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE intent_outbox SET payload=? WHERE intent_id=?",
                ('{"idempotency_key":"idem-malformed"}', first_intent.intent_id),
            )
        assert IntentOutbox(db_path).duplicate_order_count_24h() is None

    def test_memory_duplicate_identity_probe_is_unknown(self):
        assert IntentOutbox().duplicate_order_count_24h() is None


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
        """PKG14: RECOVERED 从 UNKNOWN 进入 PARTIALLY_FILLED（保留交易所事实）。"""
        ts = OrderStateTracker(order_id=OrderId("order-003"))
        ts.apply(OrderEvent.UNKNOWN)
        assert ts.status == OrderStatus.UNKNOWN
        ts.apply(OrderEvent.RECOVERED)
        # PKG14: RECOVERED → PARTIALLY_FILLED (preserves exchange state, not blank NEW)
        assert ts.status == OrderStatus.PARTIALLY_FILLED


class TestImmutableLedger:
    """BD-T12: 复式账本 — LedgerTransaction + Posting 不可变追加。"""

    def _make_fill_tx(self, tx_id: str, event_id: str, amount: str = "1000") -> LedgerTransaction:
        """构造标准的成交记账交易（2 个 posting: CASH debit + POSITION_COST credit）。"""
        return LedgerTransaction(
            transaction_id=tx_id,
            transaction_type=LedgerTransactionType.FILL,
            source_event_id=event_id,
            postings=(
                Posting(
                    "p1",
                    AccountId("main"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    InstrumentId("BTCUSDT"),
                    MonetaryValue(amount=amount),
                    PostingSide.DEBIT,
                    "buy BTC",
                ),
                Posting(
                    "p2",
                    AccountId("main"),
                    AccountType.POSITION_COST,
                    VenueId("BINANCE"),
                    InstrumentId("BTCUSDT"),
                    MonetaryValue(amount=amount),
                    PostingSide.CREDIT,
                    "cost basis",
                ),
            ),
            correlation_id=CorrelationId(f"corr-{tx_id}"),
        )

    def test_post_balanced_transaction(self):
        """BD-T12: 复式记账 — ≥2 Posting，借贷平衡。"""
        ledger = ImmutableLedger()
        tx = self._make_fill_tx("tx-001", "fill-001", "1000")
        assert tx.is_balanced()
        ledger.post(tx)
        assert ledger.is_balanced()
        assert ledger.transaction_count == 1

    def test_reject_unbalanced_transaction(self):
        """BD-T12: 不平衡的交易必须拒绝。"""
        unbalanced = LedgerTransaction(
            transaction_id="tx-ub",
            transaction_type=LedgerTransactionType.FILL,
            postings=(
                Posting(
                    "p1",
                    AccountId("main"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="1000"),
                    PostingSide.DEBIT,
                    "",
                ),
                Posting(
                    "p2",
                    AccountId("main"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="500"),
                    PostingSide.CREDIT,
                    "",
                ),
            ),
            source_event_id="fill-unbalanced-001",
        )
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="unbalanced"):
            ImmutableLedger().post(unbalanced)

    def test_reject_duplicate_event_id(self):
        """BD-T12: 相同 source_event_id 不可重复提交（幂等）。"""
        ledger = ImmutableLedger()
        tx = self._make_fill_tx("tx-001", "fill-001")
        ledger.post(tx)
        tx2 = self._make_fill_tx("tx-002", "fill-001")  # 相同 event_id
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="duplicate source_event_id"):
            ledger.post(tx2)

    def test_reversal_transaction(self):
        """BD-T12: 反转交易 — 创建反向分录，不原地编辑。"""
        ledger = ImmutableLedger()
        tx = self._make_fill_tx("tx-001", "fill-001", "1000")
        ledger.post(tx)

        reversal = ledger.reverse_transaction("tx-001", "tx-rev-001", "correction")
        assert reversal.is_correction
        assert reversal.reverses_transaction_id == "tx-001"
        ledger.post(reversal)

        # 反转后余额归零 (PKG20: 使用 Decimal 比较)
        bal = ledger.get_balance(AccountId("main"), VenueId("BINANCE"))
        assert abs(Decimal(bal.amount)) < Decimal("1e-12")

    def test_trial_balance(self):
        """BD-T12: 试算表 — 所有账户余额汇总。"""
        ledger = ImmutableLedger()
        ledger.post(self._make_fill_tx("tx-001", "fill-001", "1000"))
        tb = ledger.get_trial_balance()
        assert len(tb) >= 2  # CASH + POSITION_COST

    def test_rebuild_projection(self):
        """BD-T12: 从 posting 序列重建投影。"""
        ledger = ImmutableLedger()
        ledger.post(self._make_fill_tx("tx-001", "fill-001", "1000"))
        proj = ledger.rebuild_projection()
        assert proj["transaction_count"] == 1
        assert proj["is_balanced"]

    def test_fee_transaction(self):
        """BD-T12: 手续费交易 — FEE posting。"""
        fee_tx = LedgerTransaction(
            transaction_id="tx-fee-001",
            transaction_type=LedgerTransactionType.FEE,
            source_event_id="fee-001",
            postings=(
                Posting(
                    "p1",
                    AccountId("main"),
                    AccountType.FEES,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="10"),
                    PostingSide.DEBIT,
                    "trading fee",
                ),
                Posting(
                    "p2",
                    AccountId("main"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount="10"),
                    PostingSide.CREDIT,
                    "fee deduction",
                ),
            ),
        )
        ledger = ImmutableLedger()
        ledger.post(fee_tx)
        assert ledger.is_balanced()


class TestReconciliation:
    def _complete_snapshot(self, balance: str = "10000", source: str = "SYSTEM") -> AccountFactSnapshot:
        """Helper: create a complete (PKG20) snapshot."""
        return AccountFactSnapshot(
            account_id=AccountId("test"),
            venue_id=VenueId("BINANCE"),
            balance=MonetaryValue(amount=balance),
            positions={},
            open_orders=[],
            source=source,
            fact_version="v1",
            complete=True,  # PKG20: 显式标记完整
        )

    def test_matched(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(self._complete_snapshot("10000"))
        engine.update_exchange_facts(self._complete_snapshot("10000", "EXCHANGE"))
        result = engine.reconcile(AccountId("test"), VenueId("BINANCE"))
        assert result.matched

    def test_balance_mismatch(self):
        engine = ReconciliationEngine()
        engine.update_system_facts(self._complete_snapshot("10000"))
        engine.update_exchange_facts(self._complete_snapshot("9990", "EXCHANGE"))
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

    @pytest.mark.asyncio
    async def test_flatten_uses_governed_executor_not_direct_rest(self):
        class _GovernedExecutor:
            def __init__(self) -> None:
                self.calls = []

            async def enqueue_reduce_only_market(self, **kwargs):
                self.calls.append(kwargs)
                return True

            async def _api_async(self, *_args, **_kwargs):
                raise AssertionError("emergency flatten must not call direct REST")

        pm = PositionManager()
        pm.update_position(
            AccountId("test"),
            InstrumentId("BTCUSDT"),
            Quantity(amount="0.1"),
            Price(amount="100"),
        )
        executor = _GovernedExecutor()
        result = await pm.execute_flatten(
            EmergencyFlattenPolicy(policy_id="ef-001", version="2.0.0", signature="sig_abc"),
            engine=executor,
        )

        assert result["executed"] == "true"
        assert result["executed_count"] == "1"
        assert executor.calls[0]["side"] == "SELL"
        assert executor.calls[0]["policy_signature"] == "sig_abc"
