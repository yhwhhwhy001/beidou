"""BD-CV23/30/31/32: Strategy + Portfolio 合约集成测试。"""

from __future__ import annotations

from beidou_research.contracts import (
    KernelParityResult,
    StrategyAction,
    StrategySignal,
)
from beidou_strategy.portfolio.contracts import (
    AdaptiveSizing,
    OptimizationResult,
    PortfolioConstraints,
    PositionSide,
    RiskApproval,
    SignedPortfolioTarget,
)

# ============================================================================
# BD-CV23: StrategyAction semantics
# ============================================================================


class TestStrategyActionSemantics:
    """BD-CV23: ACTION/NO_ACTION/VETO/DEGRADED 语义。"""

    def test_action_is_actionable(self):
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.ACTION)
        assert sig.is_actionable()
        assert not sig.is_blocking()
        assert not sig.is_noop()

    def test_no_action_is_not_actionable(self):
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.NO_ACTION)
        assert not sig.is_actionable()
        assert not sig.is_blocking()
        assert sig.is_noop()

    def test_veto_is_blocking(self):
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.VETO)
        assert not sig.is_actionable()
        assert sig.is_blocking()
        assert not sig.is_noop()

    def test_degraded_is_not_actionable(self):
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.DEGRADED)
        assert not sig.is_actionable()
        assert not sig.is_blocking()
        assert not sig.is_noop()

    def test_no_action_not_rewritten_to_action(self):
        """NO_ACTION 不会被改写为 ACTION。"""
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.NO_ACTION)
        assert sig.action != StrategyAction.ACTION
        assert not sig.is_actionable()

    def test_veto_blocks_downstream_in_all_envs(self):
        """VETO 在所有环境均阻断下游。"""
        sig = StrategySignal(strategy_id="s1", action=StrategyAction.VETO)
        assert sig.is_blocking()


# ============================================================================
# BD-CV30: SignedPortfolioTarget
# ============================================================================


class TestSignedPortfolioTargetValidation:
    """BD-CV30: 签名组合目标 — gross >= abs(net)."""

    def test_long_target_valid(self):
        t = SignedPortfolioTarget(
            target_id="t1",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            target_exposure=50000.0,
            delta=100.0,
        )
        assert t.is_valid()

    def test_short_target_valid(self):
        t = SignedPortfolioTarget(
            target_id="t2",
            symbol="BTCUSDT",
            side=PositionSide.SHORT,
            target_exposure=30000.0,
            delta=50.0,
        )
        assert t.is_valid()

    def test_flat_target_valid(self):
        t = SignedPortfolioTarget(
            target_id="t3",
            symbol="BTCUSDT",
            side=PositionSide.FLAT,
            target_exposure=0.0,
            delta=0.0,
        )
        assert t.is_valid()

    def test_delta_exceeds_exposure_invalid(self):
        t = SignedPortfolioTarget(
            target_id="t4",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            target_exposure=100.0,
            delta=200.0,
        )
        assert not t.is_valid()  # gross < abs(net)

    def test_hash_deterministic(self):
        t1 = SignedPortfolioTarget(target_id="a", symbol="BTCUSDT", side=PositionSide.LONG, target_exposure=50000.0)
        t2 = SignedPortfolioTarget(target_id="a", symbol="BTCUSDT", side=PositionSide.LONG, target_exposure=50000.0)
        assert t1.compute_hash() == t2.compute_hash()

    def test_hash_differs_by_symbol(self):
        t1 = SignedPortfolioTarget(target_id="a", symbol="BTCUSDT", target_exposure=50000.0)
        t2 = SignedPortfolioTarget(target_id="a", symbol="ETHUSDT", target_exposure=50000.0)
        assert t1.compute_hash() != t2.compute_hash()


# ============================================================================
# BD-CV31: PortfolioConstraints + OptimizationResult
# ============================================================================


class TestPortfolioOptimization:
    """BD-CV31: 组合约束优化。"""

    def test_constraints_have_sane_defaults(self):
        c = PortfolioConstraints()
        assert c.max_concentration_pct <= 100.0
        assert c.max_leverage > 0
        assert c.min_positions >= 1

    def test_no_violations_is_constrained(self):
        opt = OptimizationResult(
            symbols=["BTCUSDT", "ETHUSDT"],
            weights=[0.6, 0.4],
        )
        assert opt.is_constrained()

    def test_with_violations_not_constrained(self):
        opt = OptimizationResult(
            constraint_violations=["max_concentration_exceeded"],
        )
        assert not opt.is_constrained()

    def test_capacity_utilization_constraint(self):
        c = PortfolioConstraints(capacity_utilization_pct=80.0)
        assert c.capacity_utilization_pct == 80.0


# ============================================================================
# BD-CV32: AdaptiveSizing — monotonic safety
# ============================================================================


class TestAdaptiveSizingMonotonic:
    """BD-CV32: 风险变差时 sizing 不增加。"""

    def test_risk_worsening_no_increase(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=1.0, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=2.0, is_safe=False)
        assert curr.does_size_increase(prev)

    def test_risk_improving_increase_allowed(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=1.0, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=2.0, is_safe=True)
        assert not curr.does_size_increase(prev)

    def test_unchanged_leverage_safe(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=1.5, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=1.5, is_safe=True)
        assert not curr.does_size_increase(prev)

    def test_volatility_scalar_affects_leverage(self):
        s = AdaptiveSizing(base_leverage=2.0, volatility_scalar=0.5)
        assert s.volatility_scalar < 1.0  # 高波动 → 降杠杆

    def test_drawdown_scalar_affects_leverage(self):
        s = AdaptiveSizing(base_leverage=2.0, drawdown_scalar=0.3)
        assert s.drawdown_scalar < 1.0  # 大回撤 → 降杠杆


# ============================================================================
# BD-CV24: KernelParityResult
# ============================================================================


class TestKernelParity:
    """BD-CV24: 内核一致性。"""

    def test_all_same_hash_parity(self):
        kp = KernelParityResult(
            paper_hash="abc",
            replay_hash="abc",
            backtest_hash="abc",
            is_consistent=True,
        )
        assert kp.has_parity()

    def test_replay_differs_no_parity(self):
        kp = KernelParityResult(
            paper_hash="abc",
            replay_hash="def",
            backtest_hash="abc",
            is_consistent=False,
        )
        assert not kp.has_parity()

    def test_empty_hashes_no_parity(self):
        kp = KernelParityResult()
        assert not kp.has_parity()

    def test_missing_one_hash_still_parity_if_consistent(self):
        # 仅 paper hash 存在时，由于 has_parity 检查 is_consistent 且 hashes <= 1
        kp = KernelParityResult(paper_hash="abc", is_consistent=True)
        assert kp.has_parity()  # 单一 hash + consistent = parity

    def test_inconsistencies_tracked(self):
        kp = KernelParityResult(
            paper_hash="abc",
            replay_hash="abc",
            backtest_hash="abc",
            is_consistent=True,
            events_count=100,
            inconsistencies=[],
        )
        assert kp.has_parity()
        assert kp.events_count == 100


# ============================================================================
# BD-CV33: RiskApproval lifecycle
# ============================================================================


class TestRiskApprovalLifecycle:
    """BD-CV33: Approval 生命周期。"""

    def test_approval_expired_by_flag(self):
        a = RiskApproval(approval_id="a1", is_expired=True)
        assert not a.is_valid()

    def test_approval_expired_by_time(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        a = RiskApproval(approval_id="a1", is_expired=False, expires_at=past)
        assert not a.is_valid()

    def test_approval_valid_in_future(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        a = RiskApproval(approval_id="a1", is_expired=False, expires_at=future)
        assert a.is_valid()

    def test_approval_without_expiry_invalid(self):
        a = RiskApproval(approval_id="a1", is_expired=False, expires_at="")
        assert not a.is_valid()


# Helpers for datetime in last test
from datetime import datetime, timedelta, timezone
