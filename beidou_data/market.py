"""实时行情数据与 Raw Layer。标准事件、ClickHouse 分析层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import (
    CorrelationId,
    Price,
    Quantity,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)


class MarketEventType(str, Enum):
    TRADE = "TRADE"
    BOOK_UPDATE = "BOOK_UPDATE"
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    KLINE = "KLINE"
    MARK_PRICE = "MARK_PRICE"
    FUNDING_RATE = "FUNDING_RATE"
    OPEN_INTEREST = "OPEN_INTEREST"
    LIQUIDATION = "LIQUIDATION"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_type: MarketEventType
    venue_instrument: VenueInstrument
    event_time: datetime
    schema_version: SchemaVersion
    correlation_id: CorrelationId | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw_bytes: bytes | None = None
    sequence_number: int | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_replay: bool = False


@dataclass(frozen=True, slots=True)
class TradeTick:
    venue_instrument: VenueInstrument
    trade_id: str
    price: Price
    quantity: Quantity
    side: str
    timestamp: datetime
    is_buyer_maker: bool = False
    correlation_id: CorrelationId | None = None


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Price
    quantity: Quantity
    order_count: int | None = None


@dataclass
class OrderBookSnapshot:
    venue_instrument: VenueInstrument
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    sequence_number: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: CorrelationId | None = None

    def best_bid(self) -> Price | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Price | None:
        return self.asks[0].price if self.asks else None

    def mid_price(self) -> Price | None:
        if not self.bids or not self.asks:
            return None
        mid = (float(self.bids[0].price.amount) + float(self.asks[0].price.amount)) / 2
        return Price(amount=str(mid))

    def spread(self) -> Price | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return Price(amount=str(float(ba.amount) - float(bb.amount)))

    def spread_bps(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None or float(ba.amount) == 0:
            return None
        return (float(ba.amount) - float(bb.amount)) / float(ba.amount) * 10000

    def total_bid_volume(self) -> float:
        return sum(float(b.quantity.amount) for b in self.bids)

    def total_ask_volume(self) -> float:
        return sum(float(a.quantity.amount) for a in self.asks)

    def imbalance(self) -> float:
        bid_vol = self.total_bid_volume()
        ask_vol = self.total_ask_volume()
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def apply_update(self, side: str, price_level: Price, quantity: Quantity) -> None:
        levels = self.bids if side.upper() == "BUY" else self.asks
        target = float(price_level.amount)
        new_qty = float(quantity.amount)
        for i, level in enumerate(levels):
            if float(level.price.amount) == target:
                if new_qty == 0:
                    levels.pop(i)
                else:
                    levels[i] = BookLevel(price=price_level, quantity=Quantity(amount=str(new_qty)))
                return
        if new_qty > 0:
            levels.append(BookLevel(price=price_level, quantity=Quantity(amount=str(new_qty))))
            if side.upper() == "BUY":
                self.bids.sort(key=lambda l: float(l.price.amount), reverse=True)
            else:
                self.asks.sort(key=lambda l: float(l.price.amount))


class RawLayer:
    """Raw Layer — 不可变原始行情存储。事件可追溯到原始字节。"""

    def __init__(self) -> None:
        self._events: list[MarketEvent] = []

    def ingest(self, event: MarketEvent) -> None:
        self._events.append(event)

    def replay_range(self, start: datetime, end: datetime, venue_id: VenueId | None = None) -> list[MarketEvent]:
        return [
            e
            for e in self._events
            if start <= e.event_time <= end and (venue_id is None or e.venue_instrument.venue_id == venue_id)
        ]

    def event_count(self) -> int:
        return len(self._events)
