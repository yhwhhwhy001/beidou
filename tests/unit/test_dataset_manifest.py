from pathlib import Path

import pandas as pd
import pytest

from beidou_research.data.dataset_manifest import (
    CrossSourceValidation,
    DatasetManifest,
    MarketDataProvenance,
)


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
    m2 = DatasetManifest.compute(_frame().iloc[::-1], "BTCUSDT", "1h")
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
    assert DatasetManifest.read(tmp_path / "missing.manifest.json") is None


def test_content_hash_does_not_depend_on_parquet_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _parquet_must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("manifest content identity must not depend on a parquet encoder")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", _parquet_must_not_be_called)

    manifest = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")

    assert manifest["content_hash_algorithm"] == "SHA256_OHLCV_FLOATHEX_V1"
    assert manifest["content_sha256"] == "d42aef41f056098208af3678bb10e4b09d0bbb37e6fff6ccb3d779c297257bcd"


def test_manifest_rejects_missing_columns_and_empty_frames() -> None:
    with pytest.raises(ValueError, match="MISSING_DATASET_COLUMNS:volume"):
        DatasetManifest.compute(_frame().drop(columns="volume"), "BTCUSDT", "1h")
    with pytest.raises(ValueError, match="EMPTY_DATASET"):
        DatasetManifest.compute(_frame().iloc[0:0], "BTCUSDT", "1h")


def test_content_verification_reports_unverifiable_and_incomplete_manifests() -> None:
    assert DatasetManifest.content_verification_reasons(pd.DataFrame(), {}, "BTCUSDT", "1h") == (
        "DATASET_CONTENT_UNVERIFIABLE",
    )

    missing = DatasetManifest.content_verification_reasons(_frame(), {}, "BTCUSDT", "1h")
    assert "MISSING_MANIFEST_FIELD:symbol" in missing
    assert "MISSING_MANIFEST_FIELD:content_hash_algorithm" in missing
    assert "MISSING_MANIFEST_FIELD:content_sha256" in missing

    unsupported = DatasetManifest.compute(_frame(), "BTCUSDT", "1h")
    unsupported["content_hash_algorithm"] = "UNKNOWN"
    assert "CONTENT_HASH_ALGORITHM_UNSUPPORTED" in DatasetManifest.content_verification_reasons(
        _frame(), unsupported, "BTCUSDT", "1h"
    )


def test_provenance_parser_and_endpoint_classifier_fail_closed() -> None:
    valid = MarketDataProvenance.from_endpoint(
        "https://fapi.binance.com/fapi/v1/klines",
        retrieved_at="2026-08-31T03:00:00Z",
    ).to_dict()
    assert (
        MarketDataProvenance.from_endpoint(
            "https://fapi.binance.com:invalid/fapi/v1/klines",
            retrieved_at="2026-08-31T03:00:00Z",
        ).source_class
        == "UNKNOWN"
    )

    with pytest.raises(ValueError, match="INVALID_PROVENANCE_FIELDS"):
        MarketDataProvenance.from_dict({"venue": "BINANCE_USDM"})
    invalid_type = dict(valid)
    invalid_type["venue"] = 7
    with pytest.raises(TypeError, match="INVALID_PROVENANCE_FIELD_TYPE"):
        MarketDataProvenance.from_dict(invalid_type)


def test_cross_source_parser_rejects_invalid_shape_and_types() -> None:
    valid = CrossSourceValidation(
        status="PASS",
        reference_endpoint="https://fapi.binance.com/fapi/v1/klines",
        reference_source_class="EXCHANGE_PUBLIC_API",
        checked_at="2026-08-31T03:10:00Z",
        sample_scope="EXTREMA_AND_RANDOM",
        sample_count=10,
        max_close_deviation_bps=0.1,
        evidence_sha256="a" * 64,
    ).to_dict()

    with pytest.raises(ValueError, match="INVALID_CROSS_SOURCE_FIELDS"):
        CrossSourceValidation.from_dict({"status": "PASS"})
    invalid_string = dict(valid)
    invalid_string["status"] = 1
    with pytest.raises(TypeError, match="INVALID_CROSS_SOURCE_FIELD_TYPE"):
        CrossSourceValidation.from_dict(invalid_string)
    invalid_count = dict(valid)
    invalid_count["sample_count"] = True
    with pytest.raises(TypeError, match="INVALID_CROSS_SOURCE_SAMPLE_COUNT"):
        CrossSourceValidation.from_dict(invalid_count)
    invalid_deviation = dict(valid)
    invalid_deviation["max_close_deviation_bps"] = False
    with pytest.raises(TypeError, match="INVALID_CROSS_SOURCE_DEVIATION"):
        CrossSourceValidation.from_dict(invalid_deviation)
