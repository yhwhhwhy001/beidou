from pathlib import Path

import pandas as pd

from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore, frame_to_price_data


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": [1_700_000_000_000, 1_700_003_600_000],
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.9, 1.9],
            "close": [1.2, 2.2],
            "volume": [10.0, 20.0],
            "is_closed": [True, True],
        }
    )


def test_frame_to_price_data_schema() -> None:
    rows = frame_to_price_data(_frame())
    assert len(rows) == 2
    first = rows[0]
    assert first["close"] == 1.2 and first["is_closed"] is True
    assert first["timestamp"].tzinfo is not None  # aware datetime
    assert first["open_time"] == 1_700_000_000_000


def test_manifest_hash_roundtrip_for_injection(tmp_path: Path) -> None:
    store = KlineStore(root=str(tmp_path / "klines"))
    frame = _frame()
    store.append("BTCUSDT", "1h", frame.to_dict("records"))
    manifest = DatasetManifest.compute(store.load("BTCUSDT", "1h"), "BTCUSDT", "1h")
    h = DatasetManifest.hash_of(manifest)
    assert len(h) == 64  # _validated_manifest_hash 要求完整 64 位 hex
