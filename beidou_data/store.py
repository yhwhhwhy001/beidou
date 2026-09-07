"""Parquet store for klines and funding history (``.beidou/data/`` by default)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from beidou_data.binance_public import KLINE_COLUMNS

FUNDING_COLUMNS: tuple[str, ...] = ("funding_time", "funding_rate", "mark_price")


def interval_ms(interval: str) -> int:
    """Bar length in milliseconds ("1h" -> 3_600_000); mirrors beidou_alpha.panel without importing it."""
    unit = interval[-1]
    scale = {"m": 60, "h": 3600, "d": 86400}.get(unit)
    if scale is None or not interval[:-1].isdigit():
        raise ValueError(f"unsupported interval {interval!r}")
    return int(interval[:-1]) * scale * 1000


class KlineStore:
    def __init__(self, root: str | Path = ".beidou/data") -> None:
        self.root = Path(root)

    @property
    def directory(self) -> Path:
        return self.root / "klines"

    def path(self, symbol: str, interval: str) -> Path:
        return self.directory / symbol / f"{interval}.parquet"

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

    def gaps(self, symbol: str, interval: str) -> list[tuple[int, int]]:
        """Missing stretches in the stored series as ``(after_open_time, before_open_time)`` pairs (T-D01).

        The archive is monthly and the tail comes from REST, so a merge can leave a hole that
        nothing else would notice: ``append`` dedupes and sorts but never checks continuity, and a
        backtest silently treats a hole as a jump.  Both 2022 archive-wide outages were found this way.

        A reported gap is not always a sync failure.  It can also be an absence upstream: a listing
        boundary (PUMPUSDT's first bar is 2025-07-10 07:00, so the "missing" hours before it never
        existed), a delisted symbol whose history the venue no longer serves (LITUSDT, BNXUSDT), or a
        venue outage.  Re-fetching the range is what tells the two apart, so this stays a pure query.
        """
        frame = self.load(symbol, interval)
        if len(frame) < 2:
            return []
        step = interval_ms(interval)
        opens = frame["open_time"].astype("int64").to_numpy()
        deltas = opens[1:] - opens[:-1]
        return [(int(opens[i]), int(opens[i + 1])) for i, delta in enumerate(deltas) if delta != step]

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
        if not self.directory.exists():
            return []
        return sorted(p.parent.name for p in self.directory.glob(f"*/{interval}.parquet"))


class FundingStore:
    def __init__(self, root: str | Path = ".beidou/data") -> None:
        self.root = Path(root)

    @property
    def directory(self) -> Path:
        return self.root / "funding"

    def path(self, symbol: str) -> Path:
        return self.directory / f"{symbol}.parquet"

    def symbols(self) -> list[str]:
        """Every symbol with funding stored.  Flat here - one file per symbol - unlike klines, which nest.

        Exists so nothing has to re-derive that layout; re-deriving it is what caused D-040.
        """
        if not self.directory.exists():
            return []
        return sorted(p.stem for p in self.directory.glob("*.parquet"))

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


class MetricsStore:
    """Binance futures metrics per symbol, in the ONE stamp `beidou_data.metrics` defines (DL-D2).

    Same shape as ``FundingStore`` deliberately: one parquet per symbol, append-and-dedupe, total rows
    returned.  The column that matters is ``open_time`` - never ``create_time`` and never a REST
    ``timestamp`` - because two names for one instant is how the five-minute look-ahead got in.
    """

    def __init__(self, root: str | Path = ".beidou/data") -> None:
        self._root = Path(root)

    @property
    def directory(self) -> Path:
        return self._root / "metrics"

    def path(self, symbol: str) -> Path:
        return self.directory / f"{symbol}.parquet"

    def symbols(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(p.stem for p in self.directory.glob("*.parquet"))

    def append(self, symbol: str, frame: pd.DataFrame) -> int:
        """Merge new rows (dedupe on open_time, keep last, sorted).  Returns total rows stored."""
        path = self.path(symbol)
        incoming = frame if not frame.empty else pd.DataFrame(columns=["open_time", "symbol"])
        merged = pd.concat([pd.read_parquet(path), incoming], ignore_index=True) if path.exists() else incoming
        merged = merged.drop_duplicates("open_time", keep="last").sort_values("open_time").reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        tmp.replace(path)
        return len(merged)

    def load(self, symbol: str) -> pd.DataFrame:
        path = self.path(symbol)
        if not path.exists():
            return pd.DataFrame(columns=["open_time", "symbol"])
        return pd.read_parquet(path)

    def last_open_time(self, symbol: str) -> int | None:
        path = self.path(symbol)
        if not path.exists():
            return None
        column = pd.read_parquet(path, columns=["open_time"])["open_time"]
        return int(column.max()) if len(column) else None


def bar_freq(bar_index: pd.DatetimeIndex) -> str:
    """The grid a bar index defines, as a pandas offset alias.  Gaps are whole multiples, so the smallest gap is it."""
    if len(bar_index) < 2:
        raise ValueError("a bar index needs at least two bars to define its own grid")
    step = pd.DatetimeIndex(bar_index).to_series().diff().dropna().min()
    if pd.isna(step) or step <= pd.Timedelta(0):
        raise ValueError("bar index is not strictly increasing")
    return f"{int(pd.Timedelta(step).total_seconds())}s"


def funding_per_bar(funding: pd.DataFrame, bar_index: pd.DatetimeIndex) -> pd.Series:
    """Map settled funding rates onto bars: the bar that *contains* the settlement carries the rate.

    This used to require the settlement time to equal a bar's open time.  Binance stamps
    ``fundingTime`` one to forty-seven milliseconds past the hour, so that equality silently dropped
    43.7% of the archive's 1,010,914 settlements onto zero and every backtest with
    ``use_actual_funding`` under-charged funding by roughly half (2026-09-04 audit).  Settlements
    that share a bar are summed, which is what a symbol on a schedule finer than the bar needs.
    """
    series = pd.Series(0.0, index=bar_index, dtype=float)
    if funding.empty:
        return series
    stamps = pd.DatetimeIndex(pd.to_datetime(funding["funding_time"].astype("int64"), unit="ms", utc=True))
    settled = pd.Series(funding["funding_rate"].astype(float).to_numpy(), index=stamps)
    settled = settled.groupby(stamps.floor(bar_freq(bar_index))).sum()
    aligned = settled.reindex(bar_index).fillna(0.0)
    return aligned.astype(float)
