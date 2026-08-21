"""ATR-normalized breakout Alpha with confirmation and false-break penalties."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId

from ._forecast_utils import (
    clip,
    context_features,
    feature_payload,
    finite,
    quality_factor,
    resolve_costs,
    resolve_timestamp,
    side_for_score,
    stable_hash,
)
from .contracts import AlphaForecast


class BreakoutAlpha:
    def __init__(
        self,
        *,
        alpha_id: str = "breakout-v3",
        strategy_id: StrategyId | None = None,
        policy_version: str = "breakout-policy-v3-v1",
        entry_threshold: float = 0.2,
        expected_return_scale: float = 0.02,
        horizon_seconds: int = 3600,
    ) -> None:
        if (
            not policy_version.strip()
            or not 0 < entry_threshold <= 1
            or expected_return_scale < 0
            or horizon_seconds <= 0
        ):
            raise ValueError("invalid breakout policy")
        self.alpha_id = alpha_id
        self.strategy_id = strategy_id or StrategyId("breakout-v3")
        self.policy_version = policy_version
        self.entry_threshold = entry_threshold
        self.expected_return_scale = expected_return_scale
        self.horizon_seconds = horizon_seconds

    def _feature_hash(self, features: Mapping[str, Any]) -> str:
        supplied = str(features.get("feature_hash", "")).strip()
        allowed = {
            "close",
            "high_history",
            "low_history",
            "atr",
            "volume_ratio",
            "volume_expansion",
            "breadth",
            "breakout_breadth",
            "realized_volatility",
            "data_quality",
            "liquidity_score",
            "liquidity",
        }
        return supplied or stable_hash(feature_payload(features, allowed))

    def _not_verifiable(self, context: Mapping[str, Any], features: Mapping[str, Any]) -> AlphaForecast:
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        volatility = finite(features.get("realized_volatility", features.get("ann_volatility")))
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=context.get("strategy_id", self.strategy_id),
            instrument_id=context.get("instrument_id", InstrumentId("UNKNOWN")),
            venue_id=context.get("venue_id", VenueId("UNKNOWN")),
            side="NO_ACTION",
            raw_score=0.0,
            expected_return=None,
            expected_return_after_cost=None,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.horizon_seconds,
            probability_positive=0.5,
            confidence=0.0,
            uncertainty=1.0,
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=finite(features.get("market_beta")),
            regime_fit=0.0,
            capacity_score=None,
            model_version=SchemaVersion("3.0.0"),
            policy_version=self.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        features = context_features(context)
        close = finite(features.get("close", features.get("price")))
        atr = finite(features.get("atr", features.get("atr_n")))
        highs = features.get("high_history", features.get("highs"))
        lows = features.get("low_history", features.get("lows"))
        if (
            close is None
            or close <= 0
            or atr is None
            or atr <= 0
            or not isinstance(highs, (list, tuple))
            or not isinstance(lows, (list, tuple))
            or len(highs) < 2
            or len(lows) < 2
        ):
            return self._not_verifiable(context, features)
        high_values = [finite(value) for value in highs]
        low_values = [finite(value) for value in lows]
        if any(value is None or value <= 0 for value in high_values + low_values):
            return self._not_verifiable(context, features)
        prior_high = max(value for value in high_values if value is not None)
        prior_low = min(value for value in low_values if value is not None)
        up_distance = (close - prior_high) / atr
        down_distance = (prior_low - close) / atr
        direction_distance = up_distance if up_distance >= down_distance else -down_distance
        normalized_distance = clip(direction_distance / 2.0, -1.0, 1.0)
        volume = finite(features.get("volume_ratio", features.get("volume_expansion")))
        breadth = finite(features.get("breadth"))
        breakout_breadth = finite(features.get("breakout_breadth", breadth))
        if (
            volume is None
            or breadth is None
            or breakout_breadth is None
            or not 0 <= breadth <= 1
            or not 0 <= breakout_breadth <= 1
        ):
            return self._not_verifiable(context, features)
        volume_confirmation = clip((volume - 1.0) / 1.0, -1.0, 1.0)
        breadth_confirmation = 2.0 * ((breadth + breakout_breadth) / 2.0) - 1.0
        confirmation = clip(0.65 * max(0.0, volume_confirmation) + 0.35 * max(0.0, breadth_confirmation), 0.0, 1.0)
        false_break_penalty = clip(
            0.65 * max(0.0, -volume_confirmation) + 0.35 * max(0.0, -breadth_confirmation), 0.0, 1.0
        )
        raw_score = clip(normalized_distance * (0.5 + 0.5 * confirmation) * (1.0 - false_break_penalty), -1.0, 1.0)
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        expected_return = raw_score * self.expected_return_scale
        total_cost = None if fee is None or slippage is None or funding is None else fee + slippage + funding
        after_cost = expected_return - total_cost / 10000.0 if total_cost is not None and source_hash else None
        volatility = finite(features.get("realized_volatility", features.get("ann_volatility")))
        liquidity = finite(features.get("liquidity_score", features.get("liquidity")))
        confidence = clip(abs(raw_score) * quality_factor(features), 0.0, 1.0)
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=context.get("strategy_id", self.strategy_id),
            instrument_id=context.get("instrument_id", InstrumentId("UNKNOWN")),
            venue_id=context.get("venue_id", VenueId("UNKNOWN")),
            side=side_for_score(raw_score, self.entry_threshold),
            raw_score=raw_score,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.horizon_seconds,
            probability_positive=1.0 / (1.0 + math.exp(-raw_score / 0.25)),
            confidence=confidence,
            uncertainty=1.0 - confidence,
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=finite(features.get("market_beta")),
            regime_fit=clip(abs(raw_score) * (0.5 + 0.5 * confirmation), 0.0, 1.0),
            capacity_score=clip(liquidity, 0.0, 1.0) if liquidity is not None else None,
            model_version=SchemaVersion("3.0.0"),
            policy_version=self.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate(self, context: Mapping[str, Any]) -> AlphaForecast:
        return self.generate_forecast(context)

    def validate(self) -> bool:
        return bool(self.alpha_id and self.policy_version)
