"""BD-CV11/12/13: Canonical Market Event, DQSnapshot, Point-in-Time Universe.

Wave 1 Data Truth contracts — 唯一可重放行情事实链。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

# ============================================================================
# BD-CV11: Canonical Market Event & ClosedBar/KLine
# ============================================================================


class EventType(str, Enum):
    TICK = "TICK"
    CANONICAL_MARKET_EVENT = "CANONICAL_MARKET_EVENT"
    CLOSED_BAR = "CLOSED_BAR"
    GAP = "GAP"
    LATE = "LATE"
    DUPLICATE = "DUPLICATE"
    REVISION = "REVISION"


@dataclass(frozen=True)
class CanonicalMarketEvent:
    """BD-CV11: 可重放行情事件。

    5m 10:07 tick 必须属于 10:05 bucket。
    gap/late/duplicate/revision 均有确定状态和证据。
    """

    symbol: str
    event_type: EventType
    timestamp: float
    bucket_start: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = False
    event_hash: str = ""

    def compute_hash(self) -> str:
        data = {
            "symbol": self.symbol,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "bucket_start": self.bucket_start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def floor_to_bucket(timestamp: float, interval_seconds: int = 300) -> float:
        """BD-CV11: 将时间戳对齐到 interval bucket。

        10:07 → 10:05 (5-min interval)
        """
        return (timestamp // interval_seconds) * interval_seconds


# ============================================================================
# BD-CV12: DQSnapshot & CanonicalFeatureEngine
# ============================================================================


class DQStatus(str, Enum):
    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DQCheck:
    """单个数据质量检查结果。"""

    check_id: str
    name: str
    status: DQStatus
    detail: str = ""


@dataclass(frozen=True)
class DQSnapshot:
    """BD-CV12: 数据质量快照。

    缺少任一 required DQ check 不能 PASS。
    """

    symbol: str
    observed_at: str = ""
    checks: list[DQCheck] = field(default_factory=list)
    warmup_complete: bool = False
    snapshot_hash: str = ""

    REQUIRED_CHECKS: ClassVar[list[str]] = [
        "dq.price.monotonic",
        "dq.volume.nonzero",
        "dq.timestamp.no_future",
        "dq.timestamp.no_gap_gt_5min",
        "dq.ohlcv.complete",
    ]

    def all_required_pass(self) -> bool:
        passed_ids = {c.check_id for c in self.checks if c.status == DQStatus.PASS}
        return all(rid in passed_ids for rid in self.REQUIRED_CHECKS)

    def has_warmup_data(self) -> bool:
        """RSI warmup — 确保有足够数据点。"""
        return self.warmup_complete and self.all_required_pass()

    def compute_hash(self) -> str:
        data = {
            "symbol": self.symbol,
            "observed_at": self.observed_at,
            "warmup_complete": self.warmup_complete,
            "checks": [{"check_id": c.check_id, "status": c.status.value} for c in self.checks],
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ============================================================================
# BD-CV13: Point-in-Time Trading Universe
# ============================================================================


@dataclass(frozen=True)
class UniverseEntry:
    """单个交易对的 PIT 状态。

    M02-F03: funding_rate/open_interest/capacity_score 用 None 表示
    "未知/未提供" —— 旧实现以 0.0 作 UNKNOWN 哨兵，把合法的零费率
    （市场常态）与零持仓量误判为关键字段缺失。
    """

    symbol: str
    listing_age_days: float = 0.0
    funding_rate: float | None = None
    open_interest: float | None = None
    capacity_score: float | None = None
    dq_ok: bool = False
    is_executable: bool = False
    exclude_reason: str = ""


@dataclass(frozen=True)
class PITUniverseSnapshot:
    """BD-CV13: 点时交易池快照。

    回测每个 bar/decision 可追溯到 PIT UniverseSnapshot。
    listing age/funding/OI/capacity/DQ 关键 UNKNOWN 时不允许晋级。
    """

    universe_id: str = ""
    observed_at: str = ""
    entries: list[UniverseEntry] = field(default_factory=list)
    snapshot_hash: str = ""

    def executable_symbols(self) -> list[str]:
        return [e.symbol for e in self.entries if e.is_executable]

    def blocked_symbols(self) -> dict[str, str]:
        return {e.symbol: e.exclude_reason for e in self.entries if not e.is_executable and e.exclude_reason}

    def any_unknown_critical(self) -> list[str]:
        """返回关键字段 UNKNOWN（None）的 symbol 列表。

        M02-F03: 未知语义由 None 承载 —— 合法的零费率/零 OI 不再误报。
        """
        issues = []
        for e in self.entries:
            if e.funding_rate is None and not e.exclude_reason:
                issues.append(f"{e.symbol}:funding_rate")
            if e.open_interest is None and not e.exclude_reason:
                issues.append(f"{e.symbol}:open_interest")
            if e.capacity_score is None and not e.exclude_reason:
                issues.append(f"{e.symbol}:capacity_score")
        return issues

    def compute_hash(self) -> str:
        data = {
            "universe_id": self.universe_id,
            "observed_at": self.observed_at,
            "entries": [
                {
                    "symbol": e.symbol,
                    "listing_age_days": e.listing_age_days,
                    "funding_rate": e.funding_rate,
                    "open_interest": e.open_interest,
                    "capacity_score": e.capacity_score,
                    "dq_ok": e.dq_ok,
                    "is_executable": e.is_executable,
                }
                for e in self.entries
            ],
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
