"""Daily liquidation archive downloader (#19), shaped like ``metrics_archive.py`` with one inversion.

The shape it reuses: verify each zip against its published checksum, retry a 5xx, never retry a 404,
and let the STORE be the watermark so a resume needs no side file that could go stale.

The inversion, which is the whole reason this is a separate downloader rather than a parameter on the
other one: **a 404 must not create a file, and a 200 carrying no rows must.**  For metrics, absence
and emptiness are the same event - a bucket exists for every five minutes, so a day with no rows is a
day that failed.  For liquidations they are opposite facts: a day the venue never published is
unknown, and a day it published with nothing in it is a genuine zero.  `LiquidationStore` records the
difference by whether the file exists, so this module's only real job is to not blur it.

What is downloadable, measured 2026-09-09 against the S3 listing: nothing at all under
``futures/um/`` - both the monthly path this column's plan named and the daily one list ZERO keys.
The only ``liquidationSnapshot`` published is coin-margined daily, 118 symbols, 2023-06-25 through
2024-10-14.  Pointed at ``um`` this module therefore stores nothing and every bar downstream reads
NaN, which is the correct report and not a failure to handle.

Single-threaded, deliberately.  `sync_metrics` grew a thread pool because that backfill was a
six-hour job with 0.58s of waiting per 0.06s of work; here the default market has nothing to fetch,
so tuning throughput against a source that does not exist would be guessing at a shape nobody has
seen.
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Callable, Sequence

import httpx
import pandas as pd

from beidou_data.archive import parse_checksum, verify_zip
from beidou_data.liquidations import LiquidationStore, parse_archive_csv
from beidou_data.metrics_archive import days_between

BASE_URL = "https://data.binance.vision"
_RETRIES = 4  # 1s, 2s, 4s; the CDN throttles by concurrency, so backing off works


def liquidation_path(symbol: str, day: str, *, market: str = "um") -> str:
    return f"/data/futures/{market}/daily/liquidationSnapshot/{symbol}/{symbol}-liquidationSnapshot-{day}.zip"


class LiquidationArchiveClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        market: str = "um",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)
        self._market = market

    @property
    def market(self) -> str:
        return self._market

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LiquidationArchiveClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str) -> httpx.Response:
        """One request, retried on a 5xx.  A 404 is never retried - it is this archive's normal answer."""
        delay = 1.0
        for attempt in range(_RETRIES):
            response = self._client.get(path)
            if response.status_code < 500 or attempt == _RETRIES - 1:
                return response
            time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def fetch_day(self, symbol: str, day: str) -> pd.DataFrame | None:
        """One verified day of events, or ``None`` when the archive has no file for it (404).

        ``None`` and an empty frame are the two different answers this whole column turns on, so they
        are two different return values rather than one falsy one.
        """
        path = liquidation_path(symbol, day, market=self._market)
        response = self._get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        checksum = self._get(path + ".CHECKSUM")
        checksum.raise_for_status()
        verify_zip(response.content, parse_checksum(checksum.text))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if not names:
                raise ValueError(f"liquidation archive {path} contains no csv")
            text = archive.read(names[0]).decode("utf-8")
        return parse_archive_csv(text, symbol)


def sync_liquidations(
    client: LiquidationArchiveClient,
    store: LiquidationStore,
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    """Fetch every published day in ``[start, end)`` the store does not already hold.

    Returns days STORED per symbol, not rows: rows are the wrong unit here because zero of them is a
    perfectly good day, and a caller checking a row count would read a quiet week as a failed sync.

    One symbol's failure is recorded and skipped rather than raised - including the anomaly
    `collapse_archive_duplication` raises when a file stops being doubled the way it was measured to
    be.  The store is the watermark, so a re-run resumes exactly where this stopped, while ending the
    whole run would throw away every other symbol's work.
    """
    days = days_between(start, end)
    stored: dict[str, int] = {}
    total = len(symbols)
    for done, symbol in enumerate(symbols, start=1):
        label = symbol
        try:
            written = 0
            for day in days:
                if store.has_day(symbol, day):
                    continue
                frame = client.fetch_day(symbol, day)
                if frame is None:
                    continue  # not published: recording it as an empty day would invent coverage
                store.write_day(symbol, day, frame)
                written += 1
            if written:
                stored[symbol] = written
        except Exception as exc:
            label = f"{symbol} FAILED ({type(exc).__name__}: {exc})"
        if progress is not None:
            progress(label, done, total)
    return stored
