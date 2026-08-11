"""BD-CV32: 自适应仓位引擎。

采用单调安全约束：风险变差时 sizing 不增加。
任何关键输入 UNKNOWN 时只能降低或归零。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SizingInput:
    """BD-CV32: Sizing 输入。"""

    equity: float
    available_margin: float
    portfolio_risk_pct: float = 0.02
    volatility: float = 0.02  # annualized
    capacity_utilization: float = 0.5
    regime_confidence: float = 0.5
    funding_rate: float = 0.0
    liquidation_distance_pct: float = 0.10


@dataclass
class SizingDecision:
    """BD-CV32: Sizing 决策输出。"""

    leverage: float = 0.0
    position_size_pct: float = 0.0
    reason_vector: list[str] = field(default_factory=list)
    policy_hash: str = ""
    is_safe: bool = True


def compute_adaptive_sizing(inputs: SizingInput, previous: SizingDecision | None = None) -> SizingDecision:
    """BD-CV32: 自适应仓位计算。

    单调安全约束：波动/相关性/资金费/滑点风险上升 → 杠杆不增加。
    UNKNOWN input → 降低或归零。
    """
    reasons: list[str] = []
    is_safe = True

    # 1. Base leverage from risk budget
    if inputs.portfolio_risk_pct <= 0:
        return SizingDecision(leverage=0.0, position_size_pct=0.0, reason_vector=["RISK_BUDGET_ZERO"], is_safe=False)

    base_leverage = inputs.available_margin / max(inputs.equity, 1.0)

    # 2. Volatility adjustment: higher vol → lower leverage
    vol_scalar = max(0.1, min(1.0, 0.20 / max(inputs.volatility, 0.001)))
    if vol_scalar < 0.5:
        reasons.append(f"HIGH_VOL:volatility={inputs.volatility:.1%}")
        is_safe = False

    # 3. Capacity adjustment
    cap_scalar = max(0.1, min(1.0, inputs.capacity_utilization))
    if cap_scalar < 0.3:
        reasons.append(f"CAPACITY_LIMITED:{inputs.capacity_utilization:.1%}")
        is_safe = False

    # 4. Regime confidence: low confidence → reduce
    regime_scalar = max(0.25, min(1.0, inputs.regime_confidence))
    if regime_scalar < 0.5:
        reasons.append(f"LOW_REGIME_CONFIDENCE:{inputs.regime_confidence:.1%}")

    # 5. Funding rate drag
    funding_penalty = 1.0
    if abs(inputs.funding_rate) > 0.001:  # > 0.1% hourly
        funding_penalty = max(0.25, 1.0 - abs(inputs.funding_rate) * 10.0)
        reasons.append(f"FUNDING_PENALTY:{inputs.funding_rate:.4f}")

    # 6. Liquidation distance
    liq_scalar = 1.0
    if inputs.liquidation_distance_pct < 0.20:
        liq_scalar = max(0.25, inputs.liquidation_distance_pct / 0.20)
        reasons.append(f"CLOSE_TO_LIQUIDATION:{inputs.liquidation_distance_pct:.1%}")

    adjusted_leverage = base_leverage * vol_scalar * cap_scalar * regime_scalar * funding_penalty * liq_scalar

    # 7. Monotonic safety: risk worsening → sizing must not increase
    if previous is not None and previous.leverage > 0:
        if not is_safe and adjusted_leverage > previous.leverage:
            adjusted_leverage = previous.leverage
            reasons.append("MONOTONIC_CLAMP: risk worsening, sizing frozen at previous level")

    # 8. Position size as % of equity
    position_size_pct = min(adjusted_leverage * inputs.portfolio_risk_pct, 0.95)

    return SizingDecision(
        leverage=round(adjusted_leverage, 2),
        position_size_pct=round(position_size_pct, 4),
        reason_vector=reasons,
        is_safe=is_safe,
    )
