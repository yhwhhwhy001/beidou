"""Live closed-bar feed from mainnet public endpoints (implements ``beidou_live.ports.MarketData``)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pandas as pd

from beidou_data.binance_public import DEFAULT_BASE_URL, AsyncPublicClient, drop_unclosed


class PublicMarketData:
    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, *, concurrency: int = 4, client: AsyncPublicClient | None = None
    ) -> None:
        self._client = client or AsyncPublicClient(base_url)
        self._semaphore = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def server_time_ms(self) -> int:
        return await self._client.server_time_ms()

    async def _one(self, symbol: str, interval: str, limit: int, now_ms: int) -> tuple[str, pd.DataFrame]:
        async with self._semaphore:
            frame = await self._client.klines(symbol, interval, limit + 1)
        return symbol, drop_unclosed(frame, now_ms)

    async def closed_bars(self, symbols: Sequence[str], interval: str, limit: int) -> dict[str, pd.DataFrame]:
        now_ms = await self.server_time_ms()
        results = await asyncio.gather(*(self._one(symbol, interval, limit, now_ms) for symbol in symbols))
        return {symbol: frame.tail(limit).reset_index(drop=True) for symbol, frame in results}

    async def funding_rates(self, symbols: Sequence[str]) -> dict[str, float]:
        wanted = set(symbols)
        rows = await self._client.premium_index()
        rates: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if symbol in wanted:
                try:
                    rates[symbol] = float(row.get("lastFundingRate", 0.0))
                except (TypeError, ValueError):
                    continue
        return rates
