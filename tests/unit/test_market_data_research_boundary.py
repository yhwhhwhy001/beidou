from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from apps.factor_miner.worker import run_symbol_worker
from beidou_research.data.backfill import (
    _existing_provenance,
    _feed_endpoint,
    _safe_manifest_hash,
    _to_open_time_ms,
    backfill_all,
    backfill_symbol,
)
from beidou_research.data.dataset_manifest import (
    CrossSourceValidation,
    DatasetManifest,
    MarketDataProvenance,
)
from beidou_research.data.kline_store import KlineStore

HOUR_MS = 3_600_000
T0 = 1_700_000_000_000


def _frame(rows: int = 24) -> pd.DataFrame:
    prices = [100.0 + index * 0.1 for index in range(rows)]
    return pd.DataFrame(
        {
            "open_time": [T0 + index * HOUR_MS for index in range(rows)],
            "open": prices,
            "high": [price + 0.2 for price in prices],
            "low": [price - 0.2 for price in prices],
            "close": [price + 0.05 for price in prices],
            "volume": [10.0] * rows,
            "is_closed": [True] * rows,
        }
    )


def _trusted_provenance() -> MarketDataProvenance:
    return MarketDataProvenance.from_endpoint(
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/example.zip",
        retrieved_at="2026-08-31T03:00:00Z",
    )


def _cross_source(*, sample_scope: str = "EXTREMA_AND_RANDOM") -> CrossSourceValidation:
    return CrossSourceValidation(
        status="PASS",
        reference_endpoint="https://fapi.binance.com/fapi/v1/klines",
        reference_source_class="EXCHANGE_PUBLIC_API",
        checked_at="2026-08-31T03:10:00Z",
        sample_scope=sample_scope,
        sample_count=20,
        max_close_deviation_bps=0.1,
        evidence_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://demo-fapi.binance.com",
        "https://testnet.binancefuture.com",
        "https://testnet.binancefuture.com/fapi/v1/klines",
        "https://demo-fapi.binance.com.evil.example",
        "https://unknown.example",
        "http://data.binance.vision",
        "https://fapi.binance.com",
        "https://fapi.binance.com/fapi/v1/order",
        "https://fapi.binance.com/fapi/v1/klines.evil",
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/example.zip",
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/not-a-zip",
    ],
)
def test_non_research_endpoint_is_execution_only_and_rejected(endpoint: str) -> None:
    frame = _frame()
    provenance = MarketDataProvenance.from_endpoint(endpoint, retrieved_at="2026-08-31T03:00:00Z")
    manifest = DatasetManifest.compute(frame, "BTCUSDT", "1h", provenance=provenance)

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert provenance.intended_use == "EXECUTION_ONLY"
    assert assessment.eligible is False
    assert any(reason.startswith("SOURCE_NOT_RESEARCH_ELIGIBLE") for reason in assessment.reasons)


def test_legacy_manifest_is_not_economic_research_eligible() -> None:
    frame = _frame()
    legacy = {
        key: value
        for key, value in DatasetManifest.compute(frame, "BTCUSDT", "1h").items()
        if key not in {"schema_version", "content_hash_algorithm", "provenance", "cross_source_validation"}
    }

    assessment = DatasetManifest.assess_economic_research(frame, legacy, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "UNKNOWN_MANIFEST_SCHEMA" in assessment.reasons
    assert "MISSING_PROVENANCE" in assessment.reasons


def test_trusted_source_requires_cross_source_reconciliation() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(frame, "BTCUSDT", "1h", provenance=_trusted_provenance())

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "CROSS_SOURCE_VALIDATION_MISSING" in assessment.reasons


def test_clean_trusted_reconciled_dataset_is_economic_research_eligible() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is True
    assert assessment.reasons == ()
    assert assessment.to_economic_truth_evidence()["manifest_hash"] == DatasetManifest.hash_of(manifest)


def test_exact_public_api_kline_endpoint_can_use_archive_as_distinct_reference() -> None:
    frame = _frame()
    provenance = MarketDataProvenance.from_endpoint(
        "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h",
        retrieved_at="2026-08-31T03:00:00Z",
    )
    cross_source = CrossSourceValidation(
        status="PASS",
        reference_endpoint=(
            "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-08.zip"
        ),
        reference_source_class="OFFICIAL_PUBLIC_ARCHIVE",
        checked_at="2026-08-31T03:10:00Z",
        sample_scope="EXTREMA_AND_RANDOM",
        sample_count=20,
        max_close_deviation_bps=0.1,
        evidence_sha256="a" * 64,
    )
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=provenance,
        cross_source_validation=cross_source,
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is True


def test_unknown_manifest_fields_are_rejected() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    manifest["economic_research_eligible"] = True

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "UNKNOWN_MANIFEST_FIELDS:economic_research_eligible" in assessment.reasons


def test_manifest_reader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.manifest.json"
    path.write_text(
        '{"schema_version":"2.0","provenance":{"venue":"BINANCE_USDM","venue":"OTHER"}}\n',
        encoding="utf-8",
    )

    assert DatasetManifest.read(path) is None


def test_future_retrieval_timestamp_is_rejected() -> None:
    frame = _frame()
    provenance = MarketDataProvenance.from_endpoint(
        "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/example.zip",
        retrieved_at="2099-01-01T00:00:00Z",
    )
    cross_source = CrossSourceValidation(
        status="PASS",
        reference_endpoint="https://fapi.binance.com/fapi/v1/klines",
        reference_source_class="EXCHANGE_PUBLIC_API",
        checked_at="2099-01-01T00:10:00Z",
        sample_scope="EXTREMA_AND_RANDOM",
        sample_count=20,
        max_close_deviation_bps=0.1,
        evidence_sha256="a" * 64,
    )
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=provenance,
        cross_source_validation=cross_source,
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "RETRIEVED_AT_IN_FUTURE" in assessment.reasons
    assert "CROSS_SOURCE_CHECK_TIME_IN_FUTURE" in assessment.reasons


def test_cross_source_sample_count_cannot_exceed_dataset_rows() -> None:
    frame = _frame()
    oversized_check = CrossSourceValidation(
        status="PASS",
        reference_endpoint="https://fapi.binance.com/fapi/v1/klines",
        reference_source_class="EXCHANGE_PUBLIC_API",
        checked_at="2026-08-31T03:10:00Z",
        sample_scope="EXTREMA_AND_RANDOM",
        sample_count=len(frame) + 1,
        max_close_deviation_bps=0.1,
        evidence_sha256="a" * 64,
    )
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=oversized_check,
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "CROSS_SOURCE_SAMPLE_COUNT_EXCEEDS_ROWS" in assessment.reasons


def test_manifest_content_tampering_is_rejected() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    tampered = frame.copy()
    tampered.loc[3, "close"] = 999.0

    assessment = DatasetManifest.assess_economic_research(tampered, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "MANIFEST_CONTENT_MISMATCH" in assessment.reasons


@pytest.mark.parametrize(
    ("defect", "expected_reason"),
    [
        ("gap", "BAR_INTERVAL_DISCONTINUITY"),
        ("unclosed", "UNCLOSED_BARS"),
        ("non_finite", "NON_FINITE_OHLCV"),
        ("illegal_ohlc", "OHLCV_INVARIANT_VIOLATION"),
        ("negative_volume", "OHLCV_INVARIANT_VIOLATION"),
    ],
)
def test_market_data_quality_defects_fail_closed(defect: str, expected_reason: str) -> None:
    frame = _frame()
    if defect == "gap":
        frame.loc[8, "open_time"] += HOUR_MS // 2
    elif defect == "unclosed":
        frame.loc[8, "is_closed"] = False
    elif defect == "non_finite":
        frame.loc[8, "close"] = float("nan")
    elif defect == "illegal_ohlc":
        frame.loc[8, "high"] = frame.loc[8, "close"] - 1.0
    else:
        frame.loc[8, "volume"] = -1.0
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert expected_reason in assessment.reasons


def test_non_integral_bar_timestamp_is_rejected_without_hash_truncation() -> None:
    frame = _frame()
    frame["open_time"] = frame["open_time"].astype(float) + 0.5
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "BAR_TIMESTAMP_INVALID" in assessment.reasons


def test_missing_bar_timestamp_fails_closed() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    corrupt = frame.copy()
    corrupt.loc[0, "open_time"] = pd.NA

    assessment = DatasetManifest.assess_economic_research(corrupt, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "DATASET_CONTENT_UNVERIFIABLE" in assessment.reasons
    assert "BAR_TIMESTAMP_INVALID" in assessment.reasons


def test_out_of_range_manifest_timestamp_fails_closed() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    manifest["last_open_time"] = 10**100

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "DATA_END_TIME_INVALID" in assessment.reasons


def test_spoofed_demo_provenance_classification_is_rejected() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=MarketDataProvenance.from_endpoint(
            "https://demo-fapi.binance.com",
            retrieved_at="2026-08-31T03:00:00Z",
        ),
        cross_source_validation=_cross_source(),
    )
    manifest["provenance"].update(
        {
            "source_class": "OFFICIAL_PUBLIC_ARCHIVE",
            "environment": "PUBLIC_READ_ONLY",
            "intended_use": "ECONOMIC_RESEARCH",
        }
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "PROVENANCE_CLASSIFICATION_MISMATCH" in assessment.reasons


def test_large_opposite_reversal_requires_extrema_cross_source_sampling() -> None:
    frame = _frame()
    frame.loc[10, ["open", "high", "low", "close"]] = [100.0, 101.0, 49.0, 50.0]
    frame.loc[11, ["open", "high", "low", "close"]] = [50.0, 101.0, 49.0, 100.0]
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(sample_scope="RANDOM_ONLY"),
    )

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert "CROSS_SOURCE_SAMPLE_SCOPE_INSUFFICIENT" in assessment.reasons
    assert "EXTREME_RETURN_UNRECONCILED" in assessment.reasons
    assert "OPPOSITE_REVERSAL_UNRECONCILED" in assessment.reasons


@pytest.mark.parametrize(
    ("defect", "expected_reason"),
    [
        ("invalid_shape", "CROSS_SOURCE_VALIDATION_INVALID"),
        ("failed_status", "CROSS_SOURCE_STATUS_NOT_PASS"),
        ("untrusted_reference", "CROSS_SOURCE_REFERENCE_NOT_TRUSTED"),
        ("same_source", "CROSS_SOURCE_REFERENCE_NOT_DISTINCT"),
        ("invalid_time", "CROSS_SOURCE_CHECK_TIME_INVALID"),
        ("small_sample", "CROSS_SOURCE_SAMPLE_COUNT_INSUFFICIENT"),
        ("large_deviation", "CROSS_SOURCE_DEVIATION_EXCEEDED"),
        ("invalid_digest", "CROSS_SOURCE_EVIDENCE_DIGEST_INVALID"),
    ],
)
def test_cross_source_contract_defects_fail_closed(defect: str, expected_reason: str) -> None:
    frame = _frame()
    check: object = _cross_source().to_dict()
    if defect == "invalid_shape":
        check = {"status": "PASS"}
    else:
        assert isinstance(check, dict)
        if defect == "failed_status":
            check["status"] = "FAIL"
        elif defect == "untrusted_reference":
            check["reference_endpoint"] = "https://demo-fapi.binance.com"
            check["reference_source_class"] = "EXCHANGE_DEMO_API"
        elif defect == "same_source":
            check["reference_endpoint"] = _trusted_provenance().endpoint
            check["reference_source_class"] = "OFFICIAL_PUBLIC_ARCHIVE"
        elif defect == "invalid_time":
            check["checked_at"] = "not-a-time"
        elif defect == "small_sample":
            check["sample_count"] = 1
        elif defect == "large_deviation":
            check["max_close_deviation_bps"] = 6.0
        else:
            check["evidence_sha256"] = "not-a-digest"
    manifest = DatasetManifest.compute(frame, "BTCUSDT", "1h", provenance=_trusted_provenance())
    manifest["cross_source_validation"] = check

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert expected_reason in assessment.reasons


@pytest.mark.parametrize(
    ("defect", "interval", "expected_reason"),
    [
        ("missing_column", "1h", "DATASET_SCHEMA_INVALID"),
        ("empty", "1h", "DATASET_EMPTY"),
        ("duplicate_time", "1h", "BAR_TIME_ORDER_INVALID"),
        ("unsupported_interval", "2h", "INTERVAL_UNSUPPORTED"),
    ],
)
def test_additional_frame_quality_defects_fail_closed(defect: str, interval: str, expected_reason: str) -> None:
    clean = _frame()
    manifest = DatasetManifest.compute(
        clean,
        "BTCUSDT",
        interval,
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    candidate = clean.copy()
    if defect == "missing_column":
        candidate = candidate.drop(columns="volume")
    elif defect == "empty":
        candidate = candidate.iloc[0:0]
    elif defect == "duplicate_time":
        candidate.loc[8, "open_time"] = candidate.loc[7, "open_time"]

    assessment = DatasetManifest.assess_economic_research(candidate, manifest, "BTCUSDT", interval)

    assert assessment.eligible is False
    assert expected_reason in assessment.reasons


@pytest.mark.parametrize(
    ("defect", "expected_reason"),
    [
        ("invalid_mapping", "INVALID_PROVENANCE"),
        ("unsupported_venue", "VENUE_NOT_SUPPORTED"),
        ("invalid_retrieved_at", "RETRIEVED_AT_INVALID"),
        ("retrieved_before_data_end", "RETRIEVED_AT_BEFORE_DATA_END"),
    ],
)
def test_additional_provenance_defects_fail_closed(defect: str, expected_reason: str) -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    if defect == "invalid_mapping":
        manifest["provenance"] = {"venue": "BINANCE_USDM"}
    else:
        provenance = dict(manifest["provenance"])
        if defect == "unsupported_venue":
            provenance["venue"] = "OTHER"
        elif defect == "invalid_retrieved_at":
            provenance["retrieved_at"] = "not-a-timeZ"
        else:
            provenance["retrieved_at"] = "2020-01-01T00:00:00Z"
        manifest["provenance"] = provenance

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert expected_reason in assessment.reasons


def test_manifest_hash_with_non_json_number_fails_closed() -> None:
    frame = _frame()
    manifest = DatasetManifest.compute(
        frame,
        "BTCUSDT",
        "1h",
        provenance=_trusted_provenance(),
        cross_source_validation=_cross_source(),
    )
    manifest["unknown_nan"] = float("nan")

    assessment = DatasetManifest.assess_economic_research(frame, manifest, "BTCUSDT", "1h")

    assert assessment.eligible is False
    assert assessment.manifest_hash == ""
    assert "MANIFEST_HASH_UNVERIFIABLE" in assessment.reasons


class _DemoFeed:
    _rest_url = "https://demo-fapi.binance.com"

    def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "open_time": datetime.fromtimestamp((T0 + index * HOUR_MS) / 1000, tz=timezone.utc),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
                "is_closed": True,
            }
            for index in range(3)
        ]


def test_backfill_persists_demo_as_execution_only(tmp_path: Path) -> None:
    store = KlineStore(tmp_path / "klines")
    report = backfill_symbol(
        _DemoFeed(),
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )

    manifest = DatasetManifest.read(store.manifest_path("BTCUSDT", "1h"))
    assert not report["errors"] and manifest is not None
    assert manifest["provenance"]["endpoint"] == f"{_DemoFeed._rest_url}/fapi/v1/klines"
    assert manifest["provenance"]["source_class"] == "EXCHANGE_DEMO_API"
    assert manifest["provenance"]["intended_use"] == "EXECUTION_ONLY"


def test_backfill_cannot_launder_legacy_store_provenance(tmp_path: Path) -> None:
    store = KlineStore(tmp_path / "klines")
    store.append("BTCUSDT", "1h", _frame(1).to_dict("records"))
    frame = store.load("BTCUSDT", "1h")
    full = DatasetManifest.compute(frame, "BTCUSDT", "1h")
    legacy = {
        key: value
        for key, value in full.items()
        if key not in {"schema_version", "content_hash_algorithm", "provenance", "cross_source_validation"}
    }
    DatasetManifest.write(store.manifest_path("BTCUSDT", "1h"), legacy)
    before = store.data_path("BTCUSDT", "1h").read_bytes()

    report = backfill_symbol(
        _DemoFeed(),
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )

    assert "DATASET_PROVENANCE_UNVERIFIABLE" in report["errors"]
    assert store.data_path("BTCUSDT", "1h").read_bytes() == before


def test_backfill_cannot_reseal_manifest_content_mismatch(tmp_path: Path) -> None:
    store = KlineStore(tmp_path / "klines")
    store.append("BTCUSDT", "1h", _frame(1).to_dict("records"))
    original = store.load("BTCUSDT", "1h")
    provenance = MarketDataProvenance.from_endpoint(
        _DemoFeed._rest_url,
        retrieved_at="2026-08-31T03:00:00Z",
    )
    DatasetManifest.write(
        store.manifest_path("BTCUSDT", "1h"),
        DatasetManifest.compute(original, "BTCUSDT", "1h", provenance=provenance),
    )
    store.append("BTCUSDT", "1h", _frame(2).iloc[[1]].to_dict("records"))
    before = store.data_path("BTCUSDT", "1h").read_bytes()

    report = backfill_symbol(
        _DemoFeed(),
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )

    assert "DATASET_CONTENT_MISMATCH" in report["errors"]
    assert store.data_path("BTCUSDT", "1h").read_bytes() == before


def test_backfill_endpoint_and_manifest_helpers_fail_closed(tmp_path: Path) -> None:
    class PublicFeed:
        rest_url = "https://fapi.binance.com"

    class MissingEndpointFeed:
        pass

    class CustomFeed:
        rest_url = "https://custom.example/klines"

    assert _feed_endpoint(PublicFeed()) == f"{PublicFeed.rest_url}/fapi/v1/klines"
    assert _feed_endpoint(MissingEndpointFeed()) == ""
    assert _feed_endpoint(CustomFeed()) == CustomFeed.rest_url
    assert _to_open_time_ms(T0) == T0
    assert _safe_manifest_hash(None) == ""
    assert _safe_manifest_hash({"not_json": float("nan")}) == ""

    store = KlineStore(tmp_path / "klines")
    manifest_path = store.manifest_path("BTCUSDT", "1h")
    DatasetManifest.write(
        manifest_path,
        {
            "schema_version": "2.0",
            "provenance": [],
        },
    )
    assert _existing_provenance(store, "BTCUSDT", "1h")[1] is None
    DatasetManifest.write(
        manifest_path,
        {
            "schema_version": "2.0",
            "provenance": {"venue": "BINANCE_USDM"},
        },
    )
    assert _existing_provenance(store, "BTCUSDT", "1h")[1] is None


def test_backfill_retry_empty_resume_progress_and_batch_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("beidou_research.data.backfill.time.sleep", sleeps.append)

    class FailingFeed:
        _rest_url = _DemoFeed._rest_url

        def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("offline")

    failed = backfill_symbol(
        FailingFeed(),
        KlineStore(tmp_path / "failed"),
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + HOUR_MS,
        page_pause_seconds=0,
    )
    assert failed["errors"] == [f"page@{T0}:RuntimeError"]
    assert sleeps == [1.0, 2.0]

    class EmptyFeed:
        _rest_url = _DemoFeed._rest_url

        def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

    empty_reports = backfill_all(
        EmptyFeed(),
        KlineStore(tmp_path / "empty"),
        ["BTCUSDT"],
        ["1h"],
        start_ms=T0,
        end_ms=T0 + HOUR_MS,
        page_pause_seconds=0,
    )
    assert empty_reports[0]["rows"] == 0

    progress: list[str] = []
    pages = [_frame(1000).to_dict("records"), []]

    class PagedFeed:
        _rest_url = _DemoFeed._rest_url

        def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return pages.pop(0)

    paged = backfill_symbol(
        PagedFeed(),
        KlineStore(tmp_path / "paged"),
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 1001 * HOUR_MS,
        page_pause_seconds=0.25,
        on_progress=progress.append,
    )
    assert paged["rows"] == 1000
    assert progress == ["BTCUSDT 1h: page 1 rows 1000"]
    assert sleeps[-1] == 0.25

    resume_store = KlineStore(tmp_path / "resume")
    assert not backfill_symbol(
        _DemoFeed(),
        resume_store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )["errors"]
    resumed = backfill_symbol(
        EmptyFeed(),
        resume_store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )
    assert resumed["rows"] == 3
    assert resumed["manifest_hash"]


def test_backfill_rejects_source_change_before_fetch(tmp_path: Path) -> None:
    store = KlineStore(tmp_path / "klines")
    initial = backfill_symbol(
        _DemoFeed(),
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )
    assert not initial["errors"]

    class DifferentFeed:
        rest_url = "https://fapi.binance.com"

        def fetch_klines(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            raise AssertionError("source mismatch must stop before network fetch")

    report = backfill_symbol(
        DifferentFeed(),
        store,
        "BTCUSDT",
        "1h",
        start_ms=T0,
        end_ms=T0 + 10 * HOUR_MS,
        page_pause_seconds=0,
    )

    assert report["errors"] == ["DATASET_SOURCE_MISMATCH"]


def test_factor_worker_rejects_legacy_local_manifest_before_mining(tmp_path: Path) -> None:
    store = KlineStore(tmp_path / "klines")
    records = _frame(120).to_dict("records")
    store.append("BTCUSDT", "1h", records)
    frame = store.load("BTCUSDT", "1h")
    full = DatasetManifest.compute(frame, "BTCUSDT", "1h")
    legacy = {
        key: value
        for key, value in full.items()
        if key not in {"schema_version", "content_hash_algorithm", "provenance", "cross_source_validation"}
    }
    DatasetManifest.write(store.manifest_path("BTCUSDT", "1h"), legacy)

    with pytest.raises(RuntimeError, match="DATASET_NOT_ECONOMIC_RESEARCH_ELIGIBLE.*UNKNOWN_MANIFEST_SCHEMA"):
        run_symbol_worker(
            {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "limit": 500,
                "policy": str(tmp_path / "must-not-be-read.yaml"),
                "output_dir": str(tmp_path / "evidence"),
                "from_store": True,
                "data_root": str(tmp_path / "klines"),
            }
        )
