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
    SEQUENCE = "SEQUENCE"  # BD-04: 序列连续性
    CROSS_SOURCE = "CROSS_SOURCE"  # BD-04: 跨源偏差
    OUTLIER = "OUTLIER"  # BD-04: 异常值检测
    CLOCK_SKEW = "CLOCK_SKEW"  # BD-04: 时钟偏差
    SCHEMA_VALIDITY = "SCHEMA_VALIDITY"  # BD-04: Schema 版本校验


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
        # BLOCK: 任一检查为 FAIL → 阻止交易
        if any(c.tier == DataQualityTier.FAIL for c in self.checks):
            return DataQualityTier.FAIL
        # UNKNOWN: 无 PASS 但有 CONDITIONAL → 不可交易
        has_pass = any(c.tier == DataQualityTier.PASS for c in self.checks)
        if not has_pass and any(c.tier == DataQualityTier.CONDITIONAL for c in self.checks):
            return DataQualityTier.CONDITIONAL
        # DEGRADED: 部分 CONDITIONAL 但仍可研究
        if any(c.tier == DataQualityTier.CONDITIONAL for c in self.checks):
            return DataQualityTier.CONDITIONAL
        return DataQualityTier.PASS

    def is_safe_for_trading(self) -> bool:
        return self.overall_tier() == DataQualityTier.PASS

    def is_safe_for_research(self) -> bool:
        return self.overall_tier() not in (DataQualityTier.FAIL,)

    def add_freshness_check(self, age_seconds: float, max_age: float) -> None:
        tier = DataQualityTier.PASS if age_seconds < max_age else DataQualityTier.FAIL
        self.checks.append(
            DQCheckResult(
                DQCheckType.FRESHNESS,
                tier,
                f"age={age_seconds:.0f}s max={max_age:.0f}s",
                metric_value=age_seconds,
                threshold=max_age,
            )
        )

    def add_sequence_check(self, expected_seq: int, actual_seq: int) -> None:
        tier = DataQualityTier.PASS if actual_seq == expected_seq else DataQualityTier.FAIL
        self.checks.append(
            DQCheckResult(
                DQCheckType.SEQUENCE,
                tier,
                f"expected_seq={expected_seq} actual={actual_seq}",
                metric_value=actual_seq,
                threshold=expected_seq,
            )
        )

    def add_outlier_check(self, value: float, mean: float, std: float, max_sigma: float = 5.0) -> None:
        if std == 0:
            return
        sigma = abs(value - mean) / std
        tier = DataQualityTier.PASS if sigma < max_sigma else DataQualityTier.CONDITIONAL
        self.checks.append(
            DQCheckResult(
                DQCheckType.OUTLIER, tier, f"sigma={sigma:.1f} max={max_sigma}", metric_value=sigma, threshold=max_sigma
            )
        )

    def add_clock_skew_check(self, skew_ms: int, max_skew_ms: int = 5000) -> None:
        tier = DataQualityTier.PASS if abs(skew_ms) < max_skew_ms else DataQualityTier.FAIL
        self.checks.append(
            DQCheckResult(
                DQCheckType.CLOCK_SKEW,
                tier,
                f"skew={skew_ms}ms max={max_skew_ms}ms",
                metric_value=skew_ms,
                threshold=max_skew_ms,
            )
        )


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
        for _name, entries in self._sources.items():
            for entry in entries:
                if field in entry:
                    values.append(entry[field])
        if len({str(v) for v in values}) == 1:
            return DQCheckResult(
                DQCheckType.CONSISTENCY,
                DataQualityTier.PASS,
                f"Field '{field}' consistent across {len(values)} sources",
            )
        return DQCheckResult(
            DQCheckType.CONSISTENCY, DataQualityTier.FAIL, f"Field '{field}' inconsistent across sources"
        )


class AutoRepair:
    """自动修复模块。只能修复确定性、可逆的数据质量问题。"""

    @staticmethod
    def repair_gap(series: list[dict], timestamp_field: str, expected_interval_ms: int) -> tuple[list[dict], list[str]]:
        """检测并标记数据缺口。不猜测填充。"""
        gaps: list[str] = []
        if len(series) < 2:
            return series, gaps
        for i in range(1, len(series)):
            t1 = series[i - 1][timestamp_field]
            t2 = series[i][timestamp_field]
            if isinstance(t1, datetime) and isinstance(t2, datetime):
                diff_ms = (t2 - t1).total_seconds() * 1000
                if diff_ms > expected_interval_ms * 1.5:
                    gaps.append(f"Gap of {diff_ms}ms between index {i - 1} and {i}")
        return series, gaps
