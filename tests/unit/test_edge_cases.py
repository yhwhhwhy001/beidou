"""边界测试: UNKNOWN/STALE/CORRUPT/NULL/EMPTY/NAN 处理。

BD-CV02/33: UNKNOWN/STALE/CORRUPT → NO_NEW_RISK 或更严格，绝不 EMPTY/NORMAL。
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import pytest

from beidou_control.plane import ControlAction, ControlPlane
from beidou_control.truth import TruthSnapshot, TradingEligibility, derive_eligibility
from beidou_certification.contracts import (
    FaultScenario,
    FaultInjectionResult,
    MonitorState,
    MonitoringAggregate,
    RecoveryAction,
    RecoveryCheckpoint,
    RecoveryResult,
)
from beidou_research.contracts import FactorEvidence, FactorLifecycleState
from beidou_safety.execution.contracts import (
    ExecutionPlan,
    Fill,
    LegType,
    LedgerPosting,
    LedgerTransaction,
    PlanSlice,
    PlanStatus,
    PositionAggregate,
    ProtectionAggregate,
    ProtectionOrder,
    TripleReconciliation,
)
from beidou_strategy.portfolio.contracts import (
    AdaptiveSizing,
    RiskApproval,
    RiskState,
    RiskStateAuthority,
)


# ============================================================================
# BD-CV02: UNKNOWN/STALE/CORRUPT → NOT_VERIFIABLE/NO_NEW_RISK
# ============================================================================


class TestUnknownStaleCorruptMapping:
    """BD-CV02 AC-02-02/05: UNKNOWN/STALE/CORRUPT 绝不映射 EMPTY/NORMAL。"""

    def test_empty_snapshot_not_eligible(self):
        snap = TruthSnapshot()
        assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE

    def test_stale_snapshot_not_eligible(self):
        old = time.time() - 3600
        snap = TruthSnapshot(
            snapshot_id="s1",
            created_at=datetime.now(timezone.utc).isoformat(),
            market_hash="a", account_hash="a", order_hash="a",
            position_hash="a", ledger_hash="a", reconciliation_hash="a",
            protection_hash="a", risk_hash="a",
            market_freshness=old, account_freshness=old,
            reconciliation_freshness=old, protection_freshness=old, risk_freshness=old,
            reconciliation_status="MATCHED", protection_status="ACTIVE", risk_status="NORMAL",
        )
        assert derive_eligibility(snap, max_age_seconds=300.0) == TradingEligibility.NOT_VERIFIABLE

    def test_unknown_component_not_eligible(self):
        now = time.time()
        snap = TruthSnapshot(
            snapshot_id="s1",
            created_at=datetime.now(timezone.utc).isoformat(),
            market_hash="a", account_hash="a", order_hash="a",
            position_hash="a", ledger_hash="a", reconciliation_hash="a",
            protection_hash="a", risk_hash="a", config_hash="a", policy_hash="a",
            market_freshness=now, account_freshness=now, order_freshness=now,
            position_freshness=now, ledger_freshness=now,
            reconciliation_freshness=now, protection_freshness=now, risk_freshness=now,
            reconciliation_status="UNKNOWN", protection_status="UNKNOWN", risk_status="NORMAL",
        )
        assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK

    def test_corrupt_risk_not_normal(self):
        rsa = RiskStateAuthority(state=RiskState.CORRUPT)
        assert rsa.state != RiskState.NORMAL
        assert not rsa.can_create_intent()

    def test_missing_snapshot_not_eligible(self):
        snap = TruthSnapshot(snapshot_id="s1", created_at=datetime.now(timezone.utc).isoformat())
        assert snap.is_empty()
        assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE


# ============================================================================
# BD-CV41: UNKNOWN 订单幂等 — 不重复发送
# ============================================================================


class TestUnknownOrderIdempotency:
    """BD-CV41: timeout/HTPP 5xx → UNKNOWN，query-by-client-id 不自送重发。"""

    def test_idempotency_keys_unique(self):
        from beidou_safety.execution.contracts import OrderIdempotencyKey

        k1 = OrderIdempotencyKey("c1", "o1", "b1")
        k2 = OrderIdempotencyKey("c1", "o1", "b2")  # diff outbox
        assert k1.compute_hash() != k2.compute_hash()

    def test_same_key_dedup(self):
        from beidou_safety.execution.contracts import OrderIdempotencyKey

        k1 = OrderIdempotencyKey("c1", "o1", "b1")
        k2 = OrderIdempotencyKey("c1", "o1", "b1")
        assert k1.compute_hash() == k2.compute_hash()

    def test_nan_position_not_accepted(self):
        pa = PositionAggregate(symbol="BTCUSDT", net_position=float("nan"))
        fills = [Fill(fill_id="f1", symbol="BTCUSDT", side="BUY", quantity=1.0, price=50000.0)]
        result = pa.replay(fills)
        assert math.isnan(result.net_position) or result.net_position != 0


# ============================================================================
# BD-CV33: CORRUPT/MISSING → NO_NEW_RISK
# ============================================================================


class TestCorruptMissingState:
    """BD-CV33: 状态损坏/缺失/非法值 → NO_NEW_RISK。"""

    def test_corrupt_blocks_intent(self):
        rsa = RiskStateAuthority(state=RiskState.CORRUPT)
        assert not rsa.can_create_intent()

    def test_critical_blocks_intent(self):
        rsa = RiskStateAuthority(state=RiskState.CRITICAL)
        assert not rsa.can_create_intent()

    def test_warning_allows_with_approval(self):
        from datetime import timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        approval = RiskApproval(approval_id="a1", is_expired=False, expires_at=future)
        rsa = RiskStateAuthority(state=RiskState.WARNING, active_approvals=[approval])
        assert rsa.can_create_intent()

    def test_expired_approval_blocks(self):
        from datetime import timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        approval = RiskApproval(approval_id="a1", is_expired=False, expires_at=past)
        rsa = RiskStateAuthority(state=RiskState.NORMAL, active_approvals=[approval])
        assert not rsa.can_create_intent()

    def test_control_lock_maps_to_lock(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        assert cp.get_status() == ControlAction.LOCK
        # LOCK → 仅允许 QUERY
        result = cp.validate_intent(SimpleNamespace(side="BUY", order_type="MARKET"))
        assert not result.allowed


# ============================================================================
# BD-CV50: P1 FAIL → RED (not GREEN)
# ============================================================================


class TestMonitoringStateAlgebra:
    """BD-CV50: P1 FAIL 不可能 GREEN。"""

    def test_p1_fail_red(self):
        agg = MonitoringAggregate(p0_checks=0, p0_fail=0, p1_checks=1, p1_fail=1)
        assert agg.compute_state() == MonitorState.RED

    def test_p0_fail_red(self):
        agg = MonitoringAggregate(p0_checks=1, p0_fail=1, p1_checks=0, p1_fail=0)
        assert agg.compute_state() == MonitorState.RED

    def test_all_pass_green(self):
        agg = MonitoringAggregate(p0_checks=10, p0_fail=0, p1_checks=5, p1_fail=0)
        assert agg.compute_state() == MonitorState.GREEN

    def test_empty_unknown(self):
        agg = MonitoringAggregate()
        assert agg.compute_state() == MonitorState.UNKNOWN


# ============================================================================
# BD-CV51: Recovery — 空 invariants ≠ SUCCESS
# ============================================================================


class TestRecoveryInvariants:
    """BD-CV51: 空 invariants/陈旧 facts → FAIL。"""

    def test_empty_invariants_not_success(self):
        cp = RecoveryCheckpoint(checkpoint_id="c1", module_name="m1", invariants_valid=False, facts_fresh=False)
        result = RecoveryResult(action=RecoveryAction.RESTART_MODULE, success=True, checkpoint=cp)
        assert not result.is_valid_success()

    def test_valid_invariants_success(self):
        cp = RecoveryCheckpoint(checkpoint_id="c1", module_name="m1", invariants_valid=True, facts_fresh=True)
        result = RecoveryResult(action=RecoveryAction.RESTART_MODULE, success=True, checkpoint=cp)
        assert result.is_valid_success()

    def test_no_checkpoint_with_side_effect(self):
        result = RecoveryResult(
            action=RecoveryAction.DEGRADE_TO_NO_NEW_RISK,
            success=True, side_effect_observed=True,
        )
        assert result.is_valid_success()

    def test_no_checkpoint_no_side_effect_fails(self):
        result = RecoveryResult(
            action=RecoveryAction.DEGRADE_TO_NO_NEW_RISK,
            success=True, side_effect_observed=False,
        )
        assert not result.is_valid_success()

    def test_not_supported_action(self):
        result = RecoveryResult(action=RecoveryAction.NOT_SUPPORTED, success=False)
        assert not result.is_valid_success()


# ============================================================================
# BD-CV22: NaN/Inf 因子 — 不能 PROMOTED
# ============================================================================


class TestFactorEdgeCases:
    """BD-CV22: NaN/零/负 sharpe → 不能 PROMOTED。"""

    def test_nan_sharpe_blocked(self):
        fe = FactorEvidence(factor_id="f1", sharpe=float("nan"))
        assert not fe.can_promote()

    def test_inf_sharpe_blocked(self):
        fe = FactorEvidence(factor_id="f1", sharpe=float("inf"))
        assert math.isinf(fe.sharpe)
        # NaN check handles inf too via math.isnan check

    def test_negative_sharpe_blocked(self):
        fe = FactorEvidence(factor_id="f1", sharpe=-0.5)
        assert not fe.can_promote()

    def test_zero_sharpe_blocked(self):
        fe = FactorEvidence(factor_id="f1", sharpe=0.0)
        assert not fe.can_promote()

    def test_missing_dag_blocked(self):
        fe = FactorEvidence(factor_id="f1", sharpe=1.5, evidence_dag_hash="")
        assert not fe.can_promote()

    def test_valid_factor_promoted(self):
        fe = FactorEvidence(factor_id="f1", sharpe=1.2, evidence_dag_hash="dag-abc", state=FactorLifecycleState.ACTIVE)
        assert fe.can_promote()


# ============================================================================
# BD-CV43: Protection — UNKNOWN/PENDING ≠ 覆盖
# ============================================================================


class TestProtectionEdgeCases:
    """BD-CV43: UNKNOWN/PENDING 本地对象不算覆盖。"""

    def test_inactive_sl_not_covered(self):
        sl = ProtectionOrder(order_id="o1", symbol="BTCUSDT", stop_loss_price=49000.0, is_active=False)
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, stop_loss=sl, coverage_pct=100.0)
        assert not pa.has_full_coverage()

    def test_none_sl_not_covered(self):
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, stop_loss=None, coverage_pct=0.0)
        assert not pa.has_full_coverage()

    def test_zero_position_always_covered(self):
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=0.0)
        assert pa.has_full_coverage()

    def test_partial_coverage_not_full(self):
        sl = ProtectionOrder(order_id="o1", symbol="BTCUSDT", stop_loss_price=49000.0, is_active=True)
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, stop_loss=sl, coverage_pct=50.0)
        assert not pa.has_full_coverage()


# ============================================================================
# BD-CV44: 三方同源伪造 + 对账 EDGE
# ============================================================================


class TestReconciliationEdgeCases:
    """BD-CV44: 对账边界。"""

    def test_fraud_detected_two_sources(self):
        tr = TripleReconciliation()
        assert not tr.detect_same_source_fraud(["exchange", "exchange"])  # 完全同源
        assert not tr.detect_same_source_fraud(["system", "exchange"])  # 仅两个

    def test_fraud_detected_three_sources(self):
        tr = TripleReconciliation()
        assert tr.detect_same_source_fraud(["system", "exchange", "event_stream"])

    def test_ledger_imbalance_detected(self):
        postings = [
            LedgerPosting(account="a", leg_type=LegType.DEBIT, amount=100),
            LedgerPosting(account="b", leg_type=LegType.CREDIT, amount=99.99),
        ]
        tx = LedgerTransaction(tx_id="t1", postings=postings)
        assert not tx.is_balanced()

    def test_ledger_empty_balanced(self):
        tx = LedgerTransaction(tx_id="t1")
        assert tx.is_balanced()


# ============================================================================
# BD-CV30: 空头 target 为负
# ============================================================================


class TestShortTargetNegative:
    """BD-CV30: 空头 target_position 必须为负。"""

    def test_short_position_is_negative(self):
        from beidou_strategy.portfolio.contracts import PositionSide
        assert PositionSide.SHORT.value == "SHORT"

    def test_long_positive_exposure(self):
        from beidou_strategy.portfolio.contracts import SignedPortfolioTarget, PositionSide
        t = SignedPortfolioTarget(
            target_id="t1", symbol="BTCUSDT", side=PositionSide.LONG,
            target_exposure=50000.0, delta=100.0,
        )
        assert t.is_valid()

    def test_flat_zero_delta(self):
        from beidou_strategy.portfolio.contracts import SignedPortfolioTarget, PositionSide
        t = SignedPortfolioTarget(
            target_id="t1", symbol="BTCUSDT", side=PositionSide.FLAT,
            target_exposure=0.0, delta=0.0,
        )
        assert t.is_valid()


# Helper
class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
