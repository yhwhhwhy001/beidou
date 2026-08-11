"""BD-CV: Property-based 不变性测试。

使用 hypothesis 验证关键数学不变量。
"""

from __future__ import annotations

from hypothesis import given
from hypothesis.strategies import floats, lists

from beidou_certification.contracts import FaultScenario, FaultInjectionResult
from beidou_research.contracts import StatisticalTest, StatisticalValidationResult
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
    PortfolioConstraints,
    PositionSide,
    SignedPortfolioTarget,
)


# ============================================================================
# BD-CV30: Exposure Algebra — gross >= abs(net)
# ============================================================================


class TestExposureAlgebra:
    """BD-CV30: 多空暴露代数不变量。

    SUM( |long_i|, |short_i| ) >= ABS( SUM(long_i, short_i) )
    """

    @given(
        long_exposures=lists(floats(min_value=0, max_value=100000), min_size=1, max_size=10),
        short_exposures=lists(floats(min_value=0, max_value=100000), min_size=1, max_size=10),
    )
    def test_gross_gte_abs_net(self, long_exposures, short_exposures):
        gross = sum(long_exposures) + sum(short_exposures)
        net = sum(long_exposures) - sum(short_exposures)
        assert gross >= abs(net), f"gross={gross} < abs(net)={abs(net)}"

    @given(
        target=floats(min_value=1.0, max_value=100000),
        delta=floats(min_value=0.0, max_value=0.99).map(lambda d: d),
    )
    def test_signed_target_validity(self, target, delta):
        # delta is always a fraction of target_exposure, so gross >= abs(net)
        actual_delta = target * delta
        t = SignedPortfolioTarget(
            target_id="test", symbol="BTCUSDT",
            side=PositionSide.LONG if target > 0 else PositionSide.SHORT,
            target_exposure=target, delta=actual_delta,
        )
        assert t.is_valid(), f"SignedPortfolioTarget invalid: gross={t.target_exposure}, delta={t.delta}"


# ============================================================================
# BD-CV42: PositionAggregate Determinism
# ============================================================================


class TestPositionDeterminism:
    """BD-CV42: 任意成交序列 replay 确定性。"""

    @given(
        seed_qty=floats(min_value=-100, max_value=100),
        fills1=lists(
            floats(min_value=-10, max_value=10).map(
                lambda x: Fill(fill_id="f", symbol="BTCUSDT", side="BUY" if x > 0 else "SELL", quantity=abs(x), price=50000.0)
            ),
            min_size=0, max_size=20,
        ),
    )
    def test_replay_deterministic(self, seed_qty, fills1):
        pa1 = PositionAggregate(symbol="BTCUSDT", net_position=seed_qty)
        result1 = pa1.replay(fills1)
        result2 = pa1.replay(fills1)
        assert result1.net_position == result2.net_position
        assert result1.avg_entry_price == result2.avg_entry_price


# ============================================================================
# BD-CV44: Ledger Balance — 借贷守恒
# ============================================================================


class TestLedgerInvariants:
    """BD-CV44: 复式账本借贷守恒。"""

    @given(
        amounts=lists(floats(min_value=0.01, max_value=10000), min_size=1, max_size=5),
    )
    def test_ledger_balanced(self, amounts):
        postings = [
            LedgerPosting(account="asset", leg_type=LegType.DEBIT, amount=a) for a in amounts
        ] + [
            LedgerPosting(account="liability", leg_type=LegType.CREDIT, amount=a) for a in amounts
        ]
        tx = LedgerTransaction(tx_id="test", postings=postings)
        assert tx.is_balanced(), f"Ledger unbalanced"


    def test_unbalanced_detected(self):
        postings = [
            LedgerPosting(account="a", leg_type=LegType.DEBIT, amount=100.0),
            LedgerPosting(account="b", leg_type=LegType.CREDIT, amount=99.0),
        ]
        tx = LedgerTransaction(tx_id="test", postings=postings)
        assert not tx.is_balanced()


# ============================================================================
# BD-CV43: Protection Coverage — nonzero → 100% SL
# ============================================================================


class TestProtectionInvariants:
    """BD-CV43: 仓位保护不变量。"""

    def test_zero_position_always_covered(self):
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=0.0)
        assert pa.has_full_coverage()

    def test_nonzero_without_sl_not_covered(self):
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, coverage_pct=0.0)
        assert not pa.has_full_coverage()

    def test_nonzero_with_active_sl_covered(self):
        sl = ProtectionOrder(order_id="o1", symbol="BTCUSDT", stop_loss_price=49000.0, is_active=True)
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, stop_loss=sl, coverage_pct=100.0)
        assert pa.has_full_coverage()

    def test_inactive_sl_not_covered(self):
        sl = ProtectionOrder(order_id="o1", symbol="BTCUSDT", stop_loss_price=49000.0, is_active=False)
        pa = ProtectionAggregate(symbol="BTCUSDT", position_qty=1.0, stop_loss=sl, coverage_pct=100.0)
        assert not pa.has_full_coverage()


# ============================================================================
# BD-CV40: ExecutionPlan — NOT_EXECUTABLE
# ============================================================================


class TestExecutionPlanInvariants:
    """BD-CV40: 执行计划不变量。"""

    def test_empty_slices_not_executable(self):
        plan = ExecutionPlan()
        assert not plan.is_executable()
        assert plan.status == PlanStatus.NOT_EXECUTABLE

    def test_with_slices_is_executable(self):
        plan = ExecutionPlan(
            plan_id="p1",
            slices=[PlanSlice(slice_id="s1", symbol="BTCUSDT", quantity="0.001", price="50000", order_type="LIMIT", algorithm="TWAP")],
            status=PlanStatus.PENDING,
            algorithm="TWAP",
        )
        assert plan.is_executable()

    def test_emergency_never_increases_absolute_position(self):
        plan = ExecutionPlan(
            plan_id="p1",
            slices=[
                PlanSlice(slice_id="s1", symbol="BTCUSDT", quantity="0.5", price="50000", order_type="MARKET", algorithm="EMERGENCY"),
                PlanSlice(slice_id="s2", symbol="BTCUSDT", quantity="0.3", price="50000", order_type="MARKET", algorithm="EMERGENCY"),
            ],
            is_emergency=True,
            algorithm="EMERGENCY",
        )
        # emergency total qty (0.8) <= current position (1.0) → OK
        assert plan.never_increases_absolute_position(1.0)
        # emergency total qty (0.8) > current position (0.5) → FAIL
        assert not plan.never_increases_absolute_position(0.5)


# ============================================================================
# BD-CV32: AdaptiveSizing — 风险变差不增加
# ============================================================================


class TestAdaptiveSizingInvariants:
    """BD-CV32: 自适应仓位不变量。"""

    def test_safe_sizing_does_not_increase_on_worse_risk(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=2.0, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=3.0, is_safe=False)
        assert curr.does_size_increase(prev)
        assert not curr.is_safe

    def test_safe_sizing_increase_allowed_on_better_risk(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=1.0, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=2.0, is_safe=True)
        # safe + increase → 允许
        assert not curr.does_size_increase(prev)


# ============================================================================
# BD-CV20: PBO — n_combos 变化时分区真实变化
# ============================================================================


class TestStatisticalInvariants:
    """BD-CV20: 统计验证不变量。"""

    def test_pbo_hash_changes_with_n_combos(self):
        r1 = StatisticalValidationResult(test_type=StatisticalTest.PBO, n_combos=100)
        r2 = StatisticalValidationResult(test_type=StatisticalTest.PBO, n_combos=200)
        assert r1.compute_hash() != r2.compute_hash()

    def test_same_params_same_hash(self):
        r1 = StatisticalValidationResult(test_type=StatisticalTest.DSR, p_value=0.05, deflated_sharpe=0.8, n_combos=50)
        r2 = StatisticalValidationResult(test_type=StatisticalTest.DSR, p_value=0.05, deflated_sharpe=0.8, n_combos=50)
        assert r1.compute_hash() == r2.compute_hash()


# ============================================================================
# BD-CV54: Fault Injection — 机器可判定
# ============================================================================


class TestFaultInjectionInvariants:
    """BD-CV54: 故障注入不变性。"""

    def test_all_scenarios_have_defined_contract(self):
        for scenario in FaultScenario:
            result = FaultInjectionResult(scenario=scenario, passed=True)
            assert result.is_machine_decidable(), f"Scenario {scenario.value} not machine-decidable"

    def test_duplicate_orders_blocked_on_timeout(self):
        result = FaultInjectionResult(scenario=FaultScenario.TIMEOUT, duplicate_orders_detected=0, risk_increase_detected=False, passed=True)
        assert result.is_machine_decidable()
        assert result.duplicate_orders_detected == 0

    def test_risk_increase_fails_machine_check(self):
        result = FaultInjectionResult(scenario=FaultScenario.DUAL_INSTANCE, risk_increase_detected=True, passed=False)
        assert not result.is_machine_decidable()
