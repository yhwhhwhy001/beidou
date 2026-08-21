"""Deterministic forecast calibration and multi-entry ensemble fusion."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from beidou_strategy.alpha.contracts import AlphaForecast, EnsembleComponent, EnsembleForecast

from ._forecast_utils import finite, stable_hash


def _pairs(values: Mapping[str, float] | Iterable[tuple[str, float]]) -> tuple[tuple[str, float], ...]:
    raw = values.items() if isinstance(values, Mapping) else values
    result = tuple(sorted(((str(key), float(value)) for key, value in raw), key=lambda item: item[0]))
    if any(not math.isfinite(value) for _, value in result):
        raise ValueError("calibration values must be finite")
    return result


def _normalization_pairs(
    values: Mapping[str, tuple[float, float]] | Iterable[tuple[str, tuple[float, float]]],
) -> tuple[tuple[str, tuple[float, float]], ...]:
    raw = values.items() if isinstance(values, Mapping) else values
    result = tuple(
        sorted(((str(key), (float(mean), float(scale))) for key, (mean, scale) in raw), key=lambda item: item[0])
    )
    if any(not math.isfinite(mean) or not math.isfinite(scale) or scale <= 0 for _, (mean, scale) in result):
        raise ValueError("calibration normalization must be finite with positive scales")
    return result


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    """Published, checksum-bound representation of an offline calibration."""

    artifact_id: str
    model_version: str
    feature_schema_version: str
    coefficients: tuple[tuple[str, float], ...]
    intercept: float
    training_window: str
    oos_window: str
    regime_scope: str
    metrics: tuple[tuple[str, float], ...]
    normalization: tuple[tuple[str, tuple[float, float]], ...] = ()
    checksum: str = ""

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.artifact_id,
                self.model_version,
                self.feature_schema_version,
                self.training_window,
                self.oos_window,
                self.regime_scope,
            )
        ):
            raise ValueError("calibration artifact identity and windows are required")
        object.__setattr__(self, "coefficients", _pairs(self.coefficients))
        object.__setattr__(self, "metrics", _pairs(self.metrics))
        object.__setattr__(self, "normalization", _normalization_pairs(self.normalization))
        if not math.isfinite(float(self.intercept)):
            raise ValueError("calibration intercept must be finite")

    @classmethod
    def create(
        cls,
        *,
        artifact_id: str,
        model_version: str,
        feature_schema_version: str,
        coefficients: Mapping[str, float],
        intercept: float,
        training_window: str,
        oos_window: str,
        regime_scope: str,
        metrics: Mapping[str, float],
        normalization: Mapping[str, tuple[float, float]] | None = None,
    ) -> CalibrationArtifact:
        candidate = cls(
            artifact_id=artifact_id,
            model_version=model_version,
            feature_schema_version=feature_schema_version,
            coefficients=_pairs(coefficients),
            intercept=intercept,
            training_window=training_window,
            oos_window=oos_window,
            regime_scope=regime_scope,
            metrics=_pairs(metrics),
            normalization=_normalization_pairs(normalization or {}),
        )
        return replace(candidate, checksum=candidate.expected_checksum())

    def payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "training_window": self.training_window,
            "oos_window": self.oos_window,
            "regime_scope": self.regime_scope,
            "metrics": list(self.metrics),
            "normalization": list(self.normalization),
        }

    def expected_checksum(self) -> str:
        content = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def verify_checksum(self) -> bool:
        return bool(self.checksum and self.checksum == self.expected_checksum())


class DeterministicForecastCalibrator:
    """Pure-Python runtime inference for a published artifact."""

    def __init__(self, artifact: CalibrationArtifact) -> None:
        if not artifact.verify_checksum():
            raise ValueError("calibration artifact checksum is invalid")
        self.artifact = artifact
        self._coefficients = dict(artifact.coefficients)
        self._normalization = dict(artifact.normalization)

    def verify(self) -> bool:
        return self.artifact.verify_checksum()

    def predict(self, features: Mapping[str, object]) -> float | None:
        result = float(self.artifact.intercept)
        for name, coefficient in self._coefficients.items():
            value = finite(features.get(name))
            if value is None:
                return None
            mean, scale = self._normalization.get(name, (0.0, 1.0))
            result += coefficient * ((value - mean) / scale)
        return result if math.isfinite(result) else None

    def apply(self, forecast: AlphaForecast, features: Mapping[str, object] | None = None) -> AlphaForecast:
        values: dict[str, object] = {"raw_score": forecast.raw_score, "confidence": forecast.confidence}
        if features:
            values.update(features)
        expected_return = self.predict(values)
        if expected_return is None:
            return replace(
                forecast,
                expected_return=None,
                expected_return_after_cost=None,
                model_version=self.artifact.model_version,
            )
        total_cost = forecast.total_cost_bps
        after_cost = None
        if total_cost is not None and forecast.cost_source_hash:
            after_cost = expected_return - total_cost / 10000.0
        return replace(
            forecast,
            expected_return=expected_return,
            expected_return_after_cost=after_cost,
            model_version=self.artifact.model_version,
        )


@dataclass(frozen=True, slots=True)
class DynamicEnsemblePolicy:
    policy_version: str = "ensemble-policy-v3-v1"
    correlation_penalty: float = 0.75
    min_component_quality: float = 0.0

    def __post_init__(self) -> None:
        if not self.policy_version.strip() or not 0 <= self.correlation_penalty <= 1:
            raise ValueError("invalid ensemble policy")
        if not 0 <= self.min_component_quality <= 1:
            raise ValueError("min_component_quality must be within [0, 1]")


class EnsembleFuser:
    """Order-independent fusion of all verifiable AlphaForecast inputs."""

    def __init__(self, policy: DynamicEnsemblePolicy | None = None) -> None:
        self.policy = policy or DynamicEnsemblePolicy()

    def fuse(
        self,
        forecasts: Iterable[AlphaForecast],
        *,
        reliability: Mapping[str, float] | None = None,
        correlations: Mapping[tuple[str, str], float] | None = None,
        timestamp: datetime | None = None,
    ) -> EnsembleForecast:
        ordered = tuple(sorted(forecasts, key=lambda item: item.alpha_id))
        if not ordered:
            raise ValueError("at least one forecast is required")
        if len({forecast.alpha_id for forecast in ordered}) != len(ordered):
            raise ValueError("alpha_id must be unique in an ensemble")
        instrument_id = ordered[0].instrument_id
        venue_id = ordered[0].venue_id
        if any(forecast.instrument_id != instrument_id or forecast.venue_id != venue_id for forecast in ordered):
            raise ValueError("ensemble forecasts must share instrument and venue")

        reliability_map = reliability or {}
        correlation_map = correlations or {}
        qualities: list[tuple[AlphaForecast, float, float, float]] = []
        for forecast in ordered:
            after_cost = forecast.expected_return_after_cost
            gross_return = forecast.expected_return
            if after_cost is None or gross_return is None or gross_return * after_cost <= 0.0:
                qualities.append((forecast, 0.0, 0.0, 0.0))
                continue
            reliability_value = finite(reliability_map.get(forecast.alpha_id, 1.0))
            regime_fit = forecast.regime_fit if forecast.regime_fit is not None else 0.0
            capacity = forecast.capacity_score if forecast.capacity_score is not None else 0.0
            if reliability_value is None or not 0 <= reliability_value <= 1:
                reliability_value = 0.0
            correlation = 0.0
            for other in ordered:
                if other.alpha_id == forecast.alpha_id:
                    continue
                correlation = max(
                    correlation,
                    abs(
                        float(
                            correlation_map.get(
                                (forecast.alpha_id, other.alpha_id),
                                correlation_map.get((other.alpha_id, forecast.alpha_id), 0.0),
                            )
                        )
                    ),
                )
            diversity = 1.0 - min(1.0, correlation) * self.policy.correlation_penalty
            quality = abs(after_cost) * forecast.confidence * regime_fit * capacity * reliability_value * diversity
            if quality < self.policy.min_component_quality:
                quality = 0.0
            qualities.append((forecast, quality, reliability_value, diversity))

        total_quality = sum(item[1] for item in qualities)
        components: list[EnsembleComponent] = []
        for forecast, quality, reliability_value, _diversity in qualities:
            weight = quality / total_quality if total_quality > 0 else 0.0
            after_cost = forecast.expected_return_after_cost
            contribution = weight * after_cost if after_cost is not None and weight > 0 else None
            components.append(
                EnsembleComponent(
                    alpha_id=forecast.alpha_id,
                    weight=weight,
                    expected_return=forecast.expected_return,
                    contribution=contribution,
                    regime_fit=forecast.regime_fit or 0.0,
                    reliability=reliability_value,
                )
            )

        active = [
            (forecast, quality, weight)
            for (forecast, quality, _rel, _div), weight in zip(
                qualities, (item.weight for item in components), strict=True
            )
            if quality > 0 and weight > 0
        ]
        ensemble_return = (
            sum(
                weight * float(forecast.expected_return)
                for forecast, _quality, weight in active
                if forecast.expected_return is not None
            )
            if active and all(forecast.expected_return is not None for forecast, _quality, _weight in active)
            else None
        )
        ensemble_after_cost: float | None = None
        if active:
            after_cost_values: list[float] = []
            for forecast, _quality, weight in active:
                after_cost = forecast.expected_return_after_cost
                if after_cost is None:
                    break
                after_cost_values.append(weight * after_cost)
            if len(after_cost_values) == len(active):
                ensemble_after_cost = sum(after_cost_values)
        volatility: float | None = None
        if active:
            volatility_values: list[float] = []
            for forecast, _quality, weight in active:
                expected_volatility = forecast.expected_volatility
                if expected_volatility is None:
                    break
                volatility_values.append(weight * expected_volatility)
            if len(volatility_values) == len(active):
                volatility = sum(volatility_values)
        uncertainty = sum(weight * forecast.uncertainty for forecast, _quality, weight in active) if active else 1.0
        signed_quality = sum(
            quality * (1.0 if (forecast.expected_return_after_cost or 0.0) >= 0 else -1.0)
            for forecast, quality, _rel, _div in qualities
        )
        conflict_score = 0.0 if total_quality == 0 else max(0.0, min(1.0, 1.0 - abs(signed_quality) / total_quality))
        result_timestamp = timestamp or max(forecast.timestamp for forecast in ordered)
        model_version = "ensemble:" + stable_hash([str(forecast.model_version) for forecast in ordered])
        return EnsembleForecast(
            instrument_id=instrument_id,
            venue_id=venue_id,
            expected_return=ensemble_return,
            expected_return_after_cost=ensemble_after_cost,
            expected_volatility=volatility,
            uncertainty=max(0.0, min(1.0, uncertainty)),
            conflict_score=conflict_score,
            components=tuple(components),
            model_version=model_version,
            timestamp=result_timestamp.astimezone(UTC)
            if result_timestamp.tzinfo
            else result_timestamp.replace(tzinfo=UTC),
            policy_version=self.policy.policy_version,
        )
