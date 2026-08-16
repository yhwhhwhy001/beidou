from pathlib import Path

import pandas as pd

from beidou_research.data.dataset_manifest import DatasetManifest


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": [1000, 2000],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10.0, 20.0],
            "is_closed": [True, True],
        }
    )


def test_manifest_is_deterministic() -> None:
    m1 = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    m2 = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    assert m1 == m2
    assert DatasetManifest.hash_of(m1) == DatasetManifest.hash_of(m2)


def test_hash_is_full_sha256_hex() -> None:
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    h = DatasetManifest.hash_of(m)
    assert len(h) == 64
    int(h, 16)  # 必须为合法 hex


def test_manifest_fields_and_content_change() -> None:
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    assert m["symbol"] == "BTCUSDT" and m["interval"] == "1h" and m["rows"] == 2
    assert m["first_open_time"] == 1000 and m["last_open_time"] == 2000
    changed = _frame()
    changed.loc[1, "close"] = 9.9
    m2 = DatasetManifest.compute(changed, "BTCUSDT", "1h")
    assert DatasetManifest.hash_of(m) != DatasetManifest.hash_of(m2)


def test_write_read_roundtrip(tmp_path: Path) -> None:
    m = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    DatasetManifest.write(tmp_path / "1h.manifest.json", m)
    loaded = DatasetManifest.read(tmp_path / "1h.manifest.json")
    assert loaded == m
