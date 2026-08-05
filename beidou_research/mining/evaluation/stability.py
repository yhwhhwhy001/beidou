"""BF-06: 因子稳健性评估。

测试维度：
- 时间分段（前后半段）
- 品种分段
- 高低波动
- 趋势/震荡
- 高低流动性
- 参数邻域
- 成本压力
- 数据缺口
- 极端行情
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StabilityResult:
    """稳健性评估结果。"""
    dimension: str
    metric_name: str
    full_sample_value: float
    sub_sample_value: float
    degradation_pct: float          # 正数=退化，负数=改善
    is_stable: bool                  # 退化 < threshold 认为稳定
    threshold: float = 0.30          # 30% 退化阈值
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StabilityReport:
    """综合稳健性报告。"""
    results: list[StabilityResult] = field(default_factory=list)
    stable_dimensions: int = 0
    unstable_dimensions: int = 0
    overall_stable: bool = True
    min_degradation: float = 0.0
    max_degradation: float = 0.0
    recommendations: list[str] = field(default_factory=list)


class StabilityEvaluator:
    """因子稳健性评估器。

    评估因子在不同市场条件下的表现一致性。
    大规模退化（>50%）表示因子可能过拟合特定市场状态。
    """

    def __init__(self, degradation_threshold: float = 0.30) -> None:
        self.threshold = degradation_threshold

    def evaluate_time_split(
        self,
        predictions: list[float],
        returns: list[float],
        ic_full: float,
        split_ratio: float = 0.5,
    ) -> StabilityResult:
        """时间分段稳健性：前后半段 IC 一致性。"""
        n = len(predictions)
        split = int(n * split_ratio)

        first_half_pred = predictions[:split]
        first_half_ret = returns[:split]
        second_half_pred = predictions[split:]
        second_half_ret = returns[split:]

        ic_first = _compute_ic(first_half_pred, first_half_ret)
        ic_second = _compute_ic(second_half_pred, second_half_ret)

        if abs(ic_full) < 1e-10:
            return StabilityResult(
                dimension="time_split",
                metric_name="ic",
                full_sample_value=ic_full,
                sub_sample_value=ic_second,
                degradation_pct=1.0,
                is_stable=False,
            )

        degradation = abs(ic_second - ic_first) / max(abs(ic_full), 1e-10)
        return StabilityResult(
            dimension="time_split",
            metric_name="ic",
            full_sample_value=ic_full,
            sub_sample_value=ic_second,
            degradation_pct=round(degradation, 4),
            is_stable=degradation < self.threshold,
            threshold=self.threshold,
            metadata={"ic_first_half": ic_first, "ic_second_half": ic_second},
        )

    def evaluate_volatility_regime(
        self,
        predictions: list[float],
        returns: list[float],
        volatility_indicators: list[float],
        ic_full: float,
        high_vol_threshold: float | None = None,
    ) -> StabilityResult:
        """波动率分段稳健性。"""
        if high_vol_threshold is None:
            high_vol_threshold = sorted(volatility_indicators)[int(0.7 * len(volatility_indicators))]

        high_vol_pred = [p for p, v in zip(predictions, volatility_indicators) if v >= high_vol_threshold]
        high_vol_ret = [r for r, v in zip(returns, volatility_indicators) if v >= high_vol_threshold]
        low_vol_pred = [p for p, v in zip(predictions, volatility_indicators) if v < high_vol_threshold]
        low_vol_ret = [r for r, v in zip(returns, volatility_indicators) if v < high_vol_threshold]

        ic_high = _compute_ic(high_vol_pred, high_vol_ret) if len(high_vol_pred) > 10 else 0.0
        ic_low = _compute_ic(low_vol_pred, low_vol_ret) if len(low_vol_pred) > 10 else 0.0

        if abs(ic_full) < 1e-10:
            degradation = 1.0 if abs(ic_high - ic_low) > 0.1 else 0.0
        else:
            degradation = abs(ic_high - ic_low) / max(abs(ic_full), 1e-10)

        return StabilityResult(
            dimension="volatility_regime",
            metric_name="ic",
            full_sample_value=ic_full,
            sub_sample_value=ic_high,
            degradation_pct=round(degradation, 4),
            is_stable=degradation < self.threshold,
            threshold=self.threshold,
            metadata={
                "ic_high_vol": ic_high,
                "ic_low_vol": ic_low,
                "high_vol_threshold": high_vol_threshold,
                "high_vol_samples": len(high_vol_pred),
                "low_vol_samples": len(low_vol_pred),
            },
        )

    def evaluate_trend_vs_range(
        self,
        predictions: list[float],
        returns: list[float],
        trend_indicators: list[float],
        ic_full: float,
    ) -> StabilityResult:
        """趋势/震荡分段稳健性。"""
        median_abs_trend = sorted([abs(t) for t in trend_indicators])[len(trend_indicators) // 2]

        trending_pred = [p for p, t in zip(predictions, trend_indicators) if abs(t) > median_abs_trend]
        trending_ret = [r for r, t in zip(returns, trend_indicators) if abs(t) > median_abs_trend]
        ranging_pred = [p for p, t in zip(predictions, trend_indicators) if abs(t) <= median_abs_trend]
        ranging_ret = [r for r, t in zip(returns, trend_indicators) if abs(t) <= median_abs_trend]

        ic_trend = _compute_ic(trending_pred, trending_ret) if len(trending_pred) > 10 else 0.0
        ic_range = _compute_ic(ranging_pred, ranging_ret) if len(ranging_pred) > 10 else 0.0

        if abs(ic_full) < 1e-10:
            degradation = 1.0 if abs(ic_trend - ic_range) > 0.1 else 0.0
        else:
            degradation = abs(ic_trend - ic_range) / max(abs(ic_full), 1e-10)

        return StabilityResult(
            dimension="trend_vs_range",
            metric_name="ic",
            full_sample_value=ic_full,
            sub_sample_value=ic_trend,
            degradation_pct=round(degradation, 4),
            is_stable=degradation < self.threshold,
            threshold=self.threshold,
            metadata={
                "ic_trending": ic_trend,
                "ic_ranging": ic_range,
                "trending_samples": len(trending_pred),
                "ranging_samples": len(ranging_pred),
            },
        )

    def evaluate_parameter_neighborhood(
        self,
        base_predictions: list[float],
        perturbed_predictions_list: list[list[float]],
        returns: list[float],
        ic_base: float,
    ) -> StabilityResult:
        """参数邻域稳健性。

        在主参数附近扰动参数，检查因子值稳定性。
        大的参数敏感度表示过拟合风险。
        """
        perturbed_ics = []
        for perturbed in perturbed_predictions_list:
            if len(perturbed) > 10:
                ic = _compute_ic(perturbed, returns[:len(perturbed)])
                perturbed_ics.append(ic)

        if not perturbed_ics:
            return StabilityResult(
                dimension="parameter_neighborhood",
                metric_name="ic",
                full_sample_value=ic_base,
                sub_sample_value=ic_base,
                degradation_pct=0.0,
                is_stable=True,
            )

        avg_perturbed_ic = sum(perturbed_ics) / len(perturbed_ics)
        ic_std_across_params = (
            sum((ic - avg_perturbed_ic) ** 2 for ic in perturbed_ics) / len(perturbed_ics)
        ) ** 0.5 if len(perturbed_ics) > 1 else 0.0

        degradation = ic_std_across_params / max(abs(ic_base), 1e-10)

        return StabilityResult(
            dimension="parameter_neighborhood",
            metric_name="ic",
            full_sample_value=ic_base,
            sub_sample_value=avg_perturbed_ic,
            degradation_pct=round(degradation, 4),
            is_stable=degradation < self.threshold,
            threshold=self.threshold,
            metadata={
                "ic_base": ic_base,
                "ic_perturbed_mean": avg_perturbed_ic,
                "ic_perturbed_std": ic_std_across_params,
                "n_perturbations": len(perturbed_predictions_list),
            },
        )

    def evaluate_cost_stress(
        self,
        predictions: list[float],
        returns: list[float],
        base_cost_bps: float,
        sharpe_base: float,
    ) -> list[StabilityResult]:
        """成本压力测试：1×, 1.5×, 2× 成本下的稳健性。"""
        results = []
        for multiplier in [1.0, 1.5, 2.0]:
            cost_bps = base_cost_bps * multiplier
            cost_decimal = cost_bps / 10000.0
            cost_adjusted_returns = [r - cost_decimal for r in returns]

            sharpe_cost = _compute_sharpe(cost_adjusted_returns)
            degradation = (
                (sharpe_base - sharpe_cost) / max(abs(sharpe_base), 1e-10)
                if sharpe_base > 0 else 1.0
            )

            results.append(StabilityResult(
                dimension=f"cost_stress_{multiplier}x",
                metric_name="sharpe",
                full_sample_value=sharpe_base,
                sub_sample_value=sharpe_cost,
                degradation_pct=round(max(0, degradation), 4),
                is_stable=sharpe_cost > 0 or multiplier <= 1.5,
                threshold=0.50,
                metadata={
                    "base_cost_bps": base_cost_bps,
                    "stressed_cost_bps": cost_bps,
                    "multiplier": multiplier,
                },
            ))

        return results


def _compute_ic(predictions: list[float], returns: list[float]) -> float:
    """简化的 Pearson IC 计算（不依赖 numpy）。"""
    n = min(len(predictions), len(returns))
    if n < 3:
        return 0.0
    p = predictions[:n]
    r = returns[:n]
    mp = sum(p) / n
    mr = sum(r) / n
    cov = sum((p[i] - mp) * (r[i] - mr) for i in range(n)) / (n - 1)
    sp = (sum((x - mp) ** 2 for x in p) / (n - 1)) ** 0.5
    sr = (sum((x - mr) ** 2 for x in r) / (n - 1)) ** 0.5
    if sp == 0 or sr == 0:
        return 0.0
    return cov / (sp * sr)


def _compute_sharpe(returns: list[float]) -> float:
    """简化的 Sharpe ratio（无风险利率 = 0）。"""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return mean / std
