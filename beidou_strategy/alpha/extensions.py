"""合约原生扩展槽位。资金费/基差、跨所永续价差、强平级联、稳定币风险过滤。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import InstrumentId, VenueId


@dataclass(frozen=True, slots=True)
class FundingRateSignal:
    venue_id: VenueId
    instrument_id: InstrumentId
    predicted_rate: float
    annualized_rate_pct: float
    next_funding_time: datetime | None = None
    confidence: float = 0.5
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not math.isfinite(self.predicted_rate) or not math.isfinite(self.annualized_rate_pct):
            raise ValueError("funding rates must be finite")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class LiquidationCascadeRisk:
    venue_id: VenueId
    instrument_id: InstrumentId
    long_liq_cluster_notional: float = 0.0
    short_liq_cluster_notional: float = 0.0
    cascade_probability: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        notionals = (self.long_liq_cluster_notional, self.short_liq_cluster_notional)
        if any(not math.isfinite(value) or value < 0 for value in notionals):
            raise ValueError("liquidation notionals must be finite and non-negative")
        if not math.isfinite(self.cascade_probability) or not 0.0 <= self.cascade_probability <= 1.0:
            raise ValueError("cascade_probability must be between 0 and 1")


class ContractExtension(ABC):
    """合约原生扩展槽位基类。V2 不交付现货下单。"""

    @abstractmethod
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict: ...


class FundingRateExtension(ContractExtension):
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict:
        return {
            "type": "funding_rate",
            "warning": "Funding rate is a net-alpha input, not a mechanical position signal",
        }


class LiquidationCascadeExtension(ContractExtension):
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict:
        return {"type": "liquidation_cascade", "warning": "Monitor liquidation clusters for cascade risk"}
