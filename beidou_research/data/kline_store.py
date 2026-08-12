"""K 线本地持久化：parquet 按 (symbol, interval) 存储，open_time 去重合并。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

SCHEMA_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "is_closed"]


class KlineStore:
    def __init__(self, root: str | Path = ".beidou/data/klines") -> None:
        self._root = Path(root)

    def _path(self, symbol: str, interval: str) -> Path:
        return self._root / symbol / f"{interval}.parquet"

    def append(self, symbol: str, interval: str, klines: list[dict]) -> int:
        if not klines:
            return self._count_existing(symbol, interval)
        frame = pd.DataFrame(
            [
                {
                    "open_time": int(k["open_time"]),
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": float(k.get("volume", 0.0)),
                    "is_closed": bool(k.get("is_closed", False)),
                }
                for k in klines
            ],
            columns=SCHEMA_COLUMNS,
        )
        path = self._path(symbol, interval)
        if path.exists():
            existing = pd.read_parquet(path, columns=SCHEMA_COLUMNS)
            merged = (
                pd.concat([existing, frame], ignore_index=True)
                .drop_duplicates(subset=["open_time"], keep="last")
                .sort_values("open_time")
                .reset_index(drop=True)
            )
        else:
            merged = (
                frame.drop_duplicates(subset=["open_time"], keep="last")
                .sort_values("open_time")
                .reset_index(drop=True)
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, index=False)
        return len(merged)

    def load(self, symbol: str, interval: str) -> pd.DataFrame:
        path = self._path(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"kline store has no data for {symbol}/{interval}")
        return pd.read_parquet(path, columns=SCHEMA_COLUMNS)

    def has_data(self, symbol: str, interval: str, min_rows: int = 100) -> bool:
        path = self._path(symbol, interval)
        if not path.exists():
            return False
        df = pd.read_parquet(path, columns=["open_time"])
        return len(df) >= min_rows

    def last_open_time(self, symbol: str, interval: str) -> int | None:
        path = self._path(symbol, interval)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["open_time"])
        return int(df["open_time"].max()) if len(df) else None

    def _count_existing(self, symbol: str, interval: str) -> int:
        path = self._path(symbol, interval)
        if not path.exists():
            return 0
        return len(pd.read_parquet(path, columns=["open_time"]))
