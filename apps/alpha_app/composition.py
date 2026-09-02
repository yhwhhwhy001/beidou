"""Small local-data Alpha composition with explicit dependency injection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from beidou_shared.contracts.alpha_execution import (
    ALPHA_STATE_SCHEMA_VERSION,
    STATEFUL_ALPHA_STRATEGY_ID,
    AlphaStateContractError,
    AlphaTarget,
    AlphaTargetSemantics,
    BoundCostSnapshot,
    BoundPositionSnapshot,
    canonical_contract_digest,
    canonical_contract_timestamp,
)
from beidou_shared.contracts.experiment import DatasetRef
from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import TrendAlpha
from beidou_strategy.alpha.contracts import AlphaForecast


@dataclass(frozen=True, slots=True)
class BoundLocalData:
    """Caller-bound local closes; loading and persistence stay outside this app."""

    dataset: DatasetRef
    instrument_id: InstrumentId
    venue_id: VenueId
    closes: tuple[float, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.dataset.source != "LOCAL":
            raise ValueError("Alpha app accepts LOCAL data only")
        if len(self.closes) < 51:
            raise ValueError("at least 51 closes are required for the configured horizons")
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in self.closes):
            raise ValueError("closes must be finite positive values")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class OfflineAlphaResult:
    dataset: DatasetRef
    target: AlphaTarget
    row_count: int


@dataclass(frozen=True, slots=True)
class StatefulOfflineAlphaResult:
    """Audit-complete result for the additive stateful v2 contract."""

    dataset: DatasetRef
    target: AlphaTarget
    row_count: int
    forecast_side: str
    target_semantics: AlphaTargetSemantics
    decision_at: datetime
    position_source_artifact_digest: str
    position_binding_digest: str
    position_observed_at: datetime
    position_valid_until: datetime
    cost_source_artifact_digest: str
    cost_binding_digest: str
    cost_observed_at: datetime
    cost_valid_until: datetime
    schema_version: str = ALPHA_STATE_SCHEMA_VERSION

    @property
    def result_binding_digest(self) -> str:
        return canonical_contract_digest(
            "beidou.alpha.stateful-result.v2",
            {
                "schema_version": self.schema_version,
                "dataset_id": self.dataset.dataset_id,
                "dataset_version": str(self.dataset.version),
                "dataset_content_hash": self.dataset.content_hash,
                "strategy_id": str(self.target.strategy_id),
                "instrument_id": str(self.target.instrument_id),
                "venue_id": str(self.target.venue_id),
                "target_weight": 0.0 if self.target.target_weight == 0.0 else float(self.target.target_weight),
                "forecast_hash": self.target.forecast_hash,
                "model_version": str(self.target.model_version),
                "target_timestamp": canonical_contract_timestamp(self.target.timestamp),
                "row_count": self.row_count,
                "forecast_side": self.forecast_side,
                "target_semantics": self.target_semantics.value,
                "decision_at": canonical_contract_timestamp(self.decision_at),
                "position_binding_digest": self.position_binding_digest,
                "cost_binding_digest": self.cost_binding_digest,
            },
        )


class AlphaForecastProvider(Protocol):
    def generate_forecast(self, context: Mapping[str, Any]) -> AlphaForecast:
        """Return a forecast without performing I/O."""


def _feature_hash(features: dict[str, object]) -> str:
    payload = json.dumps(features, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _returns(closes: Sequence[float], horizon: int) -> float:
    return float(closes[-1]) / float(closes[-1 - horizon]) - 1.0


def _volatility(closes: Sequence[float]) -> float:
    returns = [float(closes[index]) / float(closes[index - 1]) - 1.0 for index in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return max(math.sqrt(variance), 1e-12)


class OfflineAlphaApp:
    """Explicitly injected, zero-I/O Alpha composition for bound local data."""

    LEGACY_CONTRACT_STATUS = "LEGACY_DIAGNOSTIC_ONLY"

    def __init__(self, alpha: AlphaForecastProvider | None = None) -> None:
        self._alpha = alpha or TrendAlpha()

    def evaluate(self, data: BoundLocalData) -> OfflineAlphaResult:
        closes = data.closes
        returns_by_horizon = {horizon: _returns(closes, horizon) for horizon in (5, 20, 50)}
        features: dict[str, object] = {
            "benchmark_returns": returns_by_horizon,
            "trend_slopes": {horizon: value / horizon for horizon, value in returns_by_horizon.items()},
            "realized_volatility": _volatility(closes),
            "data_quality": "PASS",
            "liquidity_score": 1.0,
            "expected_fee_bps": 1.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.0,
            "source_hash": data.dataset.content_hash,
        }
        features["feature_hash"] = _feature_hash(features)
        context = {
            "strategy_id": StrategyId("offline-alpha-v1"),
            "instrument_id": data.instrument_id,
            "venue_id": data.venue_id,
            "timestamp": data.observed_at.astimezone(timezone.utc),
            "features": features,
        }
        forecast = self._alpha.generate_forecast(context)
        target = AlphaTarget(
            strategy_id=forecast.strategy_id,
            instrument_id=forecast.instrument_id,
            venue_id=forecast.venue_id,
            target_weight=float(forecast.raw_score),
            forecast_hash=forecast.forecast_hash,
            model_version=SchemaVersion(str(forecast.model_version)),
            timestamp=forecast.timestamp,
        )
        return OfflineAlphaResult(dataset=data.dataset, target=target, row_count=len(closes))

    def evaluate_stateful(
        self,
        data: BoundLocalData,
        *,
        position: BoundPositionSnapshot | None,
        costs: BoundCostSnapshot | None,
    ) -> StatefulOfflineAlphaResult:
        """Evaluate v2 with explicit state/cost facts and fail-closed provider checks."""

        if position is None:
            raise AlphaStateContractError("POSITION_SNAPSHOT_REQUIRED")
        if costs is None:
            raise AlphaStateContractError("COST_SNAPSHOT_REQUIRED")
        decision_at = data.observed_at.astimezone(timezone.utc)
        self._validate_stateful_inputs(data, decision_at=decision_at, position=position, costs=costs)

        closes = data.closes
        returns_by_horizon = {horizon: _returns(closes, horizon) for horizon in (5, 20, 50)}
        features: dict[str, object] = {
            "benchmark_returns": returns_by_horizon,
            "trend_slopes": {horizon: value / horizon for horizon, value in returns_by_horizon.items()},
            "realized_volatility": _volatility(closes),
            "data_quality": "PASS",
            "liquidity_score": 1.0,
        }
        features["feature_hash"] = _feature_hash(features)
        context: dict[str, object] = {
            "strategy_id": STATEFUL_ALPHA_STRATEGY_ID,
            "instrument_id": data.instrument_id,
            "venue_id": data.venue_id,
            "timestamp": decision_at,
            "features": features,
            "costs": {
                "expected_fee_bps": costs.expected_fee_bps,
                "expected_slippage_bps": costs.expected_slippage_bps,
                "expected_funding_bps": costs.expected_funding_bps,
                "source_hash": costs.source_artifact_digest,
            },
        }
        forecast = self._alpha.generate_forecast(context)
        if type(forecast) is not AlphaForecast:
            raise AlphaStateContractError("FORECAST_TYPE_INVALID")
        side, semantic, target_weight = self._validated_stateful_target(
            forecast,
            data=data,
            decision_at=decision_at,
            position=position,
            costs=costs,
        )
        target = AlphaTarget(
            strategy_id=forecast.strategy_id,
            instrument_id=forecast.instrument_id,
            venue_id=forecast.venue_id,
            target_weight=target_weight,
            forecast_hash=forecast.forecast_hash,
            model_version=SchemaVersion(str(forecast.model_version)),
            timestamp=decision_at,
        )
        return StatefulOfflineAlphaResult(
            dataset=data.dataset,
            target=target,
            row_count=len(closes),
            forecast_side=side,
            target_semantics=semantic,
            decision_at=decision_at,
            position_source_artifact_digest=position.source_artifact_digest,
            position_binding_digest=position.binding_digest,
            position_observed_at=position.observed_at,
            position_valid_until=position.valid_until,
            cost_source_artifact_digest=costs.source_artifact_digest,
            cost_binding_digest=costs.binding_digest,
            cost_observed_at=costs.observed_at,
            cost_valid_until=costs.valid_until,
        )

    @staticmethod
    def _validate_stateful_inputs(
        data: BoundLocalData,
        *,
        decision_at: datetime,
        position: BoundPositionSnapshot,
        costs: BoundCostSnapshot,
    ) -> None:
        if (
            position.strategy_id != STATEFUL_ALPHA_STRATEGY_ID
            or position.instrument_id != data.instrument_id
            or position.venue_id != data.venue_id
        ):
            raise AlphaStateContractError("POSITION_SCOPE_MISMATCH")
        if position.observed_at.astimezone(timezone.utc) > decision_at:
            raise AlphaStateContractError("POSITION_FACT_FROM_FUTURE")
        if position.valid_until.astimezone(timezone.utc) < decision_at:
            raise AlphaStateContractError("POSITION_FACT_EXPIRED")
        if costs.instrument_id != data.instrument_id or costs.venue_id != data.venue_id:
            raise AlphaStateContractError("COST_SCOPE_MISMATCH")
        if costs.observed_at.astimezone(timezone.utc) > decision_at:
            raise AlphaStateContractError("COST_FACT_FROM_FUTURE")
        if costs.valid_until.astimezone(timezone.utc) < decision_at:
            raise AlphaStateContractError("COST_FACT_EXPIRED")

    @staticmethod
    def _validated_stateful_target(
        forecast: AlphaForecast,
        *,
        data: BoundLocalData,
        decision_at: datetime,
        position: BoundPositionSnapshot,
        costs: BoundCostSnapshot,
    ) -> tuple[str, AlphaTargetSemantics, float]:
        if (
            forecast.strategy_id != STATEFUL_ALPHA_STRATEGY_ID
            or forecast.instrument_id != data.instrument_id
            or forecast.venue_id != data.venue_id
        ):
            raise AlphaStateContractError("FORECAST_SCOPE_BINDING_MISMATCH")
        if forecast.timestamp.astimezone(timezone.utc) != decision_at:
            raise AlphaStateContractError("FORECAST_TIMESTAMP_BINDING_MISMATCH")
        score = float(forecast.raw_score)
        if not math.isfinite(score) or not -1.0 <= score <= 1.0:
            raise AlphaStateContractError("FORECAST_DIRECTION_BINDING_MISMATCH")
        if (
            forecast.expected_fee_bps != costs.expected_fee_bps
            or forecast.expected_slippage_bps != costs.expected_slippage_bps
            or forecast.expected_funding_bps != costs.expected_funding_bps
            or forecast.cost_source_hash != costs.source_artifact_digest
        ):
            raise AlphaStateContractError("FORECAST_COST_BINDING_MISMATCH")
        if not forecast.cost_verifiable:
            raise AlphaStateContractError("FORECAST_COST_ARITHMETIC_INVALID")
        side = forecast.side.value if isinstance(forecast.side, Enum) else str(forecast.side).upper()
        if side == "NO_ACTION":
            return side, AlphaTargetSemantics.PRESERVE_VERIFIED_POSITION, float(position.current_weight)
        if side == "BUY":
            if score <= 0.0:
                raise AlphaStateContractError("FORECAST_DIRECTION_BINDING_MISMATCH")
            return side, AlphaTargetSemantics.FORECAST_RAW_SCORE, score
        if side == "SELL":
            if score >= 0.0:
                raise AlphaStateContractError("FORECAST_DIRECTION_BINDING_MISMATCH")
            return side, AlphaTargetSemantics.FORECAST_RAW_SCORE, score
        raise AlphaStateContractError("UNSUPPORTED_FORECAST_SIDE")
