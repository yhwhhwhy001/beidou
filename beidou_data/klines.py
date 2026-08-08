"""闭合 K 线生成、多周期隔离、修订与自动回补。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from beidou_shared.types import Price, Quantity, VenueInstrument


@dataclass(frozen=True, slots=True)
class OHLCV:
    venue_instrument: VenueInstrument
    interval: str
    open_time: datetime
    close_time: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Quantity
    quote_volume: Quantity | None = None
    trade_count: int = 0
    taker_buy_volume: Quantity | None = None
    is_closed: bool = True
    revision_number: int = 0


class KLineGenerator:
    """K 线生成器。多周期隔离，修订与自动回补。"""

    VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}

    def __init__(self, interval: str) -> None:
        if interval not in self.VALID_INTERVALS:
            raise ValueError(f"Invalid interval: {interval}")
        self.interval = interval
        self._klines: dict[str, list[OHLCV]] = {}  # venue_instrument_key -> klines
        self._current: dict[str, OHLCV | None] = {}

    def _interval_delta(self) -> timedelta:
        mapping = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
        unit = mapping[self.interval[-1]]
        value = int(self.interval[:-1])
        return timedelta(**{unit: value})

    def process_tick(
        self,
        venue_instrument: VenueInstrument,
        price: Price,
        quantity: Quantity,
        timestamp: datetime,
        is_taker_buy: bool = False,
    ) -> OHLCV | None:
        key = f"{venue_instrument.venue_id}:{venue_instrument.instrument_id}"
        delta = self._interval_delta()
        interval_start = timestamp.replace(second=0, microsecond=0)
        if delta >= timedelta(hours=1):
            interval_start = interval_start.replace(minute=0)
        if delta >= timedelta(hours=24):
            interval_start = interval_start.replace(hour=0)
        interval_end = interval_start + delta

        current = self._current.get(key)
        completed = None

        if current is None or timestamp >= current.close_time:
            if current is not None:
                # Boundary crossed — close the previous kline
                closed_kline = OHLCV(
                    venue_instrument=current.venue_instrument,
                    interval=current.interval,
                    open_time=current.open_time,
                    close_time=current.close_time,
                    open=current.open,
                    high=current.high,
                    low=current.low,
                    close=current.close,
                    volume=current.volume,
                    quote_volume=current.quote_volume,
                    trade_count=current.trade_count,
                    taker_buy_volume=current.taker_buy_volume,
                    is_closed=True,
                    revision_number=current.revision_number,
                )
                if key not in self._klines:
                    self._klines[key] = []
                self._klines[key].append(closed_kline)
                completed = closed_kline
            current = OHLCV(
                venue_instrument=venue_instrument,
                interval=self.interval,
                open_time=interval_start,
                close_time=interval_end,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Quantity(amount="0"),
                trade_count=0,
            )

        current = OHLCV(
            venue_instrument=current.venue_instrument,
            interval=current.interval,
            open_time=current.open_time,
            close_time=current.close_time,
            open=current.open,
            high=Price(amount=str(max(float(current.high.amount), float(price.amount)))),
            low=Price(amount=str(min(float(current.low.amount), float(price.amount)))),
            close=price,
            volume=Quantity(amount=str(float(current.volume.amount) + float(quantity.amount))),
            quote_volume=current.quote_volume,
            trade_count=current.trade_count + 1,
            taker_buy_volume=Quantity(
                amount=str(
                    float(current.taker_buy_volume.amount if current.taker_buy_volume else "0")
                    + (float(quantity.amount) if is_taker_buy else 0)
                )
            )
            if current.taker_buy_volume or is_taker_buy
            else None,
            is_closed=timestamp >= interval_end,
        )
        self._current[key] = current
        return completed

    def get_klines(self, venue_instrument: VenueInstrument) -> list[OHLCV]:
        key = f"{venue_instrument.venue_id}:{venue_instrument.instrument_id}"
        return self._klines.get(key, [])

    def get_current_bar(self, venue_instrument: VenueInstrument) -> OHLCV | None:
        """返回当前未闭合 K 线（实时 bar），无则 None。"""
        key = f"{venue_instrument.venue_id}:{venue_instrument.instrument_id}"
        return self._current.get(key)

    def revise(self, venue_instrument: VenueInstrument, open_time: datetime, new_ohlcv: OHLCV) -> OHLCV:
        key = f"{venue_instrument.venue_id}:{venue_instrument.instrument_id}"
        klines = self._klines.get(key, [])
        for i, k in enumerate(klines):
            if k.open_time == open_time:
                revised = OHLCV(**{**new_ohlcv.__dict__, "revision_number": k.revision_number + 1})
                klines[i] = revised
                return revised
        raise ValueError(f"No kline found at {open_time}")
