"""BD-04/05/06 算法合同类型定义。

版本化的 EntryProposal、FilterDecision、Alpha DAG 节点合同。
所有类型绑定 schema version、policy version 和 feature snapshot。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

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
    PASS = "PASS"
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

        def canonical(value):
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
