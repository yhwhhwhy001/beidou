
"""Meta-labeling、模型可靠度、冲突检测与信号融合。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from beidou_shared.types import InstrumentId, ModelId, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import AlphaSignal, SignalDirection

@dataclass(frozen=True, slots=True)
class FusedSignal:
    instrument_id: InstrumentId
    venue_id: VenueId
    direction: SignalDirection
    strength: float
    confidence: float
    contributing_signals: list[AlphaSignal] = field(default_factory=list)
    meta_label: str | None = None  # meta-labeler output
    model_reliability: float = 1.0
    conflict_detected: bool = False
    conflict_detail: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

class SignalFuser:
    """信号融合器。Meta-labeling、冲突检测、可靠度加权。"""
    def __init__(self, min_agreement_ratio: float = 0.6) -> None:
        self.min_agreement_ratio = min_agreement_ratio

    def detect_conflict(self, signals: list[AlphaSignal]) -> tuple[bool, str]:
        if len(signals) < 2:
            return False, ""
        directions = {s.direction for s in signals}
        if SignalDirection.LONG in directions and SignalDirection.SHORT in directions:
            longs = sum(1 for s in signals if s.direction == SignalDirection.LONG)
            shorts = sum(1 for s in signals if s.direction == SignalDirection.SHORT)
            return True, f"LONG={longs} vs SHORT={shorts}"
        return False, ""

    def fuse(self, signals: list[AlphaSignal]) -> FusedSignal:
        if not signals:
            return FusedSignal(instrument_id=InstrumentId("UNKNOWN"), venue_id=VenueId("UNKNOWN"), direction=SignalDirection.NO_ACTION, strength=0.0, confidence=0.0)
        conflict, detail = self.detect_conflict(signals)

        # 应用方向符号: LONG → +1, SHORT → -1, NO_ACTION → 0
        def _direction_sign(s: AlphaSignal) -> float:
            if s.direction == SignalDirection.LONG:
                return 1.0
            elif s.direction == SignalDirection.SHORT:
                return -1.0
            return 0.0

        # 权重 = confidence × strength × direction_sign
        weights = [s.confidence * s.strength * _direction_sign(s) for s in signals]
        total_weight = sum(abs(w) for w in weights)
        if total_weight == 0:
            return FusedSignal(instrument_id=signals[0].instrument_id, venue_id=signals[0].venue_id, direction=SignalDirection.NO_ACTION, strength=0.0, confidence=0.0, contributing_signals=signals, conflict_detected=conflict, conflict_detail=detail)
        net_score = sum(w for w in weights) / total_weight
        direction = SignalDirection.LONG if net_score > 0 else SignalDirection.SHORT if net_score < 0 else SignalDirection.NO_ACTION
        confidence = sum(s.confidence for s in signals) / len(signals)
        return FusedSignal(instrument_id=signals[0].instrument_id, venue_id=signals[0].venue_id, direction=direction, strength=abs(net_score), confidence=confidence, contributing_signals=signals, conflict_detected=conflict, conflict_detail=detail)
