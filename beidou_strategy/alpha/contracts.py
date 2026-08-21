"""BD-04/05/06 算法合同类型定义。

版本化的 EntryProposal、FilterDecision、Alpha DAG 节点合同。
所有类型绑定 schema version、policy version 和 feature snapshot。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any  # M21: mypy 清偿

from beidou_shared.types import (
    InstrumentId,
    OrderSide,
    SchemaVersion,
    StrategyId,
    VenueId,
)


class FilterDecision(str, Enum):
    ACCEPT = "ACCEPT"
    VETO = "VETO"
    DEGRADE = "DEGRADE"


class MarketDirection(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class MarketStress(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class DataQualityTier(str, Enum):
    PASS = "PASS"  # nosec B105 - data-quality status, not a credential
    DEGRADED = "DEGRADED"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """BD-04: 版本化行情事件合同。"""

    venue_id: VenueId
    instrument_id: InstrumentId
    event_time: datetime
    ingest_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0
    source: str = "REST"
    schema_version: SchemaVersion = SchemaVersion("2.0.0")
    quality: DataQualityTier = DataQualityTier.UNKNOWN
    revision: int = 1


@dataclass(frozen=True, slots=True)
class KLineEvent(MarketEvent):
    """BD-04: K线事件 — 仅 is_closed=True 时发布给策略。"""

    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    timeframe: str = "1h"
    is_closed: bool = False


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """BD-04: 特征快照 — 绑定数据源、特征代码哈希和DQ。"""

    name: str
    values: dict[str, float]
    timestamp: datetime
    instrument_id: InstrumentId
    venue_id: VenueId
    version: SchemaVersion = SchemaVersion("2.0.0")
    source_event_range: tuple[int, int] = (0, 0)
    feature_code_hash: str = ""
    parameter_hash: str = ""
    quality: DataQualityTier = DataQualityTier.UNKNOWN


def _forecast_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_forecast_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _forecast_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _forecast_value(getattr(value, name))
            for name in value.__dataclass_fields__
            if name not in {"timestamp", "ingest_time"}
        }
    return value


def _validate_forecast_float(value: float | None, name: str, *, minimum: float | None = None) -> None:
    if value is None:
        return
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


@dataclass(frozen=True, slots=True)
class AlphaForecast:
    """唯一的 Alpha 输出合同：raw score 之后必须进入收益/成本语义。"""

    alpha_id: str
    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    side: str | OrderSide
    raw_score: float
    expected_return: float | None
    expected_return_after_cost: float | None
    expected_volatility: float | None
    horizon_seconds: int
    probability_positive: float
    confidence: float
    uncertainty: float
    expected_fee_bps: float | None
    expected_slippage_bps: float | None
    expected_funding_bps: float | None
    market_beta: float | None
    regime_fit: float | None
    capacity_score: float | None
    model_version: str | SchemaVersion
    policy_version: str
    feature_hash: str
    timestamp: datetime
    cost_source_hash: str = ""
    schema_version: SchemaVersion = SchemaVersion("3.0.0")

    def __post_init__(self) -> None:
        if not self.alpha_id.strip() or not self.policy_version.strip() or not self.feature_hash.strip():
            raise ValueError("alpha_id, policy_version and feature_hash must be non-empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        for name in (
            "raw_score",
            "expected_return",
            "expected_return_after_cost",
            "expected_volatility",
            "expected_fee_bps",
            "expected_slippage_bps",
            "expected_funding_bps",
            "market_beta",
            "regime_fit",
            "capacity_score",
        ):
            _validate_forecast_float(
                getattr(self, name),
                name,
                minimum=0.0 if name in {"expected_volatility", "expected_fee_bps", "expected_slippage_bps"} else None,
            )
        for name in ("probability_positive", "confidence", "uncertainty", "regime_fit", "capacity_score"):
            raw_value = getattr(self, name)
            if raw_value is None:
                continue
            value = float(raw_value)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        side = self.side.value if isinstance(self.side, Enum) else str(self.side).upper()
        if side not in {"BUY", "SELL", "LONG", "SHORT", "NO_ACTION", "FLAT"}:
            raise ValueError(f"unsupported forecast side: {side}")

    @property
    def total_cost_bps(self) -> float | None:
        values = (self.expected_fee_bps, self.expected_slippage_bps, self.expected_funding_bps)
        if any(value is None for value in values):
            return None
        return sum(float(value) for value in values if value is not None)

    @property
    def cost_verifiable(self) -> bool:
        if not self.cost_source_hash or self.expected_return is None or self.expected_return_after_cost is None:
            return False
        total_cost = self.total_cost_bps
        if total_cost is None:
            return False
        return math.isclose(
            self.expected_return_after_cost,
            self.expected_return - total_cost / 10000.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        )

    @property
    def cost_breakdown(self) -> dict[str, float | str | None]:
        return {
            "expected_fee_bps": self.expected_fee_bps,
            "expected_slippage_bps": self.expected_slippage_bps,
            "expected_funding_bps": self.expected_funding_bps,
            "total_bps": self.total_cost_bps,
            "source_hash": self.cost_source_hash or None,
        }

    def canonical_payload(self) -> dict[str, object]:
        return {
            "alpha_id": self.alpha_id,
            "strategy_id": str(self.strategy_id),
            "instrument_id": str(self.instrument_id),
            "venue_id": str(self.venue_id),
            "side": self.side.value if isinstance(self.side, Enum) else str(self.side),
            "raw_score": self.raw_score,
            "expected_return": self.expected_return,
            "expected_return_after_cost": self.expected_return_after_cost,
            "expected_volatility": self.expected_volatility,
            "horizon_seconds": self.horizon_seconds,
            "probability_positive": self.probability_positive,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "expected_fee_bps": self.expected_fee_bps,
            "expected_slippage_bps": self.expected_slippage_bps,
            "expected_funding_bps": self.expected_funding_bps,
            "market_beta": self.market_beta,
            "regime_fit": self.regime_fit,
            "capacity_score": self.capacity_score,
            "model_version": str(self.model_version),
            "policy_version": self.policy_version,
            "feature_hash": self.feature_hash,
            "cost_source_hash": self.cost_source_hash,
            "schema_version": str(self.schema_version),
        }

    @property
    def forecast_hash(self) -> str:
        payload = json.dumps(
            _forecast_value(self.canonical_payload()), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class EnsembleComponent:
    """One alpha's observable contribution to an ensemble forecast."""

    alpha_id: str
    weight: float
    expected_return: float | None
    contribution: float | None
    regime_fit: float
    reliability: float

    def __post_init__(self) -> None:
        if not self.alpha_id.strip():
            raise ValueError("alpha_id must be non-empty")
        _validate_forecast_float(self.weight, "weight", minimum=0.0)
        _validate_forecast_float(self.expected_return, "expected_return")
        _validate_forecast_float(self.contribution, "contribution")
        for name in ("regime_fit", "reliability"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class EnsembleForecast:
    """Order-independent, contribution-preserving forecast fusion contract."""

    instrument_id: InstrumentId
    venue_id: VenueId
    expected_return: float | None
    expected_return_after_cost: float | None
    expected_volatility: float | None
    uncertainty: float
    conflict_score: float
    components: tuple[EnsembleComponent, ...]
    model_version: str | SchemaVersion
    timestamp: datetime
    policy_version: str = "ensemble-forecast-policy-v1"
    schema_version: SchemaVersion = SchemaVersion("3.0.0")

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")
        for name in ("expected_return", "expected_return_after_cost", "expected_volatility"):
            _validate_forecast_float(getattr(self, name), name, minimum=0.0 if name == "expected_volatility" else None)
        for name in ("uncertainty", "conflict_score"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        object.__setattr__(self, "components", tuple(self.components))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "instrument_id": str(self.instrument_id),
            "venue_id": str(self.venue_id),
            "expected_return": self.expected_return,
            "expected_return_after_cost": self.expected_return_after_cost,
            "expected_volatility": self.expected_volatility,
            "uncertainty": self.uncertainty,
            "conflict_score": self.conflict_score,
            "components": [
                {
                    "alpha_id": component.alpha_id,
                    "weight": component.weight,
                    "expected_return": component.expected_return,
                    "contribution": component.contribution,
                    "regime_fit": component.regime_fit,
                    "reliability": component.reliability,
                }
                for component in sorted(self.components, key=lambda item: item.alpha_id)
            ],
            "model_version": str(self.model_version),
            "policy_version": self.policy_version,
            "schema_version": str(self.schema_version),
        }

    @property
    def forecast_hash(self) -> str:
        payload = json.dumps(
            _forecast_value(self.canonical_payload()), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# M06-F03: DEGRADE 衰减乘数统一常量 —— 生产 TypedAlphaGraph 与
# TypedStrategyKernel 必须使用同一值(旧实现两处硬编码且语义不一致)。
DEGRADE_MULTIPLIER: float = 0.5


@dataclass(frozen=True, slots=True)
class EntryProposal:
    """BD-T05: 入场提案 — 由 Entry Alpha 节点生成。side 替换遗留 direction:str。"""

    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    side: OrderSide | None = None  # BD-T05: 类型化 side (None=NO_ACTION/BOTH)
    strength: float = 0.0  # [0, 1]
    confidence: float = 0.0  # [0, 1]
    z_score: float | None = None
    half_life_hours: float | None = None
    no_trade_band_pct: float = 0.0
    model_version: SchemaVersion = SchemaVersion("2.0.0")
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FilterResult:
    """BD-05: 过滤器决策 — Filter 节点输出。"""

    decision: FilterDecision
    confidence_multiplier: float = 1.0
    size_multiplier: float = 1.0
    reason_codes: list[str] = field(default_factory=list)
    component_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """BD-T05: 策略最终提案 — DAG 执行结果。side 替换遗留 direction:str。"""

    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    side: OrderSide | None = None  # BD-T05: 类型化 side (None=NO_ACTION/BOTH)
    strength: float = 0.0
    confidence: float = 0.0
    entry_proposals: list[EntryProposal] = field(default_factory=list)
    filter_results: list[FilterResult] = field(default_factory=list)
    conflict_detected: bool = False
    checksum: str = ""
    policy_version: str = ""
    feature_snapshot_ref: str = ""

    def hash(self) -> str:
        """BD-T05: stable hash over the complete final proposal contract."""

        def canonical(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {str(key): canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
            if isinstance(value, (list, tuple)):
                return [canonical(item) for item in value]
            if hasattr(value, "__dataclass_fields__"):
                return {
                    name: canonical(getattr(value, name))
                    for name in value.__dataclass_fields__
                    if name not in {"timestamp", "ingest_time"}
                }
            return value

        payload = json.dumps(canonical(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PortfolioDecision:
    """BD-06: 组合决策 — 优化器输出。"""

    strategy_id: StrategyId
    targets: dict[InstrumentId, float]  # symbol → target_position
    rejected: dict[InstrumentId, str]  # symbol → rejection_reason
    constraint_shadow_prices: dict[str, float]
    input_version: str
    total_risk_pct: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0


@dataclass(frozen=True, slots=True)
class AccountFactSnapshot:
    """BD-07: 版本化账户事实快照。"""

    account_id: str
    venue_id: VenueId
    balance: float
    equity: float
    margin_used: float
    margin_ratio: float
    positions: dict[InstrumentId, float]
    open_orders: list[str]
    can_trade: bool = False
    can_withdraw: bool = False
    ip_whitelisted: bool = False
    fact_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    freshness_seconds: float = 0.0
    version: str = "2.0.0"


@dataclass(frozen=True, slots=True)
class RiskRuleResult:
    """BD-07: R0-R10 单条规则结果。"""

    rule_id: str
    decision: str  # PASS / REJECT / UNKNOWN
    reason: str
    policy_version: str
    facts_version: str
    remediation: str | None = None
