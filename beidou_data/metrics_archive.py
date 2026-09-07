"""Daily metrics archive downloader, with resume and checksum verification (DL-D2 / DL-Q6).

Deliberately shaped like ``archive.py``: verify the zip against its published checksum, treat 404 as
"not published yet" rather than as an error, and let the STORE be the watermark so a resume needs no
side file to go stale.  That module learned these three things once; a second downloader should not
have to learn them again.

The publication schedule, measured 2026-09-07: a day's file appears at about T+1 06:45-07:00 UTC
(three consecutive days landed at 06:58, 06:52 and 06:43).  So "today" and usually "yesterday morning"
are legitimately absent, and a sync that treated absence as failure would be red every morning.

Every frame leaves here in the canonical shape of ``beidou_data.metrics`` - ``open_time``, never
``create_time`` - because the whole point of that module is that the conversion happens exactly once.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd

from beidou_data.archive import parse_checksum, verify_zip
from beidou_data.metrics import parse_archive_csv
from beidou_data.store import MetricsStore

BASE_URL = "https://data.binance.vision"
METRICS_PERIOD_MS = 300_000  # the daily archive is published at 5m granularity


def metrics_path(symbol: str, day: str) -> str:
    return f"/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"


def days_between(start: str, end_exclusive: str) -> list[str]:
    first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    last = datetime.strptime(end_exclusive, "%Y-%m-%d").replace(tzinfo=UTC)
    out: list[str] = []
    while first < last:
        out.append(first.strftime("%Y-%m-%d"))
        first += timedelta(days=1)
    return out


class MetricsArchiveClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MetricsArchiveClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_day(self, symbol: str, day: str) -> pd.DataFrame | None:
        """One verified day in the canonical shape, or ``None`` when the archive has no file (404)."""
        path = metrics_path(symbol, day)
        response = self._client.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        checksum = self._client.get(path + ".CHECKSUM")
        checksum.raise_for_status()
        verify_zip(response.content, parse_checksum(checksum.text))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            text = archive.read(archive.namelist()[0]).decode("utf-8")
        return parse_archive_csv(text)


def sync_metrics(
    client: MetricsArchiveClient,
    store: MetricsStore,
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
) -> dict[str, int]:
    """Fetch every published day in ``[start, end)`` that the store does not already hold.

    The watermark is the store's own ``last_open_time``, not a cursor file: a cursor can disagree with
    the data it claims to describe, and then a resume silently skips a gap.  Days already covered are
    not requested at all - which is what makes a nightly re-run cheap rather than a full re-download.
    """
    stored: dict[str, int] = {}
    for symbol in symbols:
        watermark = store.last_open_time(symbol)
        for day in days_between(start, end):
            day_end_ms = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000) + 86_400_000
            if watermark is not None and day_end_ms <= watermark + METRICS_PERIOD_MS:
                continue
            frame = client.fetch_day(symbol, day)
            if frame is None or frame.empty:
                continue
            stored[symbol] = store.append(symbol, frame)
    return stored
