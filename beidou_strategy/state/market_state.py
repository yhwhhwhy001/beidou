"""三维市场状态评估。方向、压力、数据质量三个正交维度。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import InstrumentId, VenueId


@dataclass(frozen=True, slots=True)
class DirectionState:
    regime: str  # TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, UNKNOWN
    probability: float
    uncertainty: float
    model_fallback: bool = False


@dataclass(frozen=True, slots=True)
class StressState:
    level: str  # NORMAL, ELEVATED, HIGH, CRISIS, UNKNOWN
    probability: float
    drawdown_from_peak_pct: float | None = None
    vol_regime: str | None = None
    correlation_regime: str | None = None


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

    def should_override_direction(self) -> bool:
        """压力危机或质量不可信时必须覆盖方向信号。"""
        return self.stress.level in ("CRISIS",) or self.quality.tier in ("UNRELIABLE", "UNKNOWN")

    def is_tradable(self) -> bool:
        if self.should_override_direction():
            return False
        if self.stress.level == "UNKNOWN" or self.quality.tier == "UNKNOWN":
            return False
        return not self.rollback_rate_pct > 50.0


class MarketStateEstimator:
    """市场状态评估器。正交维度输出，监控回退率。"""

    def __init__(self) -> None:
        self._rollback_count: int = 0
        self._total_predictions: int = 0

    def estimate(self, venue_id: VenueId, instrument_id: InstrumentId, features: dict[str, float]) -> MarketStateVector:
        direction = DirectionState(regime="UNKNOWN", probability=0.5, uncertainty=1.0, model_fallback=True)
        stress = StressState(level="UNKNOWN", probability=0.5)
        quality = QualityState(tier="UNKNOWN")
        self._total_predictions += 1
        return MarketStateVector(
            direction=direction,
            stress=stress,
            quality=quality,
            venue_id=venue_id,
            instrument_id=instrument_id,
            rollback_rate_pct=self.rollback_rate(),
        )

    def record_rollback(self) -> None:
        self._rollback_count += 1

    def rollback_rate(self) -> float:
        if self._total_predictions == 0:
            return 0.0
        return self._rollback_count / self._total_predictions * 100.0
