"""Wide market panel: one DataFrame per field, indexed by UTC bar open time, columns = symbols."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

BAR_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
)
REQUIRED_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}


def interval_seconds(interval: str) -> int:
    try:
        return _INTERVAL_SECONDS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported interval {interval!r}") from exc


def bars_per_year(interval: str) -> float:
    return 365.0 * 86400.0 / interval_seconds(interval)


def to_utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy indexed by tz-aware UTC ``open_time`` (accepts ms ints or datetimes)."""
    if isinstance(frame.index, pd.DatetimeIndex) and "open_time" not in frame.columns:
        index = pd.DatetimeIndex(frame.index)
        out = frame.copy()
    else:
        raw = frame["open_time"]
        if pd.api.types.is_integer_dtype(raw):
            index = pd.DatetimeIndex(pd.to_datetime(raw.to_numpy(), unit="ms", utc=True))
        else:
            index = pd.DatetimeIndex(pd.to_datetime(raw.to_numpy(), utc=True))
        out = frame.drop(columns=["open_time"]).copy()
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    out.index = pd.DatetimeIndex(index, name="open_time")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


@dataclass(frozen=True)
class Panel:
    """Aligned wide frames.  Missing fields are ``None``; ``funding`` is the per-bar funding rate (0 off-settlement)."""

    interval: str
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    quote_volume: pd.DataFrame | None = None
    trades: pd.DataFrame | None = None
    taker_buy_base: pd.DataFrame | None = None
    taker_buy_quote: pd.DataFrame | None = None
    funding: pd.DataFrame | None = None

    @classmethod
    def from_frames(
        cls,
        frames: Mapping[str, pd.DataFrame],
        interval: str,
        funding: Mapping[str, pd.Series] | pd.DataFrame | None = None,
    ) -> Panel:
        if not frames:
            raise ValueError("at least one symbol frame is required")
        normalized = {symbol: to_utc_index(frame) for symbol, frame in frames.items()}
        for symbol, frame in normalized.items():
            missing = [field for field in REQUIRED_FIELDS if field not in frame.columns]
            if missing:
                raise ValueError(f"{symbol} is missing required fields {missing}")
        wide: dict[str, pd.DataFrame | None] = {}
        for field in BAR_FIELDS:
            if all(field in frame.columns for frame in normalized.values()):
                wide[field] = pd.concat(
                    {symbol: frame[field].astype(float) for symbol, frame in normalized.items()}, axis=1
                )
            else:
                wide[field] = None
        index = wide["close"].index if wide["close"] is not None else None
        funding_frame: pd.DataFrame | None = None
        if funding is not None and index is not None:
            funding_frame = funding if isinstance(funding, pd.DataFrame) else pd.DataFrame(dict(funding))
            funding_frame = funding_frame.reindex(index).reindex(columns=list(normalized)).fillna(0.0).astype(float)
        assert wide["open"] is not None and wide["high"] is not None and wide["low"] is not None
        assert wide["close"] is not None and wide["volume"] is not None
        return cls(
            interval=interval,
            open=wide["open"],
            high=wide["high"],
            low=wide["low"],
            close=wide["close"],
            volume=wide["volume"],
            quote_volume=wide["quote_volume"],
            trades=wide["trades"],
            taker_buy_base=wide["taker_buy_base"],
            taker_buy_quote=wide["taker_buy_quote"],
            funding=funding_frame,
        )

    @property
    def symbols(self) -> list[str]:
        return [str(column) for column in self.close.columns]

    @property
    def index(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)

    @property
    def bars_per_year(self) -> float:
        return bars_per_year(self.interval)

    def _map(self, function: Callable[[pd.DataFrame], pd.DataFrame]) -> Panel:
        def maybe(frame: pd.DataFrame | None) -> pd.DataFrame | None:
            return None if frame is None else function(frame)

        return Panel(
            interval=self.interval,
            open=function(self.open),
            high=function(self.high),
            low=function(self.low),
            close=function(self.close),
            volume=function(self.volume),
            quote_volume=maybe(self.quote_volume),
            trades=maybe(self.trades),
            taker_buy_base=maybe(self.taker_buy_base),
            taker_buy_quote=maybe(self.taker_buy_quote),
            funding=maybe(self.funding),
        )

    def slice(self, start: pd.Timestamp | str | None = None, end: pd.Timestamp | str | None = None) -> Panel:
        """Rows with ``start <= open_time < end``."""
        start_ts = None if start is None else pd.Timestamp(start, tz="UTC")
        end_ts = None if end is None else pd.Timestamp(end, tz="UTC")

        def cut(frame: pd.DataFrame) -> pd.DataFrame:
            mask = pd.Series(True, index=frame.index)
            if start_ts is not None:
                mask &= frame.index >= start_ts
            if end_ts is not None:
                mask &= frame.index < end_ts
            return frame[mask.to_numpy()]

        return self._map(cut)

    def tail(self, bars: int) -> Panel:
        return self._map(lambda frame: frame.iloc[-bars:])

    def select(self, symbols: Iterable[str]) -> Panel:
        chosen = [symbol for symbol in symbols if symbol in self.close.columns]
        return self._map(lambda frame: frame[chosen])

    def latest_bar(self) -> pd.Timestamp:
        return pd.Timestamp(self.close.index[-1])
