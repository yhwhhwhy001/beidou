"""均值回归算法 — BD-05 items 4,5。

Robust z-score、half-life、no-trade band、regime gate、cost gate、volatility scaling。
多周期动量过滤：使用趋势斜率、持久性和自适应分位数，不再作为方向信号。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId

from .contracts import AlphaForecast
from .mean_reversion_math import estimate_half_life as estimate_half_life_result


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

    def __init__(
        self,
        half_life_window: int = 100,
        z_threshold: float = 1.5,
        no_trade_band: float | None = None,
        cost_margin: float | None = None,
    ):
        self._window = half_life_window
        self._z_threshold = z_threshold
        self._no_trade_band = no_trade_band if no_trade_band is not None else z_threshold
        self._cost_margin = cost_margin if cost_margin is not None else 2.0

    def compute_z_score(self, price: float, prices: list[float]) -> float:
        """Robust z-score = (price - median) / MAD。"""
        if (
            len(prices) < 20
            or not math.isfinite(price)
            or price <= 0
            or any(not math.isfinite(value) or value <= 0 for value in prices)
        ):
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
        """Return the consolidated causal AR(1) half-life in bars."""
        if len(prices) < 20:
            return 0.0
        return estimate_half_life_result(prices).half_life_bars

    def evaluate(
        self,
        price: float,
        prices: list[float],
        volatility: float,
        estimated_cost_bps: float,
        market_regime: str = "RANGING",
    ) -> ZScoreResult:
        """综合评估。"""
        if (
            not math.isfinite(volatility)
            or volatility < 0
            or not math.isfinite(estimated_cost_bps)
            or estimated_cost_bps < 0
        ):
            return ZScoreResult(0.0, 0.0, self._no_trade_band, False, False, "NO_ACTION", 0.0, 0.0)
        z_score = self.compute_z_score(price, prices)
        half_life = self.estimate_half_life(prices)

        # No-trade band: z-score within [-threshold, +threshold]
        no_trade_band = self._no_trade_band
        in_band = abs(z_score) < no_trade_band

        # Regime gate: only trade in RANGING markets
        regime_allowed = market_regime in ("RANGING", "UNKNOWN")

        # Cost gate: expected return must exceed cost
        expected_return_bps = abs(z_score) * volatility * 100  # rough estimate
        cost_viable = expected_return_bps > estimated_cost_bps * self._cost_margin

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
        if any(period <= 0 for period in self._periods):
            raise ValueError("momentum periods must be positive")

    def evaluate(self, prices: list[float], volatility: float) -> MomentumResult:
        """多周期动量评估 (P1-007: 波动率归一化)。"""
        if (
            len(prices) < max(self._periods) + 1
            or not math.isfinite(volatility)
            or volatility < 0
            or any(not math.isfinite(price) or price <= 0 for price in prices)
        ):
            return MomentumResult("FLAT", 0.0, 0.0, False, "VETO")

        # P1-007: 使用波动率归一化阈值，替代绝对 0.1%
        vol = max(volatility, 0.001)  # 最低日波动 0.1%
        vol_threshold = vol * 0.3  # 0.3 倍日波动作为方向判定阈值

        returns_by_period = {}
        for period in self._periods:
            ret = (prices[-1] / prices[-period - 1] - 1) if prices[-period - 1] > 0 else 0
            returns_by_period[period] = ret

        # P1-007: 波动率归一化方向判定（对称处理多空）
        up_count = sum(1 for r in returns_by_period.values() if r > vol_threshold)
        down_count = sum(1 for r in returns_by_period.values() if r < -vol_threshold)

        if up_count > down_count:
            direction = "UP"
        elif down_count > up_count:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # P1-007: 强度改为波动率归一化（不再用任意 *20 缩放）
        returns = list(returns_by_period.values())
        avg_ret = sum(returns) / len(returns)
        strength = min(0.7, abs(avg_ret) / vol)

        # Persistence: consecutive same-direction periods
        signs = [1 if r > vol_threshold else -1 if r < -vol_threshold else 0 for r in returns]
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
        if strength < 0.1:  # P1-007: 归一化后的阈值
            return MomentumResult(direction, strength, persistence, False, "DEGRADE")

        return MomentumResult(direction, strength, persistence, False, "ACCEPT")


class MeanReversionAlpha:
    """Forecast-layer adapter for the robust mean-reversion engine.

    The adapter intentionally does not invent an expected return. That value
    must come from a later calibration artifact; the legacy z-score remains a
    raw score and direction source only.
    """

    def __init__(self, *, alpha_id: str = "mean_reversion_v3", policy_version: str = "mr-alpha-v3-policy-v1") -> None:
        self.alpha_id = alpha_id
        self.policy_version = policy_version
        self.engine = MeanReversionEngine()

    @staticmethod
    def _timestamp(context: Mapping[str, Any], features: Mapping[str, Any]) -> datetime:
        value = context.get("timestamp", features.get("timestamp"))
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            value = datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    def _costs(
        context: Mapping[str, Any], features: Mapping[str, Any]
    ) -> tuple[float | None, float | None, float | None, str]:
        raw = context.get("costs", features.get("costs"))
        if not isinstance(raw, Mapping):
            raw = context
        values: list[float | None] = []
        for name in ("expected_fee_bps", "expected_slippage_bps", "expected_funding_bps"):
            try:
                value = float(raw.get(name))
            except (TypeError, ValueError):
                value = None
            values.append(value if value is not None and math.isfinite(value) else None)
        source_hash = str(raw.get("source_hash", "")).strip()
        if any(value is None for value in values) or not source_hash:
            return None, None, None, ""
        return values[0], values[1], values[2], source_hash

    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        features = context.get("features", context)
        if not isinstance(features, Mapping):
            features = {}
        prices_raw = features.get("prices", [])
        prices: list[float] = []
        if isinstance(prices_raw, (list, tuple)):
            for value in prices_raw:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    prices = []
                    break
                if not math.isfinite(parsed) or parsed <= 0:
                    prices = []
                    break
                prices.append(parsed)
        try:
            close = float(features.get("close", prices[-1] if prices else float("nan")))
            volatility = float(features.get("realized_volatility", features.get("ann_volatility", float("nan"))))
        except (TypeError, ValueError):
            close = float("nan")
            volatility = float("nan")
        fee, slippage, funding, source_hash = self._costs(context, features)
        known_cost = fee is not None and slippage is not None and funding is not None and bool(source_hash)
        if known_cost:
            assert fee is not None and slippage is not None and funding is not None
            cost_bps = fee + slippage + funding
        else:
            cost_bps = float("nan")
        regime = str(context.get("market_regime", features.get("market_regime", "UNKNOWN"))).upper()
        result = self.engine.evaluate(close, prices, volatility, cost_bps, regime)
        side: str | OrderSide = {
            "LONG": OrderSide.BUY,
            "SHORT": OrderSide.SELL,
        }.get(result.signal_direction, "NO_ACTION")
        raw_score = (
            result.strength
            if result.signal_direction == "LONG"
            else -result.strength
            if result.signal_direction == "SHORT"
            else 0.0
        )
        calibrated = features.get("calibrated_expected_return", context.get("calibrated_expected_return"))
        try:
            expected_return = float(calibrated) if calibrated is not None else None
        except (TypeError, ValueError):
            expected_return = None
        if expected_return is not None and not math.isfinite(expected_return):
            expected_return = None
        after_cost = None
        if expected_return is not None and known_cost:
            after_cost = expected_return - float(cost_bps) / 10000.0
        feature_hash = str(features.get("feature_hash", "")).strip()
        if not feature_hash:
            feature_hash = hashlib.sha256(
                json.dumps(prices, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()[:16]
        liquidity = features.get("liquidity_score", features.get("liquidity"))
        try:
            capacity_score = min(1.0, max(0.0, float(liquidity)))
        except (TypeError, ValueError):
            capacity_score = None
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=context.get("strategy_id", StrategyId("mean-reversion-v3")),
            instrument_id=context.get("instrument_id", InstrumentId("UNKNOWN")),
            venue_id=context.get("venue_id", VenueId("UNKNOWN")),
            side=side,
            raw_score=raw_score,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            expected_volatility=volatility if math.isfinite(volatility) and volatility >= 0 else None,
            horizon_seconds=3600,
            probability_positive=0.5 + 0.5 * raw_score,
            confidence=result.confidence,
            uncertainty=max(0.0, min(1.0, 1.0 - result.confidence)),
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=None,
            regime_fit=0.9 if regime == "RANGING" else 0.2 if regime in {"TRENDING_UP", "TRENDING_DOWN"} else 0.0,
            capacity_score=capacity_score,
            model_version=SchemaVersion("3.0.0"),
            policy_version=self.policy_version,
            feature_hash=feature_hash or "UNKNOWN",
            timestamp=self._timestamp(context, features),
            cost_source_hash=source_hash,
            schema_version=SchemaVersion("3.0.0"),
        )

    def generate(self, context: Mapping[str, Any]) -> AlphaForecast:
        return self.generate_forecast(context)

    def validate(self) -> bool:
        return bool(self.alpha_id and self.policy_version)
