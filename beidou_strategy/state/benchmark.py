"""Versioned benchmark contracts and deterministic snapshot construction.

The benchmark is an economic reference for beta attribution and market state;
it is not a trading signal.  This module intentionally uses only the Python
standard library so the production inference path does not depend on research
extras.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from statistics import pstdev
from types import MappingProxyType
from typing import Mapping, Sequence

from beidou_shared.types import InstrumentId, SchemaVersion


class BenchmarkType(str, Enum):
    """Supported benchmark constructions."""

    SINGLE_ASSET = "SINGLE_ASSET"
    UNIVERSE_INDEX = "UNIVERSE_INDEX"


_SUPPORTED_QUALITY = frozenset({"PASS", "CONDITIONAL", "DEGRADED", "UNKNOWN", "NOT_VERIFIABLE"})


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash_payload(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BenchmarkDefinition:
    """Immutable definition of a single-asset or universe benchmark."""

    benchmark_id: str
    benchmark_type: BenchmarkType | str
    components: tuple[InstrumentId, ...]
    weighting_method: str
    rebalance_rule: str
    policy_version: str
    schema_version: SchemaVersion = field(default_factory=lambda: SchemaVersion("3.0.0"))

    def __post_init__(self) -> None:
        benchmark_type = BenchmarkType(self.benchmark_type)
        components = tuple(InstrumentId(str(component)) for component in self.components)
        if not self.benchmark_id.strip():
            raise ValueError("benchmark_id must not be empty")
        if not components or any(not str(component).strip() for component in components):
            raise ValueError("benchmark components must not be empty")
        if len(set(components)) != len(components):
            raise ValueError("benchmark components must be unique")
        if benchmark_type is BenchmarkType.SINGLE_ASSET and len(components) != 1:
            raise ValueError("SINGLE_ASSET benchmark requires exactly one component")
        if not self.weighting_method.strip():
            raise ValueError("weighting_method must not be empty")
        if not self.rebalance_rule.strip():
            raise ValueError("rebalance_rule must not be empty")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty")
        object.__setattr__(self, "benchmark_type", benchmark_type)
        object.__setattr__(self, "components", components)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "benchmark_id": self.benchmark_id,
            "benchmark_type": BenchmarkType(self.benchmark_type).value,
            "components": [str(component) for component in self.components],
            "weighting_method": self.weighting_method,
            "rebalance_rule": self.rebalance_rule,
            "policy_version": self.policy_version,
            "schema_version": str(self.schema_version),
        }

    @property
    def definition_hash(self) -> str:
        return _hash_payload(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class BenchmarkSnapshot:
    """Point-in-time benchmark evidence used by downstream state/attribution."""

    benchmark_id: str
    timestamp: datetime
    returns_by_horizon: Mapping[int, float]
    realized_vol_by_horizon: Mapping[int, float]
    breadth: float | None
    breakout_breadth: float | None
    dispersion: float | None
    data_quality: str
    source_hash: str
    policy_version: str = ""
    schema_version: SchemaVersion = field(default_factory=lambda: SchemaVersion("3.0.0"))

    def __post_init__(self) -> None:
        if not self.benchmark_id.strip():
            raise ValueError("benchmark_id must not be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        quality = str(self.data_quality).upper()
        if quality not in _SUPPORTED_QUALITY:
            raise ValueError(f"unsupported data_quality: {self.data_quality}")
        if not self.source_hash.strip():
            raise ValueError("source_hash must not be empty")
        for horizon, value in self.returns_by_horizon.items():
            if int(horizon) <= 0:
                raise ValueError("return horizons must be positive")
            _finite(value, f"return[{horizon}]")
        for horizon, value in self.realized_vol_by_horizon.items():
            if int(horizon) <= 0:
                raise ValueError("volatility horizons must be positive")
            if _finite(value, f"volatility[{horizon}]") < 0:
                raise ValueError("realized volatility must not be negative")
        for name, quality_value in (
            ("breadth", self.breadth),
            ("breakout_breadth", self.breakout_breadth),
        ):
            if quality_value is not None and not 0.0 <= _finite(quality_value, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.dispersion is not None and _finite(self.dispersion, "dispersion") < 0:
            raise ValueError("dispersion must not be negative")
        object.__setattr__(self, "data_quality", quality)
        object.__setattr__(self, "returns_by_horizon", MappingProxyType(dict(self.returns_by_horizon)))
        object.__setattr__(self, "realized_vol_by_horizon", MappingProxyType(dict(self.realized_vol_by_horizon)))

    @property
    def is_verifiable(self) -> bool:
        return self.data_quality == "PASS" and bool(self.returns_by_horizon)


class BenchmarkSnapshotBuilder:
    """Build benchmark snapshots from point-in-time price histories.

    Histories must be ordered oldest-to-newest and contain only data available
    at ``timestamp``.  Missing history is represented as NOT_VERIFIABLE; invalid
    numerical input raises instead of being converted to a fabricated value.
    """

    def __init__(self, definition: BenchmarkDefinition) -> None:
        self.definition = definition

    def build(
        self,
        *,
        timestamp: datetime,
        price_history: Mapping[str | InstrumentId, Sequence[float]],
        horizons: Sequence[int],
        breadth: float | None = None,
        breakout_breadth: float | None = None,
        dispersion: float | None = None,
        data_quality: str = "PASS",
        weights: Mapping[str | InstrumentId, float] | None = None,
    ) -> BenchmarkSnapshot:
        normalized_horizons = self._normalize_horizons(horizons)
        normalized_history = self._normalize_history(price_history)
        source_payload = {
            "definition_hash": self.definition.definition_hash,
            "history": {symbol: list(values) for symbol, values in sorted(normalized_history.items())},
            "horizons": list(normalized_horizons),
            "weights": self._normalize_weights(weights),
            "timestamp": timestamp.isoformat(),
        }
        source_hash = _hash_payload(source_payload)
        quality = str(data_quality).upper()
        if quality not in _SUPPORTED_QUALITY:
            raise ValueError(f"unsupported data_quality: {data_quality}")

        missing = [
            str(component) for component in self.definition.components if str(component) not in normalized_history
        ]
        too_short = [
            str(component)
            for component in self.definition.components
            if str(component) in normalized_history
            and len(normalized_history[str(component)]) <= max(normalized_horizons)
        ]
        if missing or too_short:
            return self._snapshot(
                timestamp=timestamp,
                returns={},
                volatility={},
                breadth=breadth,
                breakout_breadth=breakout_breadth,
                dispersion=dispersion,
                data_quality="NOT_VERIFIABLE",
                source_hash=source_hash,
            )

        resolved_weights = self._resolve_weights(weights)
        returns: dict[int, float] = {}
        volatility: dict[int, float] = {}
        for horizon in normalized_horizons:
            horizon_returns = self._cross_sectional_returns(normalized_history, horizon, resolved_weights)
            returns[horizon] = horizon_returns[-1]
            volatility[horizon] = pstdev(horizon_returns) if len(horizon_returns) > 1 else 0.0

        return self._snapshot(
            timestamp=timestamp,
            returns=returns,
            volatility=volatility,
            breadth=breadth,
            breakout_breadth=breakout_breadth,
            dispersion=dispersion,
            data_quality=quality,
            source_hash=source_hash,
        )

    def _snapshot(
        self,
        *,
        timestamp: datetime,
        returns: Mapping[int, float],
        volatility: Mapping[int, float],
        breadth: float | None,
        breakout_breadth: float | None,
        dispersion: float | None,
        data_quality: str,
        source_hash: str,
    ) -> BenchmarkSnapshot:
        return BenchmarkSnapshot(
            benchmark_id=self.definition.benchmark_id,
            timestamp=timestamp,
            returns_by_horizon=returns,
            realized_vol_by_horizon=volatility,
            breadth=breadth,
            breakout_breadth=breakout_breadth,
            dispersion=dispersion,
            data_quality=data_quality,
            source_hash=source_hash,
            policy_version=self.definition.policy_version,
            schema_version=self.definition.schema_version,
        )

    @staticmethod
    def _normalize_horizons(horizons: Sequence[int]) -> tuple[int, ...]:
        normalized = tuple(sorted({int(horizon) for horizon in horizons}))
        if not normalized or any(horizon <= 0 for horizon in normalized):
            raise ValueError("horizons must contain positive values")
        return normalized

    @staticmethod
    def _normalize_history(price_history: Mapping[str | InstrumentId, Sequence[float]]) -> dict[str, tuple[float, ...]]:
        normalized: dict[str, tuple[float, ...]] = {}
        for raw_symbol, raw_prices in price_history.items():
            symbol = str(raw_symbol).strip()
            if not symbol:
                raise ValueError("price history symbol must not be empty")
            prices = tuple(_finite(price, f"price[{symbol}]") for price in raw_prices)
            if any(price <= 0 for price in prices):
                raise ValueError(f"price[{symbol}] must be positive")
            if symbol in normalized:
                raise ValueError(f"duplicate price history symbol: {symbol}")
            normalized[symbol] = prices
        return normalized

    @staticmethod
    def _normalize_weights(weights: Mapping[str | InstrumentId, float] | None) -> dict[str, float] | None:
        if weights is None:
            return None
        normalized = {str(symbol).strip(): _finite(weight, f"weight[{symbol}]") for symbol, weight in weights.items()}
        if any(weight < 0 for weight in normalized.values()):
            raise ValueError("benchmark weights must not be negative")
        return dict(sorted(normalized.items()))

    def _resolve_weights(self, weights: Mapping[str | InstrumentId, float] | None) -> dict[str, float]:
        components = tuple(str(component) for component in self.definition.components)
        method = self.definition.weighting_method.upper()
        normalized = self._normalize_weights(weights)
        if self.definition.benchmark_type is BenchmarkType.SINGLE_ASSET:
            return {components[0]: 1.0}
        if method == "EQUAL_WEIGHT":
            if normalized is not None:
                raise ValueError("EQUAL_WEIGHT benchmark does not accept custom weights")
            weight = 1.0 / len(components)
            return dict.fromkeys(components, weight)
        if method in {"CUSTOM", "STATIC_WEIGHTS"} and normalized is not None:
            if set(normalized) != set(components):
                raise ValueError("custom benchmark weights must match all components")
            total = sum(normalized.values())
            if total <= 0:
                raise ValueError("custom benchmark weights must have positive total")
            return {component: normalized[component] / total for component in components}
        raise ValueError(f"unsupported universe weighting method: {self.definition.weighting_method}")

    @staticmethod
    def _cross_sectional_returns(
        history: Mapping[str, Sequence[float]], horizon: int, weights: Mapping[str, float]
    ) -> list[float]:
        sample_count = len(next(iter(history.values()))) - horizon
        results: list[float] = []
        for index in range(horizon, horizon + sample_count):
            result = 0.0
            for symbol, weight in weights.items():
                prices = history[symbol]
                result += weight * (prices[index] / prices[index - horizon] - 1.0)
            results.append(result)
        return results
