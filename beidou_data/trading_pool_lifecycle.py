"""交易池动态生命周期管理 — BD-04。

根据点差、深度、成交量、上市时间、规则稳定性、拒单率和容量动态管理标的生命周期。
状态: OBSERVING → PROMOTED → ACTIVE → QUARANTINED → DELISTED。
有观察期证据、迟滞和自动降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum


class PoolStatus(str, Enum):
    OBSERVING = "OBSERVING"    # 观察期，不可交易
    PROMOTED = "PROMOTED"      # 晋级中，只读
    ACTIVE = "ACTIVE"          # 可交易
    QUARANTINED = "QUARANTINED"  # 隔离，仅平仓
    DELISTED = "DELISTED"      # 下架


@dataclass
class InstrumentScore:
    """标的综合评分。"""
    instrument_id: str
    spread_score: float = 0.0     # 点差评分 (0-1, 越高越好)
    depth_score: float = 0.0      # 深度评分
    volume_score: float = 0.0     # 成交量评分
    stability_score: float = 0.0  # 稳定性评分 (拒单率等)
    capacity_score: float = 0.0   # Alpha容量评分
    overall: float = 0.0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def compute_overall(self) -> float:
        weights = {"spread": 0.25, "depth": 0.25, "volume": 0.20,
                   "stability": 0.15, "capacity": 0.15}
        self.overall = (
            self.spread_score * weights["spread"] +
            self.depth_score * weights["depth"] +
            self.volume_score * weights["volume"] +
            self.stability_score * weights["stability"] +
            self.capacity_score * weights["capacity"]
        )
        return self.overall


@dataclass
class PoolEntry:
    """交易池条目。"""
    instrument_id: str
    status: PoolStatus = PoolStatus.OBSERVING
    observing_since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    promoted_at: datetime | None = None
    quarantine_reason: str | None = None
    scores: list[InstrumentScore] = field(default_factory=list)
    min_observation_hours: float = 24.0  # 最少观察24小时


class TradingPool:
    """交易池 — 点时宇宙管理。

    特性:
    - 有观察期证据才能 promote（不能任意直接 promote）
    - 自动降级：评分连续低于阈值→QUARANTINED
    - 迟滞：降级后需要持续改进才能重新 promote
    """

    PROMOTE_THRESHOLD = 0.6   # 综合评分 >= 0.6 才考虑晋级
    DEGRADE_THRESHOLD = 0.3   # 综合评分 < 0.3 触发降级
    DEGRADE_CONSECUTIVE = 3   # 连续3次低于阈值才降级（迟滞）

    def __init__(self, max_instruments: int = 50):
        self._pool: dict[str, PoolEntry] = {}
        self._max_instruments = max_instruments

    def add(self, instrument_id: str) -> PoolEntry:
        if instrument_id not in self._pool:
            entry = PoolEntry(instrument_id=instrument_id)
            self._pool[instrument_id] = entry
        return self._pool[instrument_id]

    def score(self, instrument_id: str, score: InstrumentScore) -> None:
        entry = self._pool.get(instrument_id)
        if not entry:
            return
        score.compute_overall()
        entry.scores.append(score)

        # 自动降级检查
        if entry.status == PoolStatus.ACTIVE:
            recent = entry.scores[-self.DEGRADE_CONSECUTIVE:]
            if len(recent) >= self.DEGRADE_CONSECUTIVE:
                if all(s.overall < self.DEGRADE_THRESHOLD for s in recent):
                    entry.status = PoolStatus.QUARANTINED
                    entry.quarantine_reason = f"Score below {self.DEGRADE_THRESHOLD} for {self.DEGRADE_CONSECUTIVE} consecutive evaluations"

    def try_promote(self, instrument_id: str) -> bool:
        """尝试晋级。需要观察期满 + 评分达标。"""
        entry = self._pool.get(instrument_id)
        if not entry or entry.status != PoolStatus.OBSERVING:
            return False

        # 观察期检查
        elapsed = (datetime.now(timezone.utc) - entry.observing_since).total_seconds() / 3600
        if elapsed < entry.min_observation_hours:
            return False

        # 评分检查
        if not entry.scores:
            return False
        latest = entry.scores[-1]
        if latest.overall < self.PROMOTE_THRESHOLD:
            return False

        entry.status = PoolStatus.PROMOTED
        entry.promoted_at = datetime.now(timezone.utc)
        return True

    def activate(self, instrument_id: str) -> bool:
        entry = self._pool.get(instrument_id)
        if not entry or entry.status != PoolStatus.PROMOTED:
            return False
        if self.active_count() >= self._max_instruments:
            return False
        entry.status = PoolStatus.ACTIVE
        return True

    def quarantine(self, instrument_id: str, reason: str) -> None:
        entry = self._pool.get(instrument_id)
        if entry:
            entry.status = PoolStatus.QUARANTINED
            entry.quarantine_reason = reason

    def active_instruments(self) -> list[str]:
        return [iid for iid, e in self._pool.items()
                if e.status == PoolStatus.ACTIVE]

    def active_count(self) -> int:
        return len(self.active_instruments())

    def is_tradable(self, instrument_id: str) -> bool:
        entry = self._pool.get(instrument_id)
        return entry is not None and entry.status == PoolStatus.ACTIVE
