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
    assert "non-finite" in decision.reason


def test_inf_icir_rejected() -> None:
    decision = _decide(_perf(icir=float("inf")))
    assert decision.approved is False
    assert "non-finite" in decision.reason


def test_nan_ic_mean_rejected() -> None:
    decision = _decide(_perf(icir=0.5, ic_mean=float("nan")))
    assert decision.approved is False
    assert "non-finite" in decision.reason


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


# --- M04-R2（对抗审查反例固化） ---


def test_rank_icir_inf_rejected() -> None:
    """对抗审查: 非有限检查必须覆盖全部数值字段(旧实现仅查 3 字段)。"""
    import dataclasses

    perf = dataclasses.replace(_perf(icir=0.5), rank_icir=float("inf"))
    decision = _decide(perf)
    assert decision.approved is False
    assert "rank_icir" in decision.reason


def test_cost_adjusted_ic_nan_rejected() -> None:
    import dataclasses

    perf = dataclasses.replace(_perf(icir=0.5), cost_adjusted_ic=float("nan"))
    assert _decide(perf).approved is False


def test_string_metrics_normalized_not_crash() -> None:
    """对抗审查: str '0.5' 应归一为 0.5 通过(而非 str 入证),且不 crash。"""
    perf = _perf(icir="0.5")  # type: ignore[arg-type]
    decision = _decide(perf)
    assert decision.approved is True
    assert decision.icir == 0.5
    assert isinstance(decision.icir, float)


def test_string_nan_rejected_without_crash() -> None:
    perf = _perf(icir="nan")  # type: ignore[arg-type]
    decision = _decide(perf)
    assert decision.approved is False
    assert "icir" in decision.reason


def test_none_icir_rejected_without_crash() -> None:
    perf = _perf(icir=None)  # type: ignore[arg-type]
    decision = _decide(perf)
    assert decision.approved is False


def test_performance_none_required_for_threshold_states() -> None:
    """对抗审查: 有指标门槛的状态不得在 performance=None 时晋级。"""
    gate = FactorPromotionGate()
    decision = gate.validate_evidence(
        factor_id="f1",
        current_state=FactorLifecycle.GENERATED,
        target_state=FactorLifecycle.SANITY_PASSED,
        performance=None,
        evidence_ids=["ic_significant", "rank_ic_significant", "decile_spread_positive"],
    )
    assert decision.approved is False
    assert "performance_required" in decision.reason


def test_icir_degenerate_series_clamped() -> None:
    """对抗审查: 近退化序列不得产出 3.9e11 超级 ICIR(钳制/归零)。"""
    series = [0.5, 0.500000000002, 0.500000000001, 0.500000000003, 0.5]
    value = FactorEvaluator.compute_icir(series)
    assert abs(value) <= 1e4
    assert value == 0.0  # std 远小于容差 → 保守归零


def test_icir_nan_input_filtered() -> None:
    """对抗审查: 含 NaN 的 IC 序列不得产出 NaN。"""
    assert FactorEvaluator.compute_icir([0.5, float("nan"), 0.5, 0.6]) != float("nan")
    assert FactorEvaluator.compute_icir([0.5, float("nan")]) == 0.0  # 过滤后 <2
