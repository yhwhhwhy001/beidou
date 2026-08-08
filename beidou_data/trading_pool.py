"""Venue×Instrument 动态交易池、生命周期与容量门禁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import ResultStatus, VenueInstrument


class PoolLifecycle(str, Enum):
    ACTIVE = "ACTIVE"
    WARMUP = "WARMUP"
    EXIT_ONLY = "EXIT_ONLY"
    QUARANTINED = "QUARANTINED"
    INACTIVE = "INACTIVE"


@dataclass
class TradingPoolEntry:
    venue_instrument: VenueInstrument
    lifecycle: PoolLifecycle = PoolLifecycle.WARMUP
    max_position_notional: float = 0.0
    max_order_size: float = 0.0
    capacity_used_pct: float = 0.0
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_traded_at: datetime | None = None


class TradingPoolManager:
    """动态交易池管理器。容量门禁，生命周期控制。"""

    def __init__(self) -> None:
        self._pool: dict[str, TradingPoolEntry] = {}

    def _key(self, vi: VenueInstrument) -> str:
        return f"{vi.venue_id}:{vi.instrument_id}"

    def add_instrument(self, vi: VenueInstrument, max_notional: float, max_order_size: float) -> ResultStatus:
        key = self._key(vi)
        if key in self._pool:
            return ResultStatus.ERROR
        self._pool[key] = TradingPoolEntry(vi, max_position_notional=max_notional, max_order_size=max_order_size)
        return ResultStatus.SUCCESS

    def promote(self, vi: VenueInstrument, target: PoolLifecycle) -> ResultStatus:
        key = self._key(vi)
        entry = self._pool.get(key)
        if entry is None:
            return ResultStatus.UNKNOWN
        entry.lifecycle = target
        return ResultStatus.SUCCESS

    def quarantine(self, vi: VenueInstrument, reason: str = "") -> ResultStatus:
        key = self._key(vi)
        entry = self._pool.get(key)
        if entry is None:
            return ResultStatus.UNKNOWN
        entry.lifecycle = PoolLifecycle.QUARANTINED
        return ResultStatus.SUCCESS

    def get_active(self) -> list[TradingPoolEntry]:
        return [e for e in self._pool.values() if e.lifecycle == PoolLifecycle.ACTIVE]

    def check_capacity(self, vi: VenueInstrument, order_size: float) -> ResultStatus:
        entry = self._pool.get(self._key(vi))
        if entry is None:
            return ResultStatus.UNKNOWN
        if entry.lifecycle not in (PoolLifecycle.ACTIVE, PoolLifecycle.WARMUP):
            return ResultStatus.ERROR
        if order_size > entry.max_order_size:
            return ResultStatus.ERROR
        if entry.capacity_used_pct >= 100.0:
            return ResultStatus.ERROR
        return ResultStatus.SUCCESS

    def update_capacity(self, vi: VenueInstrument, notional: float) -> None:
        """BD-FIX: 更新容量使用率 — 下单/平仓时调用。"""
        entry = self._pool.get(self._key(vi))
        if entry is not None and entry.max_position_notional > 0:
            entry.capacity_used_pct = min(100.0, (notional / entry.max_position_notional) * 100.0)

    def reset_capacity(self, vi: VenueInstrument) -> None:
        """BD-FIX: 平仓后重置容量使用率。"""
        entry = self._pool.get(self._key(vi))
        if entry is not None:
            entry.capacity_used_pct = 0.0
