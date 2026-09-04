from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from beidou_alpha.panel import Panel


class FakeClock:
    def __init__(self, now_ms: int) -> None:
        self._now = now_ms
        self.sleeps: list[float] = []

    def now_ms(self) -> int:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += int(seconds * 1000)

    def advance(self, seconds: float) -> None:
        self._now += int(seconds * 1000)


class FakeMarketData:
    """Serves closed bars from a Panel up to a movable cursor (exclusive)."""

    def __init__(
        self,
        panel: Panel,
        cursor: int,
        funding: Mapping[str, float] | None = None,
        funding_history: pd.DataFrame | None = None,
    ) -> None:
        self.panel = panel
        self.cursor = cursor
        self.funding = dict(funding or {})
        self.funding_frame = funding_history  # bars x symbols settled rates (0 off-settlement), like Panel.funding
        self.funding_history_calls: list[tuple[list[str], int]] = []
        self.lag_bars = 0
        self.fail_next = 0

    def bar_open_ms(self, position: int) -> int:
        return int(pd.Timestamp(self.panel.index[position]).timestamp() * 1000)

    async def closed_bars(self, symbols: Sequence[str], interval: str, limit: int) -> dict[str, pd.DataFrame]:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("market data unavailable")
        end = self.cursor - self.lag_bars
        start = max(0, end - limit)
        out: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            if symbol not in self.panel.close.columns:
                continue
            frame = pd.DataFrame(
                {
                    field: getattr(self.panel, field)[symbol].iloc[start:end]
                    for field in ("open", "high", "low", "close", "volume")
                }
            )
            out[symbol] = frame
        return out

    async def funding_rates(self, symbols: Sequence[str]) -> dict[str, float]:
        return {symbol: self.funding.get(symbol, 0.0) for symbol in symbols}

    async def funding_history(self, symbols: Sequence[str], start_ms: int) -> dict[str, pd.Series]:
        """Settled rates since ``start_ms`` (only the non-zero settlements, as the public endpoint returns them)."""
        self.funding_history_calls.append((list(symbols), int(start_ms)))
        out: dict[str, pd.Series] = {}
        for symbol in symbols:
            if self.funding_frame is None or symbol not in self.funding_frame.columns:
                out[symbol] = pd.Series(dtype=float)
                continue
            series = self.funding_frame[symbol]
            stamps = pd.DatetimeIndex(series.index)
            since = stamps >= pd.Timestamp(start_ms, unit="ms", tz="UTC")
            settled = series[since & (series != 0.0)]
            out[symbol] = settled.astype(float)
        return out
