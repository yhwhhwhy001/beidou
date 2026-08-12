"""数据集清单：为 (symbol, interval) 数据生成溯源哈希，绑定 EvidenceBundle。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd


class DatasetManifest:
    @staticmethod
    def compute(df: pd.DataFrame, symbol: str, interval: str) -> dict[str, Any]:
        ordered = df.sort_values("open_time").reset_index(drop=True)
        buffer = io.BytesIO()
        ordered.to_parquet(buffer, index=False, compression="zstd")
        content_sha256 = hashlib.sha256(buffer.getvalue()).hexdigest()
        return {
            "symbol": symbol,
            "interval": interval,
            "rows": len(ordered),
            "first_open_time": int(ordered["open_time"].min()),
            "last_open_time": int(ordered["open_time"].max()),
            "content_sha256": content_sha256,
        }

    @staticmethod
    def hash_of(manifest: dict[str, Any]) -> str:
        payload = json.dumps(manifest, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def write(path: Path, manifest: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
