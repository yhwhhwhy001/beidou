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
    source_hash: str = ""
    policy_version: str = "derivative-risk-policy-v3-v1"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not math.isfinite(self.predicted_rate) or not math.isfinite(self.annualized_rate_pct):
            raise ValueError("funding rates must be finite")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")

    def net_return_adjustment(self, position_sign: float) -> float:
        """Return funding's signed return adjustment for a long/short position."""
        if not math.isfinite(position_sign) or position_sign not in {-1.0, 0.0, 1.0}:
            raise ValueError("position_sign must be -1, 0 or 1")
        return -position_sign * self.predicted_rate


@dataclass(frozen=True, slots=True)
class LiquidationCascadeRisk:
    venue_id: VenueId
    instrument_id: InstrumentId
    long_liq_cluster_notional: float = 0.0
    short_liq_cluster_notional: float = 0.0
    cascade_probability: float = 0.0
    source_hash: str = ""
    policy_version: str = "derivative-risk-policy-v3-v1"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        notionals = (self.long_liq_cluster_notional, self.short_liq_cluster_notional)
        if any(not math.isfinite(value) or value < 0 for value in notionals):
            raise ValueError("liquidation notionals must be finite and non-negative")
        if not math.isfinite(self.cascade_probability) or not 0.0 <= self.cascade_probability <= 1.0:
            raise ValueError("cascade_probability must be between 0 and 1")
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")

    @property
    def stress_scalar(self) -> float:
        """Monotone risk scalar; cascade risk cannot increase exposure."""
        return 1.0 - self.cascade_probability


@dataclass(frozen=True, slots=True)
class DerivativeRiskContext:
    """Bound funding/liquidation facts for forecast and exposure consumers."""

    venue_id: VenueId
    instrument_id: InstrumentId
    funding_return_adjustment: float | None
    liquidation_stress_scalar: float
    source_hash: str
    policy_version: str
    timestamp: datetime

    @classmethod
    def from_signals(
        cls,
        *,
        funding: FundingRateSignal | None,
        liquidation: LiquidationCascadeRisk | None,
        position_sign: float,
    ) -> DerivativeRiskContext:
        signals = tuple(signal for signal in (funding, liquidation) if signal is not None)
        if not signals:
            raise ValueError("at least one derivative risk signal is required")
        venue_id = signals[0].venue_id
        instrument_id = signals[0].instrument_id
        if any(signal.venue_id != venue_id or signal.instrument_id != instrument_id for signal in signals):
            raise ValueError("derivative risk signals must share venue and instrument")
        funding_adjustment = funding.net_return_adjustment(position_sign) if funding is not None else None
        stress_scalar = liquidation.stress_scalar if liquidation is not None else 1.0
        source_hashes = [signal.source_hash.strip() for signal in signals]
        source_hash = "|".join(source_hashes) if all(source_hashes) else ""
        policy_versions = {signal.policy_version for signal in signals}
        policy_version = "|".join(sorted(policy_versions))
        timestamps = [signal.timestamp for signal in signals]
        timestamp = max(timestamps)
        return cls(
            venue_id=venue_id,
            instrument_id=instrument_id,
            funding_return_adjustment=funding_adjustment,
            liquidation_stress_scalar=stress_scalar,
            source_hash=source_hash,
            policy_version=policy_version,
            timestamp=timestamp,
        )

    @property
    def is_verifiable(self) -> bool:
        return bool(self.source_hash and self.policy_version)


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
