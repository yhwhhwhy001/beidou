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


@dataclass(frozen=True, slots=True)
class ClosedBar:
    """BD-T04: 已闭合 K 线合约 — Point-in-Time 不可变事实。

    仅在 bar 确认闭合 (is_closed=True) 后才可用作策略输入。
    每个 bar 具备完整审计键: venue/symbol/interval/open_time/revision/sequence。
    """

    venue_instrument: VenueInstrument
    open_time: datetime
    close_time: datetime
    interval: str = "1m"
    open: Price = field(default_factory=lambda: Price(amount="0"))
    high: Price = field(default_factory=lambda: Price(amount="0"))
    low: Price = field(default_factory=lambda: Price(amount="0"))
    close: Price = field(default_factory=lambda: Price(amount="0"))
    volume: Quantity = field(default_factory=lambda: Quantity(amount="0"))
    quote_volume: Quantity | None = None
    trade_count: int = 0
    taker_buy_volume: Quantity | None = None
    is_closed: bool = True
    schema_version: SchemaVersion = field(default_factory=lambda: SchemaVersion("2.0.0"))
    data_quality_tier: str = "UNKNOWN"
    revision: int = 0
    sequence: int = 0
    available_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "binance_usdm"
    payload_hash: str = ""
    correlation_id: CorrelationId | None = None

    def audit_key(self) -> str:
        """审计键: venue|symbol|interval|open_time|revision"""
        return f"{self.venue_instrument.venue_id.value}|{self.venue_instrument.instrument_id.value}|{self.interval}|{self.open_time.isoformat()}|r{self.revision}"


class BarIntegrity(str, Enum):
    """Bar 完整性状态。"""

    OK = "OK"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    GAP_DETECTED = "GAP_DETECTED"
    STALE = "STALE"
    REVISION = "REVISION"
    NOT_CLOSED = "NOT_CLOSED"


@dataclass(frozen=True, slots=True)
class ClosedBarResult:
    """ClosedBar 处理结果 — 类型化。"""

    bar: ClosedBar | None
    status: BarIntegrity
    detail: str = ""
    correlation_id: CorrelationId | None = None


class ClosedBarNormalizer:
    """ClosedBar 规范化器 — 从原始 MarketEvent/KLine 生成 ClosedBar。

    负责:
    - OHLCV 解析与校验
    - 闭合状态判定 (is_closed)
    - revision 管理 (不可覆盖原始版本)
    - sequence 管理
    - payload_hash 计算
    """

    def __init__(self) -> None:
        import hashlib

        self._seen: dict[str, int] = {}  # audit_key → sequence
        self._revisions: dict[str, int] = {}  # audit_key → max revision
        self._hashlib = hashlib
        self._seq_counter: int = 0

    def normalize(self, raw: dict, venue_instrument: VenueInstrument, interval: str = "1m") -> ClosedBarResult:
        """将原始 KLine 数据规范化为 ClosedBar。"""
        import hashlib

        cid = CorrelationId(f"bar-{venue_instrument.instrument_id.value}-{raw.get('open_time', 'unknown')}")
        try:
            open_time_val = raw.get("open_time")
            close_time_val = raw.get("close_time")
            if open_time_val is None or close_time_val is None:
                return ClosedBarResult(None, BarIntegrity.NOT_CLOSED, "Missing open_time or close_time", cid)

            if isinstance(open_time_val, (int, float)):
                open_time = datetime.fromtimestamp(open_time_val / 1000, tz=timezone.utc)
            else:
                open_time = datetime.fromisoformat(str(open_time_val))

            if isinstance(close_time_val, (int, float)):
                close_time = datetime.fromtimestamp(close_time_val / 1000, tz=timezone.utc)
            else:
                close_time = datetime.fromisoformat(str(close_time_val))

            is_closed = raw.get("is_closed", raw.get("x", True))
            if isinstance(is_closed, str):
                is_closed = is_closed.lower() in ("true", "1", "yes")

            self._seq_counter += 1
            bar = ClosedBar(
                venue_instrument=venue_instrument,
                open_time=open_time,
                close_time=close_time,
                interval=interval,
                open=Price(amount=str(raw.get("open", 0))),
                high=Price(amount=str(raw.get("high", 0))),
                low=Price(amount=str(raw.get("low", 0))),
                close=Price(amount=str(raw.get("close", 0))),
                volume=Quantity(amount=str(raw.get("volume", 0))),
                quote_volume=Quantity(amount=str(raw.get("quote_volume", 0))) if raw.get("quote_volume") else None,
                trade_count=int(raw.get("trade_count", raw.get("n", 0))),
                taker_buy_volume=Quantity(amount=str(raw.get("taker_buy_volume", 0)))
                if raw.get("taker_buy_volume")
                else None,
                is_closed=bool(is_closed),
                revision=0,
                sequence=self._seq_counter,
                available_at=datetime.now(timezone.utc),
                source="binance_usdm",
                correlation_id=cid,
            )

            # Compute payload hash
            payload_str = f"{bar.audit_key()}|{bar.open.amount}|{bar.high.amount}|{bar.low.amount}|{bar.close.amount}|{bar.volume.amount}|{bar.sequence}"
            bar = ClosedBar(
                venue_instrument=bar.venue_instrument,
                open_time=bar.open_time,
                close_time=bar.close_time,
                interval=bar.interval,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                quote_volume=bar.quote_volume,
                trade_count=bar.trade_count,
                taker_buy_volume=bar.taker_buy_volume,
                is_closed=bar.is_closed,
                revision=bar.revision,
                sequence=bar.sequence,
                available_at=bar.available_at,
                source=bar.source,
                payload_hash=hashlib.sha256(payload_str.encode()).hexdigest()[:16],
                correlation_id=bar.correlation_id,
            )

            # Check integrity
            audit_key = bar.audit_key()
            if audit_key in self._seen:
                return ClosedBarResult(bar, BarIntegrity.DUPLICATE, f"Duplicate bar: {audit_key}", cid)

            self._seen[audit_key] = bar.sequence

            if not bar.is_closed:
                return ClosedBarResult(bar, BarIntegrity.NOT_CLOSED, "Bar is not yet closed", cid)

            return ClosedBarResult(bar, BarIntegrity.OK, f"Normalized: {audit_key}", cid)

        except Exception as e:
            return ClosedBarResult(None, BarIntegrity.NOT_CLOSED, f"Normalization failed: {e}", cid)

    def revise(self, bar: ClosedBar, updated_raw: dict) -> ClosedBarResult:
        """修订 bar — 不可覆盖原始版本，revision+1。"""
        cid = CorrelationId(f"revise-{bar.audit_key()}")
        result = self.normalize(updated_raw, bar.venue_instrument, bar.interval)
        if result.bar is None:
            return result
        revised = ClosedBar(
            venue_instrument=result.bar.venue_instrument,
            open_time=result.bar.open_time,
            close_time=result.bar.close_time,
            interval=result.bar.interval,
            open=result.bar.open,
            high=result.bar.high,
            low=result.bar.low,
            close=result.bar.close,
            volume=result.bar.volume,
            quote_volume=result.bar.quote_volume,
            trade_count=result.bar.trade_count,
            taker_buy_volume=result.bar.taker_buy_volume,
            is_closed=result.bar.is_closed,
            revision=bar.revision + 1,
            sequence=result.bar.sequence,
            available_at=result.bar.available_at,
            source=result.bar.source,
            payload_hash=result.bar.payload_hash,
            correlation_id=cid,
        )
        return ClosedBarResult(revised, BarIntegrity.REVISION, f"Revised r{bar.revision}→r{revised.revision}", cid)


class BarSequenceValidator:
    """Bar 序列验证器 — 检测乱序、缺口、陈旧。"""

    def __init__(self, max_gap_seconds: int = 300, max_stale_seconds: int = 600) -> None:
        self._last_open_times: dict[str, datetime] = {}  # symbol → last open_time
        self._expected_intervals: dict[str, float] = {}  # symbol → expected interval_seconds
        self._max_gap = max_gap_seconds
        self._max_stale = max_stale_seconds

    def validate(self, bar: ClosedBar, now: datetime | None = None) -> BarIntegrity:
        """验证 bar 序列完整性。

        Returns:
            BarIntegrity.OK / OUT_OF_ORDER / GAP_DETECTED / STALE
        """
        symbol = bar.venue_instrument.instrument_id.value
        last = self._last_open_times.get(symbol)

        # Check staleness
        now = now or datetime.now(timezone.utc)
        if (now - bar.available_at).total_seconds() > self._max_stale:
            self._update(bar)
            return BarIntegrity.STALE

        # Check ordering
        if last is not None:
            if bar.open_time < last:
                self._update(bar)
                return BarIntegrity.OUT_OF_ORDER
            # Check for gaps
            expected = self._expected_intervals.get(symbol, 60.0)
            gap = (bar.open_time - last).total_seconds()
            if gap > expected * 1.5:
                self._update(bar)
                return BarIntegrity.GAP_DETECTED

        self._update(bar)
        return BarIntegrity.OK

    def _update(self, bar: ClosedBar) -> None:
        symbol = bar.venue_instrument.instrument_id.value
        self._last_open_times[symbol] = bar.open_time
        if bar.interval == "1m":
            self._expected_intervals[symbol] = 60.0
        elif bar.interval == "5m":
            self._expected_intervals[symbol] = 300.0
        elif bar.interval == "1h":
            self._expected_intervals[symbol] = 3600.0


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
