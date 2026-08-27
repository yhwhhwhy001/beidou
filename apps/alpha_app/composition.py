"""Small local-data Alpha composition with explicit dependency injection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from beidou_shared.contracts.alpha_execution import AlphaTarget
from beidou_shared.contracts.experiment import DatasetRef
from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import TrendAlpha


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

    def __init__(self, alpha: TrendAlpha | None = None) -> None:
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
