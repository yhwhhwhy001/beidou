"""因子晋级门禁 NaN/Inf 防护与 ICIR 边界测试（M04）。

背景: 旧门禁 `icir < min_icir` 对 NaN 恒 False（直接通过）、Inf 通过;
compute_icir 零方差返回 ±inf 穿透阈值。修复: 非有限指标显式拒绝、
零方差 ICIR 返回保守 0.0。
"""

from __future__ import annotations

from beidou_research.factors.factor import (
    FactorEvaluator,
    FactorLifecycle,
    FactorPerformance,
    FactorPromotionGate,
)


def _perf(icir: float, ic_mean: float = 0.3, sample_count: int = 100) -> FactorPerformance:
    return FactorPerformance(
        factor_id="f1",
        evaluation_period="2026-Q3",
        sample_count=sample_count,
        ic_mean=ic_mean,
        ic_std=0.1,
        icir=icir,
        rank_ic_mean=0.2,
        rank_ic_std=0.1,
        rank_icir=2.0,
    )


def _decide(perf: FactorPerformance) -> object:
    gate = FactorPromotionGate()
    return gate.validate_evidence(
        factor_id="f1",
        current_state=FactorLifecycle.GENERATED,
        target_state=FactorLifecycle.SANITY_PASSED,
        performance=perf,
        evidence_ids=["ic_significant", "rank_ic_significant", "decile_spread_positive"],
    )


def test_nan_icir_rejected() -> None:
    decision = _decide(_perf(icir=float("nan")))
    assert decision.approved is False
    assert "Non-finite" in decision.reason


def test_inf_icir_rejected() -> None:
    decision = _decide(_perf(icir=float("inf")))
    assert decision.approved is False
    assert "Non-finite" in decision.reason


def test_nan_ic_mean_rejected() -> None:
    decision = _decide(_perf(icir=0.5, ic_mean=float("nan")))
    assert decision.approved is False
    assert "Non-finite" in decision.reason


def test_nan_sample_count_rejected() -> None:
    nan_count = float("nan")
    decision = _decide(_perf(icir=0.5, sample_count=nan_count))  # type: ignore[arg-type]
    assert decision.approved is False


def test_finite_metrics_pass_threshold() -> None:
    decision = _decide(_perf(icir=0.5, ic_mean=0.3, sample_count=100))
    assert decision.approved is True


def test_icir_zero_variance_returns_zero_not_inf() -> None:
    """M04-F02: 全同 IC 序列无法推断信息比 → 保守 0.0（旧实现 ±inf 穿透门禁）。"""
    assert FactorEvaluator.compute_icir([0.5, 0.5, 0.5, 0.5]) == 0.0
    assert FactorEvaluator.compute_icir([-0.2, -0.2, -0.2]) == 0.0
    assert FactorEvaluator.compute_icir([0.0, 0.0]) == 0.0


def test_icir_normal_case() -> None:
    value = FactorEvaluator.compute_icir([0.1, 0.3, 0.5])
    assert value == (0.1 + 0.3 + 0.5) / 3 / ((0.2**2 + 0.2**2) / 2) ** 0.5


def test_ic_constant_predictions_return_zero() -> None:
    ic, _ = FactorEvaluator.compute_ic([1.0, 1.0, 1.0, 1.0], [0.01, -0.01, 0.02, -0.02])
    assert ic == 0.0


def test_ic_perfectly_correlated_is_one() -> None:
    preds = [1.0, 2.0, 3.0, 4.0]
    rets = [0.5, 1.0, 1.5, 2.0]
    ic, _ = FactorEvaluator.compute_ic(preds, rets)
    assert abs(ic - 1.0) < 1e-9


def test_icir_short_series_returns_zero() -> None:
    assert FactorEvaluator.compute_icir([0.5]) == 0.0
    assert FactorEvaluator.compute_icir([]) == 0.0
