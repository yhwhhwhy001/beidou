"""Official Binance public archive (data.binance.vision): monthly USDⓈ-M kline zips with SHA-256 CHECKSUM files."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

import httpx
import pandas as pd

from beidou_data.binance_public import klines_to_frame

ARCHIVE_BASE_URL = "https://data.binance.vision"


class ChecksumMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class Month:
    year: int
    month: int

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def next(self) -> Month:
        return Month(self.year + (self.month // 12), self.month % 12 + 1)

    def start_ms(self) -> int:
        return int(pd.Timestamp(year=self.year, month=self.month, day=1, tz="UTC").timestamp() * 1000)

    def end_ms(self) -> int:
        following = self.next()
        return following.start_ms()

    @classmethod
    def parse(cls, text: str) -> Month:
        year, month = text.split("-")[:2]
        return cls(int(year), int(month))

    @classmethod
    def of_ms(cls, timestamp_ms: int) -> Month:
        stamp = pd.Timestamp(timestamp_ms, unit="ms", tz="UTC")
        return cls(stamp.year, stamp.month)


def month_range(start: Month, end_exclusive: Month) -> list[Month]:
    months: list[Month] = []
    current = start
    while (current.year, current.month) < (end_exclusive.year, end_exclusive.month):
        months.append(current)
        current = current.next()
    return months


def archive_path(symbol: str, interval: str, month: Month) -> str:
    return f"/data/futures/um/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def parse_checksum(text: str) -> str:
    """CHECKSUM files contain ``<sha256>  <filename>``."""
    token = text.strip().split()[0] if text.strip() else ""
    if len(token) != 64:
        raise ChecksumMismatch(f"malformed checksum file: {text[:80]!r}")
    return token.lower()


def verify_zip(payload: bytes, expected_sha256: str) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256.lower():
        raise ChecksumMismatch(f"sha256 {digest} != expected {expected_sha256}")


def zip_to_frame(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not names:
            raise ValueError("archive contains no csv")
        with archive.open(names[0]) as handle:
            text = handle.read().decode("utf-8")
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        if not parts[0].strip().isdigit():
            continue  # header row (newer archives carry one)
        rows.append(parts)
    return klines_to_frame([list(row) for row in rows])


class ArchiveClient:
    def __init__(self, base_url: str = ARCHIVE_BASE_URL, timeout: float = 60.0) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers={"User-Agent": "beidou-v5"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ArchiveClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_month(self, symbol: str, interval: str, month: Month) -> pd.DataFrame | None:
        """Return the verified month frame, or ``None`` when the archive has no file for that month (404)."""
        path = archive_path(symbol, interval, month)
        response = self._client.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        checksum = self._client.get(path + ".CHECKSUM")
        checksum.raise_for_status()
        verify_zip(response.content, parse_checksum(checksum.text))
        return zip_to_frame(response.content)
