"""Independent, offline verification of the frozen ALPHA-DATA-002 artifacts.

This verifier intentionally does not import the acquisition helper. It parses
the raw API JSON and archive CSV files again, checks published checksums and
artifact hashes, and re-runs the repository's economic-research gate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from beidou_research.data.dataset_manifest import DatasetManifest  # noqa: E402

DATASET = REPOSITORY_ROOT / "artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
START_MS = 1_785_542_400_000
END_MS = 1_788_134_400_000
HOUR_MS = 3_600_000
EXPECTED_ROWS = 720
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DECIMAL_INDICES = (1, 2, 3, 4, 5, 7, 9, 10)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp_ms(value: object) -> int:
    raw = int(str(value))
    return raw // 1000 if raw >= 1_000_000_000_000_000 else raw


def _normalized_raw_row(row: object) -> tuple[object, ...]:
    if not isinstance(row, list) or len(row) != 12:
        raise AssertionError("KLINE_ROW_SHAPE_INVALID")
    normalized: list[object] = []
    for index in range(11):
        if index in (0, 6):
            normalized.append(_timestamp_ms(row[index]))
        elif index == 8:
            normalized.append(int(str(row[index])))
        elif index in _DECIMAL_INDICES:
            value = Decimal(str(row[index]))
            if not value.is_finite():
                raise AssertionError(f"NON_FINITE_RAW_VALUE:{index}")
            normalized.append(value)
        else:
            raise AssertionError(f"UNEXPECTED_RAW_FIELD:{index}")
    return tuple(normalized)


def _api_rows(path: Path) -> list[tuple[object, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise AssertionError("API_PAYLOAD_NOT_LIST")
    return [_normalized_raw_row(row) for row in payload]


def _archive_rows(path: Path) -> list[tuple[object, ...]]:
    with zipfile.ZipFile(path) as bundle:
        files = [item for item in bundle.infolist() if not item.is_dir()]
        if len(files) != 1:
            raise AssertionError(f"ARCHIVE_MEMBER_COUNT:{path.name}")
        records = list(csv.reader(io.StringIO(bundle.read(files[0]).decode("utf-8-sig"))))
    if records and records[0] and records[0][0].lower() == "open_time":
        records = records[1:]
    return [_normalized_raw_row(row) for row in records]


def _published_checksum(path: Path) -> tuple[str, str]:
    parts = path.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or _HEX64.fullmatch(parts[0].lower()) is None:
        raise AssertionError(f"CHECKSUM_FORMAT_INVALID:{path.name}")
    return parts[0].lower(), parts[1]


def _assert_cadence(rows: list[tuple[object, ...]]) -> None:
    if len(rows) != EXPECTED_ROWS:
        raise AssertionError(f"ROW_COUNT:{len(rows)}")
    times = [int(row[0]) for row in rows]
    if times != list(range(START_MS, END_MS, HOUR_MS)):
        raise AssertionError("BAR_CADENCE_OR_RANGE_MISMATCH")
    if len(times) != len(set(times)):
        raise AssertionError("BAR_TIME_DUPLICATE")
    if any(int(row[6]) != int(row[0]) + HOUR_MS - 1 for row in rows):
        raise AssertionError("BAR_CLOSE_TIME_MISMATCH")


def _assert_frame_matches_raw(frame: pd.DataFrame, raw: list[tuple[object, ...]]) -> None:
    if frame["open_time"].astype("int64").tolist() != [int(row[0]) for row in raw]:
        raise AssertionError("PARQUET_TIME_MISMATCH")
    if not frame["is_closed"].map(bool).all():
        raise AssertionError("PARQUET_UNCLOSED_BAR")
    mapping = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
    for column, index in mapping.items():
        actual = frame[column].astype(float).tolist()
        expected = [float(row[index]) for row in raw]
        if any(
            abs(left - right) > max(1e-12, abs(right) * 1e-12)
            for left, right in zip(actual, expected, strict=True)
        ):
            raise AssertionError(f"PARQUET_VALUE_MISMATCH:{column}")


def verify() -> dict[str, Any]:
    index_path = DATASET / "dataset-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("status") != "PASS" or tuple(index.get("symbols", ())) != SYMBOLS:
        raise AssertionError("INDEX_SCOPE_MISMATCH")
    results: dict[str, Any] = {}
    for symbol in SYMBOLS:
        item = index["datasets"][symbol]
        api_path = DATASET / "raw/api" / f"{symbol}-1h.json"
        raw_api = _api_rows(api_path)
        _assert_cadence(raw_api)

        archive_root = DATASET / "raw/archive" / symbol
        zip_paths = sorted(archive_root.glob("*.zip"))
        checksum_paths = sorted(archive_root.glob("*.zip.CHECKSUM"))
        if len(zip_paths) != 30 or len(checksum_paths) != 30:
            raise AssertionError(f"ARCHIVE_FILE_COUNT:{symbol}")
        raw_archive: list[tuple[object, ...]] = []
        checksum_count = 0
        for zip_path in zip_paths:
            checksum_path = zip_path.with_name(f"{zip_path.name}.CHECKSUM")
            published, filename = _published_checksum(checksum_path)
            if filename != zip_path.name or published != _sha256(zip_path):
                raise AssertionError(f"CHECKSUM_MISMATCH:{zip_path.name}")
            checksum_count += 1
            raw_archive.extend(_archive_rows(zip_path))
        _assert_cadence(raw_archive)
        if raw_api != raw_archive:
            for position, (left, right) in enumerate(zip(raw_api, raw_archive, strict=True)):
                if left != right:
                    raise AssertionError(f"RAW_SOURCE_MISMATCH:{symbol}:{position}")
            raise AssertionError(f"RAW_SOURCE_LENGTH_MISMATCH:{symbol}")

        parquet_path = REPOSITORY_ROOT / item["parquet_path"]
        manifest_path = REPOSITORY_ROOT / item["manifest_path"]
        reconciliation_path = REPOSITORY_ROOT / item["reconciliation_path"]
        if DATASET.resolve() not in parquet_path.resolve().parents:
            raise AssertionError("PARQUET_PATH_ESCAPE")
        frame = pd.read_parquet(parquet_path)
        _assert_frame_matches_raw(frame, raw_api)
        manifest = DatasetManifest.read(manifest_path)
        if manifest is None:
            raise AssertionError("MANIFEST_UNREADABLE")
        assessment = DatasetManifest.assess_economic_research(frame, manifest, symbol, "1h")
        if not assessment.eligible:
            raise AssertionError(f"ECONOMIC_GATE_FAIL:{symbol}:{assessment.reasons}")
        reconciliation_sha256 = _sha256(reconciliation_path)
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        if (
            reconciliation.get("status") != "PASS"
            or reconciliation["comparison"].get("compared_rows") != EXPECTED_ROWS
            or reconciliation["comparison"].get("mismatch_count") != 0
            or reconciliation["comparison"].get("max_close_deviation_bps") != 0.0
            or len(reconciliation.get("archive_files", ())) != 30
        ):
            raise AssertionError(f"RECONCILIATION_SUMMARY_INVALID:{symbol}")
        if reconciliation["api"]["sha256"] != _sha256(api_path):
            raise AssertionError(f"API_RAW_HASH_MISMATCH:{symbol}")
        if manifest["cross_source_validation"]["evidence_sha256"] != reconciliation_sha256:
            raise AssertionError(f"RECONCILIATION_BINDING_MISMATCH:{symbol}")
        if (
            item["parquet_sha256"] != _sha256(parquet_path)
            or item["manifest_sha256"] != _sha256(manifest_path)
            or item["manifest_hash"] != DatasetManifest.hash_of(manifest)
            or item["reconciliation_sha256"] != reconciliation_sha256
        ):
            raise AssertionError(f"INDEX_HASH_MISMATCH:{symbol}")

        returns = frame["close"].astype(float).pct_change(fill_method=None)
        results[symbol] = {
            "status": "PASS",
            "rows": len(frame),
            "official_checksums_verified": checksum_count,
            "raw_rows_compared": len(raw_api),
            "raw_field_mismatches": 0,
            "max_close_deviation_bps": 0.0,
            "max_abs_hourly_return": float(returns.abs().max()),
            "lag1_return_autocorrelation": float(returns.autocorr(lag=1)),
            "manifest_hash": DatasetManifest.hash_of(manifest),
            "assessment": assessment.to_economic_truth_evidence(),
        }
    return {
        "status": "PASS",
        "verifier": "offline_second_implementation",
        "dataset": str(DATASET.relative_to(REPOSITORY_ROOT)),
        "symbols": results,
        "limitations": [
            (
                "Both source classes are Binance-operated; this is path/class independence, "
                "not institutional independence."
            ),
            "Network acquisition authenticity also depends on the recorded Cloudflare DoH CNAME transport evidence.",
        ],
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(verify(), sort_keys=True, indent=2, allow_nan=False) + "\n")
