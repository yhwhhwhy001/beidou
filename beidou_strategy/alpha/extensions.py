
"""合约原生扩展槽位。资金费/基差、跨所永续价差、强平级联、稳定币风险过滤。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from abc import ABC, abstractmethod
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

@dataclass(frozen=True, slots=True)
class LiquidationCascadeRisk:
    venue_id: VenueId
    instrument_id: InstrumentId
    long_liq_cluster_notional: float = 0.0
    short_liq_cluster_notional: float = 0.0
    cascade_probability: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

class ContractExtension(ABC):
    """合约原生扩展槽位基类。V2 不交付现货下单。"""
    @abstractmethod
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict: ...

class FundingRateExtension(ContractExtension):
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict:
        return {"type": "funding_rate", "warning": "Funding rate is a net-alpha input, not a mechanical position signal"}

class LiquidationCascadeExtension(ContractExtension):
    async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict:
        return {"type": "liquidation_cascade", "warning": "Monitor liquidation clusters for cascade risk"}
