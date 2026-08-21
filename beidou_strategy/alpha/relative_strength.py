"""Point-in-time benchmark-relative cross-sectional Alpha."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId

from ._forecast_utils import (
    clip,
    context_features,
    feature_payload,
    finite,
    horizon_values,
    point_in_time_universe,
    quality_factor,
    resolve_costs,
    resolve_timestamp,
    side_for_score,
    stable_hash,
)
from .contracts import AlphaForecast


@dataclass(frozen=True, slots=True)
class RelativeStrengthPolicy:
    policy_version: str = "relative-strength-policy-v3-v1"
    horizons: tuple[int, ...] = (5, 20)
    horizon_weights: tuple[float, ...] = (0.4, 0.6)
    entry_threshold: float = 0.2
    score_scale: float = 0.08
    expected_return_scale: float = 0.02
    horizon_seconds: int = 3600

    def __post_init__(self) -> None:
        if len(self.horizons) != len(self.horizon_weights) or not self.horizons:
            raise ValueError("relative-strength horizons and weights must align")
        if any(horizon <= 0 for horizon in self.horizons) or sum(self.horizon_weights) <= 0:
            raise ValueError("relative-strength horizons must be positive")
        if any(weight < 0 or not math.isfinite(weight) for weight in self.horizon_weights):
            raise ValueError("relative-strength weights must be finite and non-negative")
        if self.score_scale <= 0 or self.expected_return_scale < 0 or self.horizon_seconds <= 0:
            raise ValueError("relative-strength policy scales are invalid")


class RelativeStrengthAlpha:
    def __init__(
        self,
        policy: RelativeStrengthPolicy | None = None,
        *,
        alpha_id: str = "relative-strength-v3",
        strategy_id: StrategyId | None = None,
    ) -> None:
        self.policy = policy or RelativeStrengthPolicy()
        self.alpha_id = alpha_id
        self.strategy_id = strategy_id or StrategyId("relative-strength-v3")

    @staticmethod
    def _universe(features: Mapping[str, Any]) -> Mapping[str, Any] | None:
        value = features.get("universe_returns", features.get("universe"))
        return value if isinstance(value, Mapping) else None

    def _base_identity(self, context: Mapping[str, Any]) -> tuple[StrategyId, InstrumentId, VenueId]:
        return (
            context.get("strategy_id", self.strategy_id),
            context.get("instrument_id", InstrumentId("UNKNOWN")),
            context.get("venue_id", VenueId("UNKNOWN")),
        )

    def _feature_hash(self, features: Mapping[str, Any]) -> str:
        supplied = str(features.get("feature_hash", "")).strip()
        allowed = {
            "asset_returns",
            "benchmark_returns",
            "universe_returns",
            "universe_snapshot_timestamp",
            "universe_source_hash",
            "realized_volatility",
            "data_quality",
            "liquidity_score",
            "market_beta",
        }
        return supplied or stable_hash(feature_payload(features, allowed))

    def _not_verifiable(self, context: Mapping[str, Any], features: Mapping[str, Any]) -> AlphaForecast:
        strategy_id, instrument_id, venue_id = self._base_identity(context)
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        volatility = finite(features.get("realized_volatility", features.get("ann_volatility")))
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            side="NO_ACTION",
            raw_score=0.0,
            expected_return=None,
            expected_return_after_cost=None,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.policy.horizon_seconds,
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
            policy_version=self.policy.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        features = context_features(context)
        asset, asset_invalid = horizon_values(features, ("asset_returns", "returns_by_horizon"))
        benchmark, benchmark_invalid = horizon_values(features, ("benchmark_returns",))
        universe = self._universe(features)
        if (
            asset_invalid
            or benchmark_invalid
            or not asset
            or not benchmark
            or universe is None
            or not point_in_time_universe(context, features)
            or not isinstance(features.get("instrument_id", context.get("instrument_id", "")), str)
        ):
            return self._not_verifiable(context, features)
        instrument = str(context.get("instrument_id", features.get("instrument_id", "")))
        if instrument not in universe:
            return self._not_verifiable(context, features)
        primary_horizon = self.policy.horizons[-1]
        scores: dict[str, float] = {}
        for symbol, raw in universe.items():
            if not isinstance(raw, Mapping):
                return self._not_verifiable(context, features)
            symbol_returns, invalid = horizon_values({"value": raw}, ("value",))
            if invalid:
                return self._not_verifiable(context, features)
            if primary_horizon not in symbol_returns or primary_horizon not in benchmark:
                return self._not_verifiable(context, features)
            scores[str(symbol)] = symbol_returns[primary_horizon] - benchmark[primary_horizon]
        sorted_symbols = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
        rank_index = sorted_symbols.index(instrument)
        rank_score = 2.0 * rank_index / max(1, len(sorted_symbols) - 1) - 1.0
        weights = dict(zip(self.policy.horizons, self.policy.horizon_weights, strict=True))
        available = [horizon for horizon in self.policy.horizons if horizon in asset and horizon in benchmark]
        if not available:
            return self._not_verifiable(context, features)
        total_weight = sum(weights[horizon] for horizon in available)
        relative = (
            sum(
                math.tanh((asset[horizon] - benchmark[horizon]) / self.policy.score_scale) * weights[horizon]
                for horizon in available
            )
            / total_weight
        )
        raw_score = clip(0.75 * relative + 0.25 * rank_score, -1.0, 1.0)
        fee, slippage, funding, source_hash = resolve_costs(context, features)
        expected_return = raw_score * self.policy.expected_return_scale
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
            side=side_for_score(raw_score, self.policy.entry_threshold),
            raw_score=raw_score,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            expected_volatility=volatility if volatility is not None and volatility >= 0 else None,
            horizon_seconds=self.policy.horizon_seconds,
            probability_positive=1.0 / (1.0 + math.exp(-raw_score / 0.25)),
            confidence=confidence,
            uncertainty=1.0 - confidence,
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=finite(features.get("market_beta")),
            regime_fit=clip(abs(raw_score), 0.0, 1.0),
            capacity_score=clip(liquidity, 0.0, 1.0) if liquidity is not None else None,
            model_version=SchemaVersion("3.0.0"),
            policy_version=self.policy.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=resolve_timestamp(context, features),
            cost_source_hash=source_hash,
        )

    def generate(self, context: Mapping[str, Any]) -> AlphaForecast:
        return self.generate_forecast(context)

    def validate(self) -> bool:
        return bool(self.alpha_id and self.policy.policy_version)
