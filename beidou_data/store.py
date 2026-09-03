"""Parquet store for klines and funding history (``.beidou/data/`` by default)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from beidou_data.binance_public import KLINE_COLUMNS

FUNDING_COLUMNS: tuple[str, ...] = ("funding_time", "funding_rate", "mark_price")


class KlineStore:
    def __init__(self, root: str | Path = ".beidou/data") -> None:
        self.root = Path(root)

    def path(self, symbol: str, interval: str) -> Path:
        return self.root / "klines" / symbol / f"{interval}.parquet"

    def exists(self, symbol: str, interval: str) -> bool:
        return self.path(symbol, interval).exists()

    def append(self, symbol: str, interval: str, frame: pd.DataFrame) -> int:
        """Merge new rows (dedupe on open_time, keep last, sorted).  Returns total rows stored."""
        path = self.path(symbol, interval)
        incoming = (
            frame.reindex(columns=list(KLINE_COLUMNS)) if not frame.empty else pd.DataFrame(columns=list(KLINE_COLUMNS))
        )
        if path.exists():
            existing = pd.read_parquet(path)
            merged = pd.concat([existing, incoming], ignore_index=True)
        else:
            merged = incoming
        merged = merged.drop_duplicates("open_time", keep="last").sort_values("open_time").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(path)
        return len(merged)

    def load(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
        path = self.path(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"no klines stored for {symbol}/{interval} under {self.root}")
        frame = pd.read_parquet(path)
        if start_ms is not None:
            frame = frame[frame["open_time"] >= int(start_ms)]
        if end_ms is not None:
            frame = frame[frame["open_time"] < int(end_ms)]
        return frame.reset_index(drop=True)

    def last_open_time(self, symbol: str, interval: str) -> int | None:
        path = self.path(symbol, interval)
        if not path.exists():
            return None
        column = pd.read_parquet(path, columns=["open_time"])["open_time"]
        return int(column.max()) if len(column) else None

    def count(self, symbol: str, interval: str) -> int:
        path = self.path(symbol, interval)
        return len(pd.read_parquet(path, columns=["open_time"])) if path.exists() else 0

    def symbols(self, interval: str) -> list[str]:
        base = self.root / "klines"
        if not base.exists():
            return []
        return sorted(p.parent.name for p in base.glob(f"*/{interval}.parquet"))


class FundingStore:
    def __init__(self, root: str | Path = ".beidou/data") -> None:
        self.root = Path(root)

    def path(self, symbol: str) -> Path:
        return self.root / "funding" / f"{symbol}.parquet"

    def append(self, symbol: str, frame: pd.DataFrame) -> int:
        path = self.path(symbol)
        incoming = frame[list(FUNDING_COLUMNS)] if not frame.empty else pd.DataFrame(columns=list(FUNDING_COLUMNS))
        merged = pd.concat([pd.read_parquet(path), incoming], ignore_index=True) if path.exists() else incoming
        merged = merged.drop_duplicates("funding_time", keep="last").sort_values("funding_time").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(path)
        return len(merged)

    def load(self, symbol: str) -> pd.DataFrame:
        path = self.path(symbol)
        if not path.exists():
            return pd.DataFrame(columns=list(FUNDING_COLUMNS))
        return pd.read_parquet(path)

    def last_time(self, symbol: str) -> int | None:
        path = self.path(symbol)
        if not path.exists():
            return None
        column = pd.read_parquet(path, columns=["funding_time"])["funding_time"]
        return int(column.max()) if len(column) else None


def funding_per_bar(funding: pd.DataFrame, bar_index: pd.DatetimeIndex) -> pd.Series:
    """Map settled funding rates onto bars: the bar whose open_time equals the settlement time carries the rate."""
    series = pd.Series(0.0, index=bar_index, dtype=float)
    if funding.empty:
        return series
    times = pd.to_datetime(funding["funding_time"].astype("int64"), unit="ms", utc=True)
    settled = pd.Series(funding["funding_rate"].astype(float).to_numpy(), index=pd.DatetimeIndex(times))
    settled = settled.groupby(level=0).sum()
    aligned = settled.reindex(bar_index).fillna(0.0)
    return aligned.astype(float)
