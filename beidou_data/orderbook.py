"""OrderBook Snapshot + Diff Sequence — BD-04 item 3。

订单簿采用 snapshot + diff sequence 模式。
检测 duplicate、gap、out-of-order 并自动回补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass
class OrderBookSnapshot:
    """订单簿快照 — 某时刻的完整深度。"""

    instrument_id: str
    venue_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    sequence: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    checksum: str = ""

    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else float("inf")

    def spread_bps(self) -> float:
        best_ask = self.best_ask()
        best_bid = self.best_bid()
        if best_ask > 0:
            return (best_ask - best_bid) / best_ask * 10000
        return 0.0

    def mid_price(self) -> float:
        return (self.best_bid() + self.best_ask()) / 2


@dataclass
class OrderBookDiff:
    """订单簿增量更新。"""

    instrument_id: str
    sequence: int
    prev_sequence: int
    bid_updates: list[OrderBookLevel]  # price=0 means delete
    ask_updates: list[OrderBookLevel]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OrderBookManager:
    """订单簿管理器。

    - 接收 snapshot + diff sequence
    - 检测 duplicate sequence（跳过）
    - 检测 gap（触发 REST 回补）
    - 检测 out-of-order（丢弃 + 回补）
    """

    def __init__(self, max_depth: int = 50):
        self._snapshot: OrderBookSnapshot | None = None
        self._last_sequence: int = 0
        self._max_depth = max_depth

    def apply_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        """应用完整快照（重置序列）。"""
        self._snapshot = snapshot
        self._last_sequence = snapshot.sequence

    def apply_diff(self, diff: OrderBookDiff) -> bool:
        """应用增量更新。返回 False 表示需要回补。"""
        if not self._snapshot:
            return False  # 无快照基线

        # Duplicate detection
        if diff.sequence <= self._last_sequence:
            return True  # 跳过重复

        # Gap detection: every diff must be provably contiguous.  Applying a
        # later event after a missing one would manufacture an order-book state
        # that cannot be reconstructed from the durable event stream.
        expected = self._last_sequence + 1
        if diff.prev_sequence != self._last_sequence or diff.sequence != expected:
            return False  # sequence/prev_sequence mismatch -> request snapshot

        # Apply bid updates
        bids = list(self._snapshot.bids)
        for level in diff.bid_updates:
            # BD-FIX: price=0 是删除标记，但当前协议未携带要删除的 price。
            # 当 quantity=0 时删除对应价格档位（交易所标准做法）；
            # 当两者均为 0 时无法确定删除目标，跳过（安全优先）。
            if level.price == 0 and level.quantity == 0:
                # 无法识别要删除的价格档位，跳过
                continue
            if level.quantity == 0:
                # quantity=0 → 删除该价格档位
                bids = [b for b in bids if abs(b.price - level.price) > 1e-15]
            elif level.price > 0:
                # 正常更新：先移除旧价位（如果存在），再插入新价位
                bids = [b for b in bids if abs(b.price - level.price) > 1e-15]
                bids.append(level)
        bids.sort(key=lambda x: x.price, reverse=True)
        bids = bids[: self._max_depth]

        # Apply ask updates
        asks = list(self._snapshot.asks)
        for level in diff.ask_updates:
            # BD-FIX: same fix as bids — use quantity=0 as deletion signal
            if level.price == 0 and level.quantity == 0:
                continue
            if level.quantity == 0:
                asks = [a for a in asks if abs(a.price - level.price) > 1e-15]
            elif level.price > 0:
                asks = [a for a in asks if abs(a.price - level.price) > 1e-15]
                asks.append(level)
        asks.sort(key=lambda x: x.price)
        asks = asks[: self._max_depth]

        self._snapshot = OrderBookSnapshot(
            instrument_id=self._snapshot.instrument_id,
            venue_id=self._snapshot.venue_id,
            bids=bids,
            asks=asks,
            sequence=diff.sequence,
        )
        self._last_sequence = diff.sequence
        return True

    def needs_resync(self) -> bool:
        """是否需要回补。"""
        return self._snapshot is None

    def get_snapshot(self) -> OrderBookSnapshot | None:
        return self._snapshot


@dataclass
class WarmingWindow:
    """BD-04 item 8: 数据恢复后 warming window。

    必须经过 warming window 和连续性验证才能恢复交易资格。
    """

    instrument_id: str
    min_warmup_seconds: float = 60.0  # 最少预热时间
    min_events: int = 100  # 最少事件数
    started_at: float | None = None
    events_received: int = 0
    sequence_validated: bool = False
    is_warmed: bool = False

    def start(self, start_time: float) -> None:
        self.started_at = start_time
        self.events_received = 0
        self.sequence_validated = False
        self.is_warmed = False

    def record_event(self, current_time: float) -> None:
        self.events_received += 1
        if self.started_at and not self.is_warmed:
            elapsed = current_time - self.started_at
            if elapsed >= self.min_warmup_seconds and self.events_received >= self.min_events:
                self.is_warmed = True

    def can_trade(self) -> bool:
        return self.is_warmed and self.sequence_validated
