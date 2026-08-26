"""BF-06: 边际贡献评估。

增量贡献定义:
  ΔMetric = Metric(portfolio + candidate) - Metric(portfolio)

必须在相同 OOS folds、成本模型、仓位约束、资本预算、
风险政策和再平衡频率下比较。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class IncrementalContribution:
    """增量贡献结果。"""

    candidate_id: str
    delta_sharpe: float
    delta_ic: float
    delta_ir: float
    delta_diversification: float
    is_positive_contribution: bool
    sample_count: int
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MarginalContributionRecomputation:
    """Strict same-key incremental return recomputation used by T08."""

    sample_count: int
    delta_returns: tuple[float, ...]
    mean_delta_return: float
    marginal_volatility: float
    marginal_sharpe_per_bar: float
    marginal_annualized_sharpe: float


def recompute_marginal_contribution(
    champion_net_returns: list[float],
    combined_net_returns: list[float],
    *,
    annualization_bars: int = 8760,
) -> MarginalContributionRecomputation:
    """Recompute marginal metrics without truncation, defaults, or alignment repair."""

    if len(champion_net_returns) != len(combined_net_returns):
        raise ValueError("RETURN_ALIGNMENT_MISMATCH")
    if len(champion_net_returns) < 2:
        raise ValueError("RETURN_SAMPLES_INSUFFICIENT")
    values = [*champion_net_returns, *combined_net_returns]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError("RETURN_VALUE_INVALID")
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("RETURN_VALUE_NON_FINITE")
    delta_returns = tuple(
        float(combined) - float(champion)
        for champion, combined in zip(champion_net_returns, combined_net_returns, strict=True)
    )
    mean_delta = _mean(list(delta_returns))
    volatility = _std(list(delta_returns))
    sharpe = mean_delta / volatility if volatility > 0.0 else 0.0
    return MarginalContributionRecomputation(
        sample_count=len(delta_returns),
        delta_returns=delta_returns,
        mean_delta_return=mean_delta,
        marginal_volatility=volatility,
        marginal_sharpe_per_bar=sharpe,
        marginal_annualized_sharpe=sharpe * math.sqrt(float(annualization_bars)),
    )


def compute_incremental_contribution(
    portfolio_returns: list[float],
    portfolio_with_candidate_returns: list[float],
    candidate_id: str = "",
) -> IncrementalContribution:
    """计算候选因子对组合的增量贡献。

    Args:
        portfolio_returns: 不含候选因子的组合收益
        portfolio_with_candidate_returns: 含候选因子的组合收益
        candidate_id: 候选ID

    Returns:
        IncrementalContribution
    """
    n = min(len(portfolio_returns), len(portfolio_with_candidate_returns))
    if n < 10:
        return IncrementalContribution(
            candidate_id=candidate_id,
            delta_sharpe=0.0,
            delta_ic=0.0,
            delta_ir=0.0,
            delta_diversification=0.0,
            is_positive_contribution=False,
            sample_count=n,
        )

    base_ret = portfolio_returns[:n]
    combined_ret = portfolio_with_candidate_returns[:n]

    # Delta metrics
    sharpe_base = _sharpe(base_ret)
    sharpe_combined = _sharpe(combined_ret)
    delta_sharpe = sharpe_combined - sharpe_base

    # IC delta (简化: 使用收益变化作为代理)
    delta_ret = [combined_ret[i] - base_ret[i] for i in range(n)]
    delta_ic = _mean(delta_ret) / (_std(delta_ret) + 1e-10)

    # IR delta
    delta_ir = delta_sharpe * (252**0.5) if delta_sharpe != 0 else 0.0

    # Diversification delta (收益波动率变化)
    vol_base = _std(base_ret)
    vol_combined = _std(combined_ret)
    delta_diversification = (vol_base - vol_combined) / max(vol_base, 1e-10)

    return IncrementalContribution(
        candidate_id=candidate_id,
        delta_sharpe=round(delta_sharpe, 6),
        delta_ic=round(delta_ic, 6),
        delta_ir=round(delta_ir, 6),
        delta_diversification=round(delta_diversification, 6),
        is_positive_contribution=delta_sharpe > 0,
        sample_count=n,
    )


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return float((sum((v - m) ** 2 for v in values) / (n - 1)) ** 0.5)


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    s = _std(returns)
    if s == 0:
        return 0.0
    return _mean(returns) / s
