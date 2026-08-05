"""均值回归算法 — BD-05 items 4,5。

Robust z-score、half-life、no-trade band、regime gate、cost gate、volatility scaling。
多周期动量过滤：使用趋势斜率、持久性和自适应分位数，不再作为方向信号。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ZScoreResult:
    z_score: float
    half_life_hours: float
    no_trade_band_pct: float
    regime_allowed: bool
    cost_viable: bool
    signal_direction: str  # LONG / SHORT / NO_ACTION
    strength: float
    confidence: float


class MeanReversionEngine:
    """Robust 均值回归引擎。

    特性:
    - z-score with robust statistics (median/MAD)
    - half-life estimation via OLS log-return regression
    - no-trade band (z-score within band = no action)
    - regime gate (trending markets skip mean reversion)
    - cost gate (expected return must exceed costs)
    - volatility scaling (size inversely to vol)
    """

    def __init__(self, half_life_window: int = 100, z_threshold: float = 1.5):
        self._window = half_life_window
        self._z_threshold = z_threshold

    def compute_z_score(self, price: float, prices: list[float]) -> float:
        """Robust z-score = (price - median) / MAD。"""
        if len(prices) < 20:
            return 0.0

        n = len(prices)
        sorted_prices = sorted(prices)
        median = sorted_prices[n // 2]

        # MAD = median absolute deviation
        deviations = sorted(abs(p - median) for p in prices)
        mad = deviations[n // 2]
        if mad == 0:
            return 0.0

        return (price - median) / (mad * 1.4826)  # 1.4826 = normal consistency constant

    def estimate_half_life(self, prices: list[float]) -> float:
        """OLS 对数回归估算半衰期（小时）。"""
        if len(prices) < 20:
            return 0.0

        y = [math.log(p) for p in prices[1:]]
        x = [math.log(p) for p in prices[:-1]]

        n = len(y)
        x_mean = sum(x) / n
        y_mean = sum(y) / n

        # OLS slope
        num = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
        den = sum((x[i] - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0

        slope = num / den
        if slope <= 0:
            return float("inf")  # No mean reversion

        return -math.log(2) / slope

    def evaluate(
        self,
        price: float,
        prices: list[float],
        volatility: float,
        estimated_cost_bps: float,
        market_regime: str = "RANGING",
    ) -> ZScoreResult:
        """综合评估。"""
        z_score = self.compute_z_score(price, prices)
        half_life = self.estimate_half_life(prices)

        # No-trade band: z-score within [-threshold, +threshold]
        no_trade_band = self._z_threshold
        in_band = abs(z_score) < no_trade_band

        # Regime gate: only trade in RANGING markets
        regime_allowed = market_regime in ("RANGING", "UNKNOWN")

        # Cost gate: expected return must exceed cost
        expected_return_bps = abs(z_score) * volatility * 100  # rough estimate
        cost_viable = expected_return_bps > estimated_cost_bps * 2  # 2x safety margin

        # Direction
        if in_band or not regime_allowed or not cost_viable:
            direction = "NO_ACTION"
            strength = 0.0
            confidence = 0.0
        elif z_score < -no_trade_band:
            direction = "LONG"  # Oversold → buy
            strength = min(0.9, abs(z_score) / 5.0)
            confidence = 0.5 + strength * 0.3
        else:
            direction = "SHORT"  # Overbought → sell
            strength = min(0.9, abs(z_score) / 5.0)
            confidence = 0.5 + strength * 0.3

        return ZScoreResult(
            z_score=z_score,
            half_life_hours=half_life,
            no_trade_band_pct=no_trade_band,
            regime_allowed=regime_allowed,
            cost_viable=cost_viable,
            signal_direction=direction,
            strength=strength,
            confidence=confidence,
        )


@dataclass
class MomentumResult:
    """多周期动量过滤结果。"""

    trend_direction: str  # UP / DOWN / FLAT
    trend_strength: float  # 0-1
    persistence: float  # 连续同向周期比
    volatility_override: bool  # 高波动时否决
    filter_decision: str  # ACCEPT / VETO / DEGRADE


class MultiPeriodMomentum:
    """多周期动量过滤器 — BD-05 item 5。

    使用多周期收益、趋势斜率、持久性和自适应分位数。
    不再作为方向信号——只做过滤。
    """

    def __init__(self, periods: list[int] | None = None):
        self._periods = periods or [5, 10, 20, 50]

    def evaluate(self, prices: list[float], volatility: float) -> MomentumResult:
        """多周期动量评估。"""
        if len(prices) < max(self._periods) + 1:
            return MomentumResult("FLAT", 0.0, 0.0, False, "VETO")

        returns_by_period = {}
        for period in self._periods:
            if len(prices) > period:
                ret = (prices[-1] / prices[-period - 1] - 1) if prices[-period - 1] > 0 else 0
                returns_by_period[period] = ret

        # Trend direction: majority vote
        up_count = sum(1 for r in returns_by_period.values() if r > 0.001)
        down_count = sum(1 for r in returns_by_period.values() if r < -0.001)

        if up_count > down_count:
            direction = "UP"
        elif down_count > up_count:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # Strength: average return across periods
        returns = list(returns_by_period.values())
        strength = min(0.7, abs(sum(returns)) / len(returns) * 20 if returns else 0)

        # Persistence: consecutive same-direction periods
        signs = [1 if r > 0 else -1 if r < 0 else 0 for r in returns]
        persistence = 0.0
        for i in range(1, len(signs)):
            if signs[i] == signs[i - 1] and signs[i] != 0:
                persistence += 1
        persistence = persistence / max(1, len(signs) - 1)

        # Volatility override
        vol_override = volatility > 0.5
        if vol_override:
            return MomentumResult(direction, strength, persistence, True, "VETO")

        # Filter decision
        if strength < 0.01:
            return MomentumResult(direction, strength, persistence, False, "DEGRADE")

        return MomentumResult(direction, strength, persistence, False, "ACCEPT")
