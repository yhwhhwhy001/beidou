
"""数据质量三态门禁、跨源校验与自动修复。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from beidou_shared.types import CorrelationId, DataQualityTier, VenueInstrument

class DQCheckType(str, Enum):
    FRESHNESS = "FRESHNESS"
    COMPLETENESS = "COMPLETENESS"
    CONSISTENCY = "CONSISTENCY"
    ACCURACY = "ACCURACY"
    UNIQUENESS = "UNIQUENESS"
    TIMELINESS = "TIMELINESS"

@dataclass(frozen=True, slots=True)
class DQCheckResult:
    check_type: DQCheckType
    tier: DataQualityTier
    detail: str
    metric_value: float | None = None
    threshold: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class DataQualityGate:
    """数据质量三态门禁。PASS/CONDITIONAL/FAIL。"""
    checks: list[DQCheckResult] = field(default_factory=list)
    venue_instrument: VenueInstrument | None = None
    correlation_id: CorrelationId | None = None

    def overall_tier(self) -> DataQualityTier:
        if not self.checks:
            return DataQualityTier.FAIL
        if any(c.tier == DataQualityTier.FAIL for c in self.checks):
            return DataQualityTier.FAIL
        if any(c.tier == DataQualityTier.CONDITIONAL for c in self.checks):
            return DataQualityTier.CONDITIONAL
        return DataQualityTier.PASS

    def is_safe_for_trading(self) -> bool:
        return self.overall_tier() == DataQualityTier.PASS

    def is_safe_for_research(self) -> bool:
        return self.overall_tier() != DataQualityTier.FAIL

class CrossSourceValidator:
    """跨源数据校验器。"""
    def __init__(self) -> None:
        self._sources: dict[str, list[dict[str, Any]]] = {}

    def add_source(self, name: str, data: dict[str, Any]) -> None:
        if name not in self._sources:
            self._sources[name] = []
        self._sources[name].append(data)

    def validate_consistency(self, field: str) -> DQCheckResult:
        values: list[Any] = []
        for name, entries in self._sources.items():
            for entry in entries:
                if field in entry:
                    values.append(entry[field])
        if len(set(str(v) for v in values)) == 1:
            return DQCheckResult(DQCheckType.CONSISTENCY, DataQualityTier.PASS, f"Field '{field}' consistent across {len(values)} sources")
        return DQCheckResult(DQCheckType.CONSISTENCY, DataQualityTier.FAIL, f"Field '{field}' inconsistent across sources")

class AutoRepair:
    """自动修复模块。只能修复确定性、可逆的数据质量问题。"""
    @staticmethod
    def repair_gap(series: list[dict], timestamp_field: str, expected_interval_ms: int) -> tuple[list[dict], list[str]]:
        """检测并标记数据缺口。不猜测填充。"""
        gaps: list[str] = []
        if len(series) < 2:
            return series, gaps
        for i in range(1, len(series)):
            t1 = series[i-1][timestamp_field]
            t2 = series[i][timestamp_field]
            if isinstance(t1, datetime) and isinstance(t2, datetime):
                diff_ms = (t2 - t1).total_seconds() * 1000
                if diff_ms > expected_interval_ms * 1.5:
                    gaps.append(f"Gap of {diff_ms}ms between index {i-1} and {i}")
        return series, gaps
