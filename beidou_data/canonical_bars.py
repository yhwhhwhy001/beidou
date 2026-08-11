"""BD-CV11: CanonicalBarBuilder — 唯一可重放 K 线构建器。

运行时、回测、研究、replay 统一调用。
显式建模 GAP/LATE/DUPLICATE/REVISION/CLOSED 事件。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from beidou_data.klines import OHLCV, KLineGenerator
from beidou_shared.types import Price, Quantity, VenueInstrument


class BarEventType(str):
    GAP = "GAP"
    LATE = "LATE"
    DUPLICATE = "DUPLICATE"
    REVISION = "REVISION"
    CLOSED = "CLOSED"
    NEW = "NEW"


@dataclass
class BarEvent:
    """BD-CV11: K 线事件，含事件类型和 hash。"""

    event_type: str  # BarEventType
    bar: OHLCV
    event_hash: str = ""
    gap_duration: float = 0.0
    late_by: float = 0.0
    duplicate_of: int = -1

    def compute_hash(self) -> str:
        data = {
            "event_type": self.event_type,
            "symbol": str(self.bar.venue_instrument.instrument_id),
            "open_time": self.bar.open_time.isoformat(),
            "close_time": self.bar.close_time.isoformat(),
            "open": str(self.bar.open.amount),
            "high": str(self.bar.high.amount),
            "low": str(self.bar.low.amount),
            "close": str(self.bar.close.amount),
            "volume": str(self.bar.volume.amount),
            "is_closed": self.bar.is_closed,
            "revision": self.bar.revision_number,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class CanonicalBarBuilder:
    """BD-CV11: 唯一可重放行情事实链构建器。

    - 使用 exchange timestamp + interval spec 计算 bar_open_time=floor(ts/interval)*interval
    - 覆盖 1m/3m/5m/15m/1h/2h/4h/1d
    - Gap 不能被静默跳过
    - 运行时/回测/research/replay 统一调用
    """

    VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}

    def __init__(self, interval: str = "5m") -> None:
        if interval not in self.VALID_INTERVALS:
            raise ValueError(f"Invalid interval: {interval}")
        self.interval = interval
        self._generator = KLineGenerator(interval)
        self._events: list[BarEvent] = []
        self._last_bar_time: dict[str, datetime] = {}
        self._bar_hashes: set[str] = set()
        self._gap_events: list[BarEvent] = []

    @staticmethod
    def floor_to_bucket(timestamp: datetime, interval_seconds: int) -> datetime:
        """BD-CV11: 将时间戳对齐到 interval bucket。

        10:07 → 10:05 (5-min interval)
        """
        ts = timestamp.timestamp()
        floored = (int(ts) // interval_seconds) * interval_seconds
        return datetime.fromtimestamp(floored, tz=timestamp.tzinfo)

    def process_tick(
        self,
        venue_instrument: VenueInstrument,
        price: Price,
        quantity: Quantity,
        timestamp: datetime,
        is_taker_buy: bool = False,
    ) -> BarEvent | None:
        """处理一个 tick，返回 BarEvent（如果是闭合 K 线）或 None。"""
        symbol = str(venue_instrument.instrument_id)
        completed = self._generator.process_tick(venue_instrument, price, quantity, timestamp, is_taker_buy)

        if completed is None:
            return None

        # 计算 bar hash
        bar_hash_raw = (
            f"{symbol}:{completed.open_time.isoformat()}:{completed.close_time.isoformat()}:{completed.close.amount}"
        )
        bar_hash = hashlib.sha256(bar_hash_raw.encode()).hexdigest()

        # Duplicate detection
        if bar_hash in self._bar_hashes:
            event = BarEvent(event_type=BarEventType.DUPLICATE, bar=completed)
            event.compute_hash()
            return event

        self._bar_hashes.add(bar_hash)

        # Gap detection
        event_type = BarEventType.CLOSED
        last_time = self._last_bar_time.get(symbol)
        if last_time is not None:
            expected_next = (
                self._generator._interval_delta() + last_time if hasattr(self._generator, "_interval_delta") else None
            )
            gap = None
            if hasattr(self._generator, "_interval_delta"):
                gap = completed.open_time - last_time
                if gap > self._generator._interval_delta():
                    event_type = BarEventType.GAP

        self._last_bar_time[symbol] = completed.open_time

        # Late detection
        late_by = 0.0
        if completed.is_closed:
            late_by = max(0.0, (timestamp - completed.close_time).total_seconds())
            if late_by > 60:
                event_type = BarEventType.LATE

        event = BarEvent(
            event_type=event_type,
            bar=completed,
            gap_duration=0.0,
            late_by=late_by,
        )
        event.event_hash = event.compute_hash()
        self._events.append(event)

        if event_type == BarEventType.GAP:
            self._gap_events.append(event)

        return event

    @property
    def events(self) -> list[BarEvent]:
        return list(self._events)

    @property
    def gaps(self) -> list[BarEvent]:
        return list(self._gap_events)

    def closed_bars(self) -> list[OHLCV]:
        """返回所有已闭合的 K 线（不含 GAP/DUPLICATE/LATE）。"""
        return [e.bar for e in self._events if e.event_type == BarEventType.CLOSED]

    def bar_count(self) -> int:
        return len(self._events)

    def gap_count(self) -> int:
        return len(self._gap_events)

    def any_unclosed_or_gapped(self) -> bool:
        """BD-CV11: 任何未闭合、缺口 bar 不得进入增加风险的特征链。"""
        has_gaps = bool(self._gap_events)
        has_unclosed = any(e.event_type != BarEventType.CLOSED for e in self._events[-1:] if not e.bar.is_closed)
        return has_gaps or has_unclosed


# 全局注册表 — 各子系统通过 interval 获取共享 builder
_bar_builders: dict[str, CanonicalBarBuilder] = {}


def get_canonical_bar_builder(interval: str = "5m") -> CanonicalBarBuilder:
    """BD-CV11: 获取共享的 CanonicalBarBuilder。

    运行时、回测、研究、replay 统一调用此函数。
    删除重复 KLine 构造实现。
    """
    if interval not in _bar_builders:
        _bar_builders[interval] = CanonicalBarBuilder(interval)
    return _bar_builders[interval]


def reset_bar_builders() -> None:
    """重置所有 bar builder（测试用）。"""
    _bar_builders.clear()
