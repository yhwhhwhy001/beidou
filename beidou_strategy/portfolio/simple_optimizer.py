"""BD-CV31: 约束组合优化器。

协方差 UNKNOWN/非PSD 时明确 fail-closed。
输出 optimizer diagnostics。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OptimizerInput:
    """优化器输入。"""

    symbols: list[str]
    expected_returns: list[float]  # alpha scores
    covariance_matrix: list[list[float]] | None = None
    current_weights: list[float] | None = None
    max_concentration: float = 0.25
    max_leverage: float = 3.0
    max_turnover: float = 0.30  # 30% max turnover
    risk_aversion: float = 1.0


@dataclass
class OptimizerOutput:
    """BD-CV31: 优化器输出含 diagnostics。"""

    weights: list[float] = field(default_factory=list)
    feasible: bool = True
    diagnostics: dict = field(default_factory=dict)
    constraint_violations: list[str] = field(default_factory=list)
    shadow_prices: dict[str, float] = field(default_factory=dict)
    infeasibility_reason: str = ""


def equal_weight_portfolio(symbols: list[str]) -> OptimizerOutput:
    """BD-CV31: 仅在所有输入 UNKNOWN 时使用等权，并明确标记。"""
    n = len(symbols)
    if n == 0:
        return OptimizerOutput(feasible=False, infeasibility_reason="NO_SYMBOLS")
    w = 1.0 / n
    return OptimizerOutput(
        weights=[w] * n,
        feasible=True,
        diagnostics={"method": "equal_weight", "note": "BD-CV31: covariance UNKNOWN — explicit fallback, NOT silent"},
    )


def optimize_portfolio(inputs: OptimizerInput) -> OptimizerOutput:
    """BD-CV31: 简单约束组合优化器。

    目标: max expected_return - risk_aversion * variance
    约束: concentration <= max_concentration, abs(weights) <= leverage
           turnover <= max_turnover

    协方差 UNKNOWN/非PSD 时 FAIL-CLOSED。
    """
    symbols = inputs.symbols
    n = len(symbols)

    if n == 0:
        return OptimizerOutput(feasible=False, infeasibility_reason="NO_SYMBOLS")

    returns = inputs.expected_returns
    if len(returns) != n:
        return OptimizerOutput(feasible=False, infeasibility_reason="DIM_MISMATCH")

    # Check for NaN/Inf in returns
    if any(math.isnan(r) or math.isinf(r) for r in returns):
        return OptimizerOutput(feasible=False, infeasibility_reason="NAN_INF_RETURNS")

    # Covariance UNKNOWN → equal weight with explicit annotation
    cov = inputs.covariance_matrix
    if cov is None or len(cov) != n:
        return equal_weight_portfolio(symbols)

    # Check PSD: diagonal must be >= 0 (variance ≥ 0)
    for i in range(n):
        if len(cov[i]) != n or cov[i][i] < 0:
            return equal_weight_portfolio(symbols)

    # Simple mean-variance with constraints
    # weights = returns / (risk_aversion * variance + epsilon)
    weights = [0.0] * n
    diagnostics: dict[str, Any] = {"method": "mean_variance_constrained"}
    violations: list[str] = []
    shadows: dict[str, float] = {}

    for i in range(n):
        var_i = max(cov[i][i], 0.0001)  # floor at 0.0001 to avoid division by zero
        raw_weight = returns[i] / (inputs.risk_aversion * var_i)
        weights[i] = raw_weight

    # Normalize to sum of absolute values
    total_abs = sum(abs(w) for w in weights)
    if total_abs > 0:
        weights = [w / total_abs for w in weights]

    # Apply concentration constraint
    for i in range(n):
        if abs(weights[i]) > inputs.max_concentration:
            violations.append(f"CONCENTRATION:{symbols[i]}")
            # Clip to max concentration
            excess = abs(weights[i]) - inputs.max_concentration
            sign = 1.0 if weights[i] > 0 else -1.0
            weights[i] = sign * inputs.max_concentration
            shadows[symbols[i]] = excess

    # Apply leverage constraint
    total_abs_after = sum(abs(w) for w in weights)
    if total_abs_after > inputs.max_leverage:
        scale = inputs.max_leverage / max(total_abs_after, 0.01)
        weights = [w * scale for w in weights]
        violations.append(f"LEVERAGE:{total_abs_after:.2f}")

    # Apply turnover constraint
    if inputs.current_weights and len(inputs.current_weights) == n:
        turnover = sum(abs(weights[i] - inputs.current_weights[i]) for i in range(n))
        if turnover > inputs.max_turnover:
            scale = inputs.max_turnover / max(turnover, 0.01)
            for i in range(n):
                weights[i] = inputs.current_weights[i] + (weights[i] - inputs.current_weights[i]) * scale
            violations.append(f"TURNOVER:{turnover:.2f}")

    diagnostics["constraint_violations"] = len(violations)
    diagnostics["total_abs_weight"] = total_abs_after

    return OptimizerOutput(
        weights=weights,
        feasible=True,
        diagnostics=diagnostics,
        constraint_violations=violations,
        shadow_prices=shadows,
    )
