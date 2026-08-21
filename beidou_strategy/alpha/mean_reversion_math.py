"""Single production implementation of robust mean-reversion math.

Both the V2 compatibility entry and the V3 Alpha adapter import these
functions.  The old ``components.mean_reversion_fixed`` path only re-exports
them so it cannot develop a second half-life or z-score semantic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class HalfLifeResult:
    half_life_bars: float
    ar1_coefficient: float
    beta: float
    is_mean_reverting: bool
    r_squared: float
    sample_count: int
    valid: bool


def _invalid(sample_count: int) -> HalfLifeResult:
    return HalfLifeResult(
        half_life_bars=float("inf"),
        ar1_coefficient=1.0,
        beta=0.0,
        is_mean_reverting=False,
        r_squared=0.0,
        sample_count=sample_count,
        valid=False,
    )


def estimate_half_life(prices: list[float]) -> HalfLifeResult:
    """Estimate OU/AR(1) half-life with one deterministic formula."""
    if len(prices) < 30 or any(not math.isfinite(price) or price <= 0 for price in prices):
        return _invalid(len(prices))
    log_prices = [math.log(price) for price in prices]
    sample_count = len(log_prices) - 1
    x = log_prices[:-1]
    y = [log_prices[index + 1] - log_prices[index] for index in range(sample_count)]
    mean_x = sum(x) / sample_count
    mean_y = sum(y) / sample_count
    var_x = sum((value - mean_x) ** 2 for value in x)
    if var_x <= 1e-15:
        return _invalid(sample_count)
    beta = sum((x[index] - mean_x) * (y[index] - mean_y) for index in range(sample_count)) / var_x
    intercept = mean_y - beta * mean_x
    predicted = [intercept + beta * x[index] for index in range(sample_count)]
    ss_res = sum((y[index] - predicted[index]) ** 2 for index in range(sample_count))
    ss_tot = sum((value - mean_y) ** 2 for value in y)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    phi = 1.0 + beta
    is_mean_reverting = beta < 0 and 0 < phi < 1
    half_life = -math.log(2.0) / math.log(phi) if is_mean_reverting else float("inf")
    valid = is_mean_reverting and half_life < 500.0 and r_squared > 0.01
    return HalfLifeResult(
        half_life_bars=half_life,
        ar1_coefficient=phi,
        beta=beta,
        is_mean_reverting=is_mean_reverting,
        r_squared=r_squared,
        sample_count=sample_count,
        valid=valid,
    )


def robust_zscore(values: list[float], window: int = 20) -> list[float]:
    """Causal median/MAD z-score; the current value is not in its window."""
    if window <= 0:
        raise ValueError("window must be positive")
    result = [0.0] * len(values)
    for index in range(window, len(values)):
        window_values = values[index - window : index]
        sorted_values = sorted(window_values)
        median = sorted_values[len(sorted_values) // 2]
        deviations = sorted(abs(value - median) for value in window_values)
        mad = deviations[len(deviations) // 2]
        if mad > 1e-15:
            result[index] = (values[index] - median) / mad
    return result
