"""Formal directional trend Alpha for the Alpha V3 forecast layer.

This module deliberately does not import mean-reversion code. Trend is an
independent entry source; fusion and risk remain downstream concerns.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId

from .contracts import AlphaForecast


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _stable_hash(value: object) -> str:
    def safe(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): safe(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(value) for value in item]
        if isinstance(item, float) and not math.isfinite(item):
            return f"NON_FINITE:{item!r}"
        return item

    payload = json.dumps(safe(value), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class TrendAlphaPolicy:
    """Versioned deterministic inference parameters."""

    policy_version: str = "trend-alpha-v3-policy-v1"
    horizons: tuple[int, ...] = (5, 20, 50)
    horizon_weights: tuple[float, ...] = (0.2, 0.3, 0.5)
    return_weight: float = 0.40
    slope_weight: float = 0.25
    persistence_weight: float = 0.10
    breadth_weight: float = 0.10
    breakout_weight: float = 0.10
    volume_weight: float = 0.05
    return_scale: float = 0.20
    slope_scale: float = 0.01
    entry_threshold: float = 0.20
    probability_temperature: float = 0.25
    expected_return_scale: float = 0.02
    horizon_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")
        if len(self.horizons) != len(self.horizon_weights) or not self.horizons:
            raise ValueError("horizons and horizon_weights must align")
        if any(horizon <= 0 for horizon in self.horizons):
            raise ValueError("horizons must be positive")
        if any(weight < 0 or not math.isfinite(weight) for weight in self.horizon_weights):
            raise ValueError("horizon_weights must be finite and non-negative")
        if sum(self.horizon_weights) <= 0:
            raise ValueError("horizon_weights must have positive mass")
        for name in (
            "return_weight",
            "slope_weight",
            "persistence_weight",
            "breadth_weight",
            "breakout_weight",
            "volume_weight",
        ):
            value = getattr(self, name)
            if value < 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.return_scale <= 0 or self.slope_scale <= 0 or self.probability_temperature <= 0:
            raise ValueError("inference scales must be positive")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")
        if self.expected_return_scale < 0 or not math.isfinite(self.expected_return_scale):
            raise ValueError("expected_return_scale must be finite and non-negative")
        if self.horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")


DEFAULT_TREND_ALPHA_POLICY = TrendAlphaPolicy()


def _context_features(context: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = context.get("features", context)
    return raw if isinstance(raw, Mapping) else {}


def _horizon_mapping(features: Mapping[str, Any], names: tuple[str, ...]) -> tuple[dict[int, float], bool]:
    for name in names:
        raw = features.get(name)
        if raw is None:
            continue
        if not isinstance(raw, Mapping) or not raw:
            return {}, True
        result: dict[int, float] = {}
        for horizon, value in raw.items():
            try:
                parsed_horizon = int(horizon)
            except (TypeError, ValueError):
                return {}, True
            parsed_value = _finite(value)
            if parsed_horizon <= 0 or parsed_value is None:
                return {}, True
            result[parsed_horizon] = parsed_value
        return result, False
    return {}, False


def _known_feature_payload(features: Mapping[str, Any]) -> dict[str, Any]:
    """Select only point-in-time inputs; future/label keys are ignored."""
    allowed = {
        "benchmark_returns",
        "returns_by_horizon",
        "trend_slopes",
        "trend_slope_by_horizon",
        "breadth",
        "breakout_breadth",
        "volume_expansion",
        "volume_ratio",
        "vol_ratio",
        "realized_volatility",
        "realized_vol",
        "ann_volatility",
        "liquidity_score",
        "liquidity",
        "data_quality",
        "quality",
        "market_beta",
    }
    return {key: features[key] for key in sorted(allowed & set(features))}


def _resolve_timestamp(context: Mapping[str, Any], features: Mapping[str, Any]) -> datetime:
    value = context.get("timestamp", features.get("timestamp"))
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def resolve_forecast_costs(
    context: Mapping[str, Any], features: Mapping[str, Any]
) -> tuple[float | None, float | None, float | None, str]:
    raw = context.get("costs", features.get("costs"))
    if not isinstance(raw, Mapping):
        raw = context
    values: list[float | None] = []
    for name in ("expected_fee_bps", "expected_slippage_bps", "expected_funding_bps"):
        value = _finite(raw.get(name))
        values.append(value)
    source_hash = str(raw.get("source_hash", "")).strip()
    if any(value is None for value in values) or not source_hash:
        return None, None, None, ""
    return values[0], values[1], values[2], source_hash


class TrendAlpha:
    """A deterministic directional entry alpha producing ``AlphaForecast``."""

    def __init__(
        self,
        policy: TrendAlphaPolicy | None = None,
        *,
        alpha_id: str = "trend_v3",
        strategy_id: StrategyId | None = None,
    ) -> None:
        self.policy = policy or DEFAULT_TREND_ALPHA_POLICY
        self.alpha_id = alpha_id
        self.strategy_id = strategy_id or StrategyId("trend-v3")

    def _base_identity(
        self, context: Mapping[str, Any], features: Mapping[str, Any]
    ) -> tuple[StrategyId, InstrumentId, VenueId]:
        return (
            context.get("strategy_id", self.strategy_id),
            context.get("instrument_id", InstrumentId("UNKNOWN")),
            context.get("venue_id", VenueId("UNKNOWN")),
        )

    def _feature_hash(self, features: Mapping[str, Any]) -> str:
        supplied = str(features.get("feature_hash", "")).strip()
        return supplied or _stable_hash(_known_feature_payload(features))

    def _not_verifiable(self, context: Mapping[str, Any], features: Mapping[str, Any], reason: str) -> AlphaForecast:
        strategy_id, instrument_id, venue_id = self._base_identity(context, features)
        fee, slippage, funding, source_hash = resolve_forecast_costs(context, features)
        volatility = _finite(features.get("realized_volatility", features.get("ann_volatility")))
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
            market_beta=_finite(features.get("market_beta")),
            regime_fit=0.0,
            capacity_score=None,
            model_version=SchemaVersion("3.0.0"),
            policy_version=self.policy.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=_resolve_timestamp(context, features),
            cost_source_hash=source_hash,
            schema_version=SchemaVersion("3.0.0"),
        )

    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        features = _context_features(context)
        returns, returns_invalid = _horizon_mapping(features, ("benchmark_returns", "returns_by_horizon"))
        slopes, slopes_invalid = _horizon_mapping(features, ("trend_slopes", "trend_slope_by_horizon"))
        volatility = _finite(
            features.get("realized_volatility", features.get("realized_vol", features.get("ann_volatility")))
        )
        if returns_invalid or slopes_invalid or not returns or volatility is None or volatility < 0:
            return self._not_verifiable(context, features, "CORE_FEATURES_UNKNOWN")

        policy = self.policy
        weights = dict(zip(policy.horizons, policy.horizon_weights, strict=True))
        available = [horizon for horizon in policy.horizons if horizon in returns]
        if not available:
            return self._not_verifiable(context, features, "HORIZON_FEATURES_UNKNOWN")
        weight_total = sum(weights[horizon] for horizon in available)
        momentum_values = {
            horizon: math.tanh(returns[horizon] / max(volatility, policy.return_scale, 1e-12)) for horizon in available
        }
        slope_values = {
            horizon: math.tanh(slopes.get(horizon, returns[horizon] / horizon) / policy.slope_scale)
            for horizon in available
        }
        momentum = sum(momentum_values[horizon] * weights[horizon] for horizon in available) / weight_total
        slope = sum(slope_values[horizon] * weights[horizon] for horizon in available) / weight_total
        signs = [1 if value > 0 else -1 if value < 0 else 0 for value in momentum_values.values()]
        persistence = abs(sum(signs) / len(signs))

        components: list[tuple[float, float]] = [
            (momentum, policy.return_weight),
            (slope, policy.slope_weight),
            (persistence * (1.0 if momentum >= 0 else -1.0), policy.persistence_weight),
        ]
        for feature_name, weight in (
            ("breadth", policy.breadth_weight),
            ("breakout_breadth", policy.breakout_weight),
        ):
            value = _finite(features.get(feature_name))
            if value is not None and 0.0 <= value <= 1.0:
                components.append((2.0 * value - 1.0, weight))
        volume = _finite(features.get("volume_expansion", features.get("volume_ratio", features.get("vol_ratio"))))
        if volume is not None and volume > 0:
            components.append((_clip(volume - 1.0, -1.0, 1.0) * (1.0 if momentum >= 0 else -1.0), policy.volume_weight))
        component_weight = sum(weight for _, weight in components)
        raw_score = _clip(sum(value * weight for value, weight in components) / component_weight, -1.0, 1.0)
        probability_positive = 1.0 / (1.0 + math.exp(-raw_score / policy.probability_temperature))
        side: str | OrderSide
        if raw_score >= policy.entry_threshold:
            side = OrderSide.BUY
        elif raw_score <= -policy.entry_threshold:
            side = OrderSide.SELL
        else:
            side = "NO_ACTION"

        expected_return = raw_score * policy.expected_return_scale
        fee, slippage, funding, source_hash = resolve_forecast_costs(context, features)
        total_cost_bps = None
        after_cost: float | None = None
        if fee is not None and slippage is not None and funding is not None and source_hash:
            total_cost_bps = fee + slippage + funding
            after_cost = expected_return - total_cost_bps / 10000.0
        quality = str(features.get("data_quality", features.get("quality", ""))).upper()
        quality_factor = 1.0 if quality in {"PASS", "GOOD", "RELIABLE"} else 0.7 if quality == "CONDITIONAL" else 0.5
        confidence = _clip(abs(raw_score) * quality_factor, 0.0, 1.0)
        state = context.get("state", {})
        state_direction = str(state.get("direction", "")) if isinstance(state, Mapping) else ""
        if state_direction in {"TRENDING_UP", "TRENDING_DOWN"}:
            aligned = (raw_score > 0 and state_direction == "TRENDING_UP") or (
                raw_score < 0 and state_direction == "TRENDING_DOWN"
            )
            regime_fit = 0.95 if aligned else 0.20
        else:
            regime_fit = _clip(abs(raw_score), 0.0, 1.0)
        liquidity = _finite(features.get("liquidity_score", features.get("liquidity")))
        capacity_score = _clip(liquidity, 0.0, 1.0) if liquidity is not None else None
        strategy_id, instrument_id, venue_id = self._base_identity(context, features)
        return AlphaForecast(
            alpha_id=self.alpha_id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            side=side,
            raw_score=raw_score,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            expected_volatility=volatility,
            horizon_seconds=policy.horizon_seconds,
            probability_positive=probability_positive,
            confidence=confidence,
            uncertainty=_clip(1.0 - confidence, 0.0, 1.0),
            expected_fee_bps=fee,
            expected_slippage_bps=slippage,
            expected_funding_bps=funding,
            market_beta=_finite(features.get("market_beta")),
            regime_fit=regime_fit,
            capacity_score=capacity_score,
            model_version=SchemaVersion("3.0.0"),
            policy_version=policy.policy_version,
            feature_hash=self._feature_hash(features),
            timestamp=_resolve_timestamp(context, features),
            cost_source_hash=source_hash,
            schema_version=SchemaVersion("3.0.0"),
        )

    def generate(self, context: Mapping[str, Any]) -> AlphaForecast:
        """Alias for callers that use the generic Alpha generate vocabulary."""
        return self.generate_forecast(context)

    def validate(self) -> bool:
        return bool(self.alpha_id and self.policy.policy_version)
