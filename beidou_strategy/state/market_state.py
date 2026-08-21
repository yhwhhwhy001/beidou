"""Deterministic Market State V3 contracts and estimator.

The estimator intentionally remains rule based. It is a replayable, visible
production state estimator; more complex models can be challengers without
creating a second market-state meaning in the execution path.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any, cast

from beidou_shared.types import InstrumentId, VenueId

_REGIMES = ("STRONG_UP", "UP", "RANGE", "DOWN", "STRONG_DOWN")
_VALID_STRESS_LEVELS = {"NORMAL", "ELEVATED", "HIGH", "CRISIS", "UNKNOWN"}
_EPSILON = 1e-12


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return f"NON_FINITE:{value!r}"
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _stable_hash(value: object) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class MarketStatePolicy:
    """Versioned numerical and classification policy for Market State V3."""

    policy_version: str = "market-state-v3-policy-v1"
    horizons: tuple[int, ...] = (5, 20, 50)
    horizon_weights: tuple[float, ...] = (0.2, 0.3, 0.5)
    momentum_weight: float = 0.40
    slope_weight: float = 0.25
    persistence_weight: float = 0.05
    breadth_weight: float = 0.15
    breakout_weight: float = 0.10
    volume_weight: float = 0.05
    momentum_scale: float = 0.20
    slope_scale: float = 0.01
    trend_threshold: float = 0.25
    strong_trend_threshold: float = 0.60
    probability_temperature: float = 0.25
    elevated_volatility: float = 0.30
    high_volatility: float = 0.80
    crisis_volatility: float = 1.20
    annualization_bars: float = 365.0

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")
        if not self.horizons or any(horizon <= 0 for horizon in self.horizons):
            raise ValueError("horizons must be positive")
        if len(self.horizons) != len(self.horizon_weights):
            raise ValueError("horizon_weights must align with horizons")
        if any(weight < 0 or not math.isfinite(weight) for weight in self.horizon_weights):
            raise ValueError("horizon_weights must be finite and non-negative")
        if sum(self.horizon_weights) <= 0:
            raise ValueError("horizon_weights must have positive mass")
        for name in (
            "momentum_weight",
            "slope_weight",
            "persistence_weight",
            "breadth_weight",
            "breakout_weight",
            "volume_weight",
        ):
            value = getattr(self, name)
            if value < 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.momentum_scale <= 0 or self.slope_scale <= 0 or self.probability_temperature <= 0:
            raise ValueError("numerical scales must be positive")
        if not 0 < self.trend_threshold < self.strong_trend_threshold <= 1:
            raise ValueError("trend thresholds must be ordered in (0, 1]")
        if not 0 <= self.elevated_volatility < self.high_volatility < self.crisis_volatility:
            raise ValueError("volatility thresholds must be ordered")
        if self.annualization_bars <= 0 or not math.isfinite(self.annualization_bars):
            raise ValueError("annualization_bars must be positive and finite")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "horizons": self.horizons,
            "horizon_weights": self.horizon_weights,
            "momentum_weight": self.momentum_weight,
            "slope_weight": self.slope_weight,
            "persistence_weight": self.persistence_weight,
            "breadth_weight": self.breadth_weight,
            "breakout_weight": self.breakout_weight,
            "volume_weight": self.volume_weight,
            "momentum_scale": self.momentum_scale,
            "slope_scale": self.slope_scale,
            "trend_threshold": self.trend_threshold,
            "strong_trend_threshold": self.strong_trend_threshold,
            "probability_temperature": self.probability_temperature,
            "elevated_volatility": self.elevated_volatility,
            "high_volatility": self.high_volatility,
            "crisis_volatility": self.crisis_volatility,
            "annualization_bars": self.annualization_bars,
        }

    @property
    def policy_hash(self) -> str:
        return _stable_hash(self.canonical_payload())


DEFAULT_MARKET_STATE_POLICY = MarketStatePolicy()


@dataclass(frozen=True, slots=True)
class DirectionState:
    regime: str  # TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, UNKNOWN
    probability: float
    uncertainty: float
    model_fallback: bool = False
    trend_score: float = 0.0
    trend_probability: float = 0.0
    trend_persistence: float = 0.0
    trend_acceleration: float = 0.0
    breadth: float | None = None
    breakout_breadth: float | None = None
    dispersion: float | None = None
    benchmark_return: float | None = None
    regime_probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StressState:
    level: str  # NORMAL, ELEVATED, HIGH, CRISIS, UNKNOWN
    probability: float
    drawdown_from_peak_pct: float | None = None
    vol_regime: str | None = None
    correlation_regime: str | None = None
    stress_score: float = 0.0
    realized_volatility: float | None = None


@dataclass(frozen=True, slots=True)
class QualityState:
    tier: str  # GOOD, DEGRADED, UNRELIABLE, UNKNOWN
    data_gap_seconds: float | None = None
    stale_instruments: list[InstrumentId] = field(default_factory=list)
    rejection_rate_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class MarketStateVector:
    direction: DirectionState
    stress: StressState
    quality: QualityState
    venue_id: VenueId
    instrument_id: InstrumentId
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    rollback_rate_pct: float = 0.0
    policy_version: str = ""
    policy_hash: str = ""
    features_hash: str = ""
    state_hash: str = ""

    def should_override_direction(self) -> bool:
        """压力危机或质量不可信时必须覆盖方向信号。"""
        return self.stress.level in ("CRISIS",) or self.quality.tier in ("UNRELIABLE", "UNKNOWN")

    def is_tradable(self) -> bool:
        if self.should_override_direction():
            return False
        if self.stress.level == "UNKNOWN" or self.quality.tier == "UNKNOWN":
            return False
        return not self.rollback_rate_pct > 50.0

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": {
                "regime": self.direction.regime,
                "probability": self.direction.probability,
                "uncertainty": self.direction.uncertainty,
                "model_fallback": self.direction.model_fallback,
                "trend_score": self.direction.trend_score,
                "trend_probability": self.direction.trend_probability,
                "trend_persistence": self.direction.trend_persistence,
                "trend_acceleration": self.direction.trend_acceleration,
                "breadth": self.direction.breadth,
                "breakout_breadth": self.direction.breakout_breadth,
                "dispersion": self.direction.dispersion,
                "benchmark_return": self.direction.benchmark_return,
                "regime_probabilities": dict(self.direction.regime_probabilities),
            },
            "stress": {
                "level": self.stress.level,
                "probability": self.stress.probability,
                "drawdown_from_peak_pct": self.stress.drawdown_from_peak_pct,
                "vol_regime": self.stress.vol_regime,
                "correlation_regime": self.stress.correlation_regime,
                "stress_score": self.stress.stress_score,
                "realized_volatility": self.stress.realized_volatility,
            },
            "quality": {
                "tier": self.quality.tier,
                "data_gap_seconds": self.quality.data_gap_seconds,
                "stale_instruments": [str(item) for item in self.quality.stale_instruments],
                "rejection_rate_pct": self.quality.rejection_rate_pct,
            },
            "venue_id": str(self.venue_id),
            "instrument_id": str(self.instrument_id),
            "timestamp": self.timestamp.isoformat(),
            "rollback_rate_pct": self.rollback_rate_pct,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "features_hash": self.features_hash,
            "state_hash": self.state_hash,
        }


class MarketStateEstimator:
    """确定性市场状态评估器，valid data 不会被永久映射为 UNKNOWN。"""

    def __init__(self, policy: MarketStatePolicy | None = None) -> None:
        self.policy = policy or DEFAULT_MARKET_STATE_POLICY
        self._rollback_count: int = 0
        self._total_predictions: int = 0

    @staticmethod
    def _mapping_series(features: Mapping[str, object], names: tuple[str, ...]) -> tuple[dict[int, float], bool, bool]:
        """Return (series, present, invalid) for a horizon mapping."""
        for name in names:
            raw = features.get(name)
            if raw is None:
                continue
            if not isinstance(raw, Mapping) or not raw:
                return {}, True, True
            parsed: dict[int, float] = {}
            for raw_horizon, raw_value in raw.items():
                try:
                    horizon = int(raw_horizon)
                except (TypeError, ValueError):
                    return {}, True, True
                value = _finite(raw_value)
                if horizon <= 0 or value is None:
                    return {}, True, True
                parsed[horizon] = value
            return parsed, True, False
        return {}, False, False

    @staticmethod
    def _prices(features: Mapping[str, object]) -> tuple[list[float], bool, bool]:
        raw = features.get("prices")
        if raw is None:
            return [], False, False
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            return [], True, True
        prices: list[float] = []
        for item in raw:
            value = _finite(item)
            if value is None or value <= 0:
                return [], True, True
            prices.append(value)
        return prices, True, len(prices) < 2

    def _extract_returns(self, features: Mapping[str, object]) -> tuple[dict[int, float], bool]:
        returns, present, invalid = self._mapping_series(
            features,
            ("benchmark_returns", "returns_by_horizon", "benchmark_return_by_horizon"),
        )
        if present:
            return returns, invalid or not returns

        direct: dict[int, float] = {}
        for horizon in self.policy.horizons:
            for name in (f"trend_{horizon}_pct", f"return_{horizon}_pct"):
                if name in features:
                    value = _finite(features[name])
                    if value is None:
                        return {}, True
                    direct[horizon] = value / 100.0
                    break
        if direct:
            return direct, False

        prices, present, invalid = self._prices(features)
        if present:
            if invalid:
                return {}, True
            derived: dict[int, float] = {}
            for horizon in self.policy.horizons:
                if len(prices) > horizon:
                    derived[horizon] = math.log(prices[-1] / prices[-horizon - 1])
            return derived, not bool(derived)
        return {}, True

    def _extract_slopes(
        self,
        features: Mapping[str, object],
        returns: Mapping[int, float],
    ) -> tuple[dict[int, float], bool]:
        slopes, present, invalid = self._mapping_series(
            features,
            ("trend_slopes", "trend_slope_by_horizon"),
        )
        if present:
            return slopes, invalid or not slopes
        if "trend_slope" in features:
            value = _finite(features["trend_slope"])
            if value is None:
                return {}, True
            return {max(self.policy.horizons): value}, False
        # A published return divided by its horizon is a conservative slope
        # fallback. It is deterministic and remains visible in quality.
        if returns:
            return {horizon: value / horizon for horizon, value in returns.items()}, False
        return {}, True

    def _extract_volatility(self, features: Mapping[str, object]) -> tuple[float | None, bool]:
        for name in ("realized_volatility", "realized_vol", "ann_volatility", "volatility"):
            if name in features:
                value = _finite(features[name])
                return (value, value is None or value < 0)
        mapped, present, invalid = self._mapping_series(features, ("realized_vol_by_horizon", "volatility_by_horizon"))
        if present:
            return (max(mapped.values()) if mapped else None, invalid or not mapped)
        prices, present, invalid = self._prices(features)
        if present:
            if invalid or len(prices) < 3:
                return None, True
            log_returns = [math.log(current / previous) for previous, current in pairwise(prices)]
            mean = sum(log_returns) / len(log_returns)
            variance = sum((item - mean) ** 2 for item in log_returns) / len(log_returns)
            return math.sqrt(variance) * math.sqrt(self.policy.annualization_bars), False
        return None, True

    @staticmethod
    def _optional_number(features: Mapping[str, object], names: tuple[str, ...]) -> tuple[float | None, bool]:
        for name in names:
            if name in features:
                value = _finite(features[name])
                return value, value is None
        return None, False

    def _invalid_state(
        self,
        venue_id: VenueId,
        instrument_id: InstrumentId,
        timestamp: datetime,
        features: Mapping[str, object],
    ) -> MarketStateVector:
        self._total_predictions += 1
        probabilities = dict.fromkeys(_REGIMES, 0.2)
        direction = DirectionState(
            regime="UNKNOWN",
            probability=0.0,
            uncertainty=1.0,
            model_fallback=True,
            regime_probabilities=probabilities,
        )
        stress = StressState(level="UNKNOWN", probability=0.0)
        quality = QualityState(tier="UNKNOWN")
        features_hash = _stable_hash({"features": features, "policy_hash": self.policy.policy_hash})
        state = MarketStateVector(
            direction=direction,
            stress=stress,
            quality=quality,
            venue_id=venue_id,
            instrument_id=instrument_id,
            timestamp=timestamp,
            rollback_rate_pct=self.rollback_rate(),
            policy_version=self.policy.policy_version,
            policy_hash=self.policy.policy_hash,
            features_hash=features_hash,
        )
        object.__setattr__(state, "state_hash", _stable_hash(state.to_dict() | {"timestamp": None, "state_hash": ""}))
        return state

    def estimate(
        self,
        venue_id: VenueId,
        instrument_id: InstrumentId,
        features: Mapping[str, object],
        *,
        timestamp: datetime | None = None,
    ) -> MarketStateVector:
        """Estimate a replayable state from published market features.

        Missing or non-finite core inputs produce an explicit UNKNOWN state;
        they never become a normal/tradable state through a numeric fallback.
        """
        observed_at = timestamp or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        returns, returns_invalid = self._extract_returns(features)
        if returns_invalid:
            return self._invalid_state(venue_id, instrument_id, observed_at, features)
        slopes, slopes_invalid = self._extract_slopes(features, returns)
        volatility, volatility_invalid = self._extract_volatility(features)
        if slopes_invalid or volatility_invalid or volatility is None:
            return self._invalid_state(venue_id, instrument_id, observed_at, features)

        optional: dict[str, float | None] = {}
        optional_invalid = False
        for field_name, names in (
            ("breadth", ("breadth", "universe_breadth")),
            ("breakout_breadth", ("breakout_breadth",)),
            ("dispersion", ("dispersion", "cross_sectional_dispersion")),
            ("correlation", ("cross_sectional_correlation", "correlation")),
            ("liquidity", ("liquidity_score", "liquidity")),
            ("volume_expansion", ("volume_expansion", "volume_ratio", "vol_ratio")),
        ):
            value, invalid = self._optional_number(features, names)
            optional[field_name] = value
            optional_invalid |= invalid
        if optional["breadth"] is not None and not 0 <= optional["breadth"] <= 1:
            optional_invalid = True
        if optional["breakout_breadth"] is not None and not 0 <= optional["breakout_breadth"] <= 1:
            optional_invalid = True
        if optional["correlation"] is not None and not -1 <= optional["correlation"] <= 1:
            optional_invalid = True
        if optional["liquidity"] is not None and not 0 <= optional["liquidity"] <= 1:
            optional_invalid = True
        if optional["dispersion"] is not None and optional["dispersion"] < 0:
            optional_invalid = True
        if optional["volume_expansion"] is not None and optional["volume_expansion"] <= 0:
            optional_invalid = True
        if optional_invalid:
            return self._invalid_state(venue_id, instrument_id, observed_at, features)

        policy = self.policy
        horizon_weights = dict(zip(policy.horizons, policy.horizon_weights, strict=True))
        available_horizons = [horizon for horizon in policy.horizons if horizon in returns]
        if not available_horizons:
            return self._invalid_state(venue_id, instrument_id, observed_at, features)
        weight_total = sum(horizon_weights[horizon] for horizon in available_horizons)

        momentum_values = {
            horizon: math.tanh(returns[horizon] / max(volatility, policy.momentum_scale, _EPSILON))
            for horizon in available_horizons
        }
        slope_values = {
            horizon: math.tanh(slopes.get(horizon, returns[horizon] / horizon) / policy.slope_scale)
            for horizon in available_horizons
        }
        momentum = (
            sum(momentum_values[horizon] * horizon_weights[horizon] for horizon in available_horizons) / weight_total
        )
        slope = sum(slope_values[horizon] * horizon_weights[horizon] for horizon in available_horizons) / weight_total
        signed_components = [1.0 if value > 0 else -1.0 if value < 0 else 0.0 for value in momentum_values.values()]
        persistence = abs(sum(signed_components) / len(signed_components))
        short_horizon = min(available_horizons)
        long_horizon = max(available_horizons)
        acceleration = _clip(momentum_values[short_horizon] - momentum_values[long_horizon], -1.0, 1.0)

        breadth_score = 2.0 * optional["breadth"] - 1.0 if optional["breadth"] is not None else None
        breakout_score = 2.0 * optional["breakout_breadth"] - 1.0 if optional["breakout_breadth"] is not None else None
        volume_score: float | None = None
        if optional["volume_expansion"] is not None:
            volume_score = _clip(optional["volume_expansion"] - 1.0, -1.0, 1.0)
            volume_score *= 1.0 if momentum >= 0 else -1.0

        components: list[tuple[float, float]] = [
            (momentum, policy.momentum_weight),
            (slope, policy.slope_weight),
            (persistence * (1.0 if momentum >= 0 else -1.0), policy.persistence_weight),
        ]
        if breadth_score is not None:
            components.append((breadth_score, policy.breadth_weight))
        if breakout_score is not None:
            components.append((breakout_score, policy.breakout_weight))
        if volume_score is not None:
            components.append((volume_score, policy.volume_weight))
        component_weight = sum(weight for _, weight in components)
        trend_score = _clip(sum(value * weight for value, weight in components) / component_weight, -1.0, 1.0)
        trend_probability = _clip(abs(trend_score), 0.0, 1.0)
        logits = [
            -((trend_score - center) ** 2) / policy.probability_temperature for center in (1.0, 0.5, 0.0, -0.5, -1.0)
        ]
        max_logit = max(logits)
        exp_logits = [math.exp(logit - max_logit) for logit in logits]
        probability_total = sum(exp_logits)
        regime_probabilities = {
            regime: value / probability_total for regime, value in zip(_REGIMES, exp_logits, strict=True)
        }
        probability = max(regime_probabilities.values())
        uncertainty = _clip(1.0 - probability, 0.0, 1.0)
        if trend_score >= policy.trend_threshold:
            regime = "TRENDING_UP"
        elif trend_score <= -policy.trend_threshold:
            regime = "TRENDING_DOWN"
        elif volatility >= policy.high_volatility:
            regime = "VOLATILE"
        else:
            regime = "RANGING"

        stress_level_raw = str(features.get("stress_level", "")).upper().strip()
        if stress_level_raw:
            if stress_level_raw not in _VALID_STRESS_LEVELS - {"UNKNOWN"}:
                return self._invalid_state(venue_id, instrument_id, observed_at, features)
            stress_level = stress_level_raw
        elif volatility >= policy.crisis_volatility:
            stress_level = "CRISIS"
        elif volatility >= policy.high_volatility:
            stress_level = "HIGH"
        elif volatility >= policy.elevated_volatility:
            stress_level = "ELEVATED"
        else:
            stress_level = "NORMAL"
        stress_score = _clip(volatility / policy.crisis_volatility, 0.0, 1.0)
        if stress_level == "CRISIS":
            stress_probability = 1.0
        elif stress_level == "HIGH":
            stress_probability = _clip(stress_score, 0.5, 0.99)
        elif stress_level == "ELEVATED":
            stress_probability = _clip(stress_score, 0.2, 0.8)
        else:
            stress_probability = _clip(stress_score, 0.0, 0.2)
        correlation = optional["correlation"]
        correlation_regime = None
        if correlation is not None:
            correlation_regime = "HIGH" if correlation >= 0.7 else "NORMAL"
        stress = StressState(
            level=stress_level,
            probability=stress_probability,
            vol_regime=stress_level,
            correlation_regime=correlation_regime,
            stress_score=stress_score,
            realized_volatility=volatility,
        )

        explicit_quality = str(features.get("data_quality", features.get("quality", ""))).upper().strip()
        if explicit_quality in {"FAIL", "BLOCK", "UNKNOWN", "NOT_VERIFIABLE", "UNRELIABLE"}:
            quality_tier = "UNKNOWN"
        elif explicit_quality in {"DEGRADED", "CONDITIONAL"}:
            quality_tier = "DEGRADED"
        elif explicit_quality in {"PASS", "GOOD", "RELIABLE", ""}:
            quality_tier = "GOOD"
        else:
            return self._invalid_state(venue_id, instrument_id, observed_at, features)
        optional_missing = sum(value is None for value in optional.values())
        if quality_tier == "GOOD" and optional_missing:
            quality_tier = "DEGRADED"
        data_gap, data_gap_invalid = self._optional_number(features, ("data_gap_seconds", "data_gap"))
        if data_gap_invalid or (data_gap is not None and data_gap < 0):
            return self._invalid_state(venue_id, instrument_id, observed_at, features)
        quality = QualityState(tier=quality_tier, data_gap_seconds=data_gap)

        self._total_predictions += 1
        direction = DirectionState(
            regime=regime,
            probability=probability,
            uncertainty=uncertainty,
            model_fallback=False,
            trend_score=trend_score,
            trend_probability=trend_probability,
            trend_persistence=persistence,
            trend_acceleration=acceleration,
            breadth=optional["breadth"],
            breakout_breadth=optional["breakout_breadth"],
            dispersion=optional["dispersion"],
            benchmark_return=returns[long_horizon],
            regime_probabilities=regime_probabilities,
        )
        features_hash = _stable_hash({"features": features, "policy_hash": policy.policy_hash})
        state = MarketStateVector(
            direction=direction,
            stress=stress,
            quality=quality,
            venue_id=venue_id,
            instrument_id=instrument_id,
            timestamp=observed_at,
            rollback_rate_pct=self.rollback_rate(),
            policy_version=policy.policy_version,
            policy_hash=policy.policy_hash,
            features_hash=features_hash,
        )
        object.__setattr__(state, "state_hash", _stable_hash(state.to_dict() | {"timestamp": None, "state_hash": ""}))
        return state

    def record_rollback(self) -> None:
        self._rollback_count += 1

    def rollback_rate(self) -> float:
        if self._total_predictions == 0:
            return 0.0
        return self._rollback_count / self._total_predictions * 100.0
