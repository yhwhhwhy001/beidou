"""BF-09: 修复 half-life 公式的均值回归算法。

当前实现的错误:
  half_life = -ln(2) / slope  (回归 log(P_t) 对 log(P_{t-1}))
  对于正 AR(1) 系数会产生负半衰期。

正确方法:
  Δlog(P_t) = α + β log(P_{t-1}) + ε_t
  half_life = -ln(2) / β, 需要 β < 0

  或先估计 AR(1) 系数 φ, 再计算 -ln(2)/ln(φ), 并处理 0 < φ < 1。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class HalfLifeResult:
    """半衰期估计结果。"""

    half_life_bars: float  # 半衰期（以 K 线数计）
    ar1_coefficient: float  # AR(1) 系数 φ
    beta: float  # 回归系数 β
    is_mean_reverting: bool  # 是否均值回归（β < 0 且 φ < 1）
    r_squared: float  # 回归 R²
    sample_count: int
    valid: bool  # 估计是否有效


def estimate_half_life(prices: list[float]) -> HalfLifeResult:
    """正确估计均值回归半衰期。

    使用 OU 过程离散化：
        Δlog(P_t) = α + β log(P_{t-1}) + ε_t

    如果 β < 0，价格有均值回归倾向。
    half_life = -ln(2) / β

    同时估计 AR(1) 系数 φ, 用于验证:
        φ ≈ 1 + β (对于小时间步长)
        half_life = -ln(2) / ln(φ)  (需要 0 < φ < 1)

    Args:
        prices: 按时间排序的价格序列

    Returns:
        HalfLifeResult
    """
    n = len(prices)
    if n < 30:
        return HalfLifeResult(
            half_life_bars=float("inf"),
            ar1_coefficient=1.0,
            beta=0.0,
            is_mean_reverting=False,
            r_squared=0.0,
            sample_count=n,
            valid=False,
        )

    # 计算对数价格
    log_prices = [math.log(p) for p in prices if p > 0]
    if len(log_prices) < 30:
        return HalfLifeResult(
            half_life_bars=float("inf"),
            ar1_coefficient=1.0,
            beta=0.0,
            is_mean_reverting=False,
            r_squared=0.0,
            sample_count=len(log_prices),
            valid=False,
        )

    # 构造 Δlog(P_t) 和 log(P_{t-1})
    m = len(log_prices) - 1
    y = [log_prices[t + 1] - log_prices[t] for t in range(m)]  # Δlog(P_t)
    x = [log_prices[t] for t in range(m)]  # log(P_{t-1})

    # OLS 回归: y = α + β x + ε
    mean_x = sum(x) / m
    mean_y = sum(y) / m

    cov_xy = sum((x[t] - mean_x) * (y[t] - mean_y) for t in range(m))
    var_x = sum((xt - mean_x) ** 2 for xt in x)

    if abs(var_x) < 1e-15:
        return HalfLifeResult(
            half_life_bars=float("inf"),
            ar1_coefficient=1.0,
            beta=0.0,
            is_mean_reverting=False,
            r_squared=0.0,
            sample_count=m,
            valid=False,
        )

    beta = cov_xy / var_x
    alpha = mean_y - beta * mean_x

    # R-squared
    y_pred = [alpha + beta * x[t] for t in range(m)]
    ss_res = sum((y[t] - y_pred[t]) ** 2 for t in range(m))
    ss_tot = sum((yt - mean_y) ** 2 for yt in y)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # AR(1) 系数
    # log(P_t) = φ log(P_{t-1}) + c + ε_t
    # φ = 1 + β
    phi = 1.0 + beta

    # 半衰期计算
    is_mr = beta < 0 and 0 < phi < 1

    if is_mr:
        # 通过 β: half_life = -ln(2) / β
        half_life_beta = -math.log(2) / beta
        # 通过 φ: half_life = -ln(2) / ln(φ)
        if 0 < phi < 1:
            half_life_phi = -math.log(2) / math.log(phi)
            # 取两者平均值（通常很接近）
            half_life_bars = (half_life_beta + half_life_phi) / 2.0
        else:
            half_life_bars = half_life_beta
    else:
        half_life_bars = float("inf")

    valid = is_mr and half_life_bars < 500 and r_squared > 0.01

    return HalfLifeResult(
        half_life_bars=half_life_bars,
        ar1_coefficient=phi,
        beta=beta,
        is_mean_reverting=is_mr,
        r_squared=r_squared,
        sample_count=m,
        valid=valid,
    )


def robust_zscore(values: list[float], window: int = 20) -> list[float]:
    """鲁棒 z-score：使用 median 和 MAD 而非 mean 和 std。

    robust_zscore = (x - median) / MAD
    MAD = median(|x - median|)
    """
    n = len(values)
    result = [0.0] * n

    for i in range(window, n):
        window_vals = values[i - window : i]
        w_n = len(window_vals)

        sorted_vals = sorted(window_vals)
        median = sorted_vals[w_n // 2]

        abs_devs = sorted([abs(v - median) for v in window_vals])
        mad = abs_devs[w_n // 2]

        if mad > 1e-15:
            result[i] = (values[i] - median) / mad
        else:
            result[i] = 0.0

    return result
