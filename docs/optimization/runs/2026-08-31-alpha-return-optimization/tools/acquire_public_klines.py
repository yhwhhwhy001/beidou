"""Run-local, read-only acquisition of frozen Binance USD-M K-lines.

The logical primary endpoint remains ``fapi.binance.com/fapi/v1/klines``.
This environment's local DNS path blocks that hostname, so the HTTPS
transport uses the CloudFront distribution returned by Cloudflare DoH while
preserving ``Host: fapi.binance.com``.  Official daily archive ZIPs and their
published SHA-256 checksums provide a distinct, full-range reconciliation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

import httpx
import pandas as pd

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from beidou_research.data.dataset_manifest import (  # noqa: E402
    CrossSourceValidation,
    DatasetManifest,
    MarketDataProvenance,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
INTERVAL = "1h"
INTERVAL_MS = 3_600_000
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 31, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)
END_MS = int(END.timestamp() * 1000)
EXPECTED_ROWS = (END_MS - START_MS) // INTERVAL_MS
LOGICAL_API_ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
LOGICAL_API_HOST = "fapi.binance.com"
API_TRANSPORT_HOST = "d2ukl3c6tymv7q.cloudfront.net"
API_TRANSPORT_ENDPOINT = f"https://{API_TRANSPORT_HOST}/fapi/v1/klines"
ARCHIVE_BASE = "https://data.binance.vision/data/futures/um/daily/klines"
OUTPUT_DEFAULT = REPOSITORY_ROOT / "artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1"
TRANSPORT_BASIS_EVIDENCE = (
    "artifacts/evidence/ALPHA-DATA-002-PUBLIC-API-CLOUDFRONT-TRANSPORT/"
    "20260831T182623729546Z.manifest.json"
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DECIMAL_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base",
    "taker_buy_quote",
)


@dataclass(frozen=True, slots=True)
class KlineRow:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time: int
    quote_volume: Decimal
    trades: int
    taker_buy_base: Decimal
    taker_buy_quote: Decimal

    def frame_record(self) -> dict[str, object]:
        return {
            "open_time": self.open_time,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": float(self.volume),
            "is_closed": True,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _timestamp_ms(value: object) -> int:
    try:
        raw = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("BAR_TIMESTAMP_INVALID") from exc
    if 1_000_000_000_000 <= raw < 100_000_000_000_000:
        return raw
    if 1_000_000_000_000_000 <= raw < 100_000_000_000_000_000:
        return raw // 1000
    raise ValueError("BAR_TIMESTAMP_UNIT_INVALID")


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"BAR_DECIMAL_INVALID:{field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"BAR_DECIMAL_NON_FINITE:{field}")
    return parsed


def _row(value: object) -> KlineRow:
    if not isinstance(value, (list, tuple)) or len(value) != 12:
        raise ValueError("KLINE_ROW_SHAPE_INVALID")
    try:
        trades = int(str(value[8]))
    except (TypeError, ValueError) as exc:
        raise ValueError("BAR_TRADE_COUNT_INVALID") from exc
    row = KlineRow(
        open_time=_timestamp_ms(value[0]),
        open=_decimal(value[1], "open"),
        high=_decimal(value[2], "high"),
        low=_decimal(value[3], "low"),
        close=_decimal(value[4], "close"),
        volume=_decimal(value[5], "volume"),
        close_time=_timestamp_ms(value[6]),
        quote_volume=_decimal(value[7], "quote_volume"),
        trades=trades,
        taker_buy_base=_decimal(value[9], "taker_buy_base"),
        taker_buy_quote=_decimal(value[10], "taker_buy_quote"),
    )
    if min(row.open, row.high, row.low, row.close) <= 0:
        raise ValueError("OHLC_NON_POSITIVE")
    if min(row.volume, row.quote_volume, row.taker_buy_base, row.taker_buy_quote) < 0 or trades < 0:
        raise ValueError("VOLUME_OR_COUNT_NEGATIVE")
    if row.high < max(row.open, row.close, row.low) or row.low > min(row.open, row.close, row.high):
        raise ValueError("OHLC_INVARIANT_VIOLATION")
    return row


def parse_checksum(payload: bytes, expected_filename: str) -> str:
    try:
        parts = payload.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise ValueError("CHECKSUM_FORMAT_INVALID") from exc
    if len(parts) != 2 or _HEX64.fullmatch(parts[0].lower()) is None:
        raise ValueError("CHECKSUM_FORMAT_INVALID")
    if parts[1] != expected_filename or Path(parts[1]).name != parts[1]:
        raise ValueError("CHECKSUM_FILENAME_MISMATCH")
    return parts[0].lower()


def parse_archive_zip(payload: bytes, expected_csv_name: str) -> list[KlineRow]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            files = [info for info in bundle.infolist() if not info.is_dir()]
            if len(files) != 1 or files[0].filename != expected_csv_name:
                raise ValueError("ARCHIVE_MEMBER_MISMATCH")
            if files[0].file_size > 10_000_000:
                raise ValueError("ARCHIVE_MEMBER_TOO_LARGE")
            body = bundle.read(files[0]).decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ValueError("ARCHIVE_INVALID") from exc

    records = list(csv.reader(io.StringIO(body)))
    if not records:
        raise ValueError("ARCHIVE_EMPTY")
    if records[0] and records[0][0].strip().lower() == "open_time":
        records = records[1:]
    if not records or any(not record for record in records):
        raise ValueError("ARCHIVE_EMPTY")
    return [_row(record) for record in records]


def parse_api_payload(payload: bytes) -> list[KlineRow]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("API_JSON_INVALID") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("API_PAYLOAD_NOT_KLINES")
    return [_row(record) for record in value]


def validate_complete_rows(rows: list[KlineRow], *, start_ms: int, end_ms: int, interval_ms: int) -> None:
    expected_count = (end_ms - start_ms) // interval_ms
    if end_ms <= start_ms or (end_ms - start_ms) % interval_ms != 0 or len(rows) != expected_count:
        raise ValueError("DATASET_RANGE_MISMATCH")
    times = [row.open_time for row in rows]
    if len(times) != len(set(times)):
        raise ValueError("BAR_TIME_DUPLICATE")
    if any(right - left != interval_ms for left, right in pairwise(times)):
        raise ValueError("BAR_INTERVAL_DISCONTINUITY")
    if times[0] != start_ms or times[-1] != end_ms - interval_ms:
        raise ValueError("DATASET_RANGE_MISMATCH")
    if any(row.close_time != row.open_time + interval_ms - 1 for row in rows):
        raise ValueError("BAR_CLOSE_TIME_INVALID")


def reconcile_rows(primary: list[KlineRow], reference: list[KlineRow]) -> dict[str, object]:
    if len(primary) != len(reference):
        raise ValueError("CROSS_SOURCE_ROW_COUNT_MISMATCH")
    max_close_deviation_bps = Decimal("0")
    for left, right in zip(primary, reference, strict=True):
        if left.open_time != right.open_time:
            raise ValueError("CROSS_SOURCE_TIME_MISMATCH")
        for field in _DECIMAL_FIELDS:
            left_value = getattr(left, field)
            right_value = getattr(right, field)
            if left_value != right_value:
                raise ValueError(f"CROSS_SOURCE_VALUE_MISMATCH:{field}@{left.open_time}")
        if left.close_time != right.close_time:
            raise ValueError(f"CROSS_SOURCE_VALUE_MISMATCH:close_time@{left.open_time}")
        if left.trades != right.trades:
            raise ValueError(f"CROSS_SOURCE_VALUE_MISMATCH:trades@{left.open_time}")
        deviation = abs(left.close - right.close) / right.close * Decimal("10000")
        max_close_deviation_bps = max(max_close_deviation_bps, deviation)
    return {
        "status": "PASS",
        "compared_rows": len(primary),
        "compared_fields": ["open_time", *_DECIMAL_FIELDS, "close_time", "trades"],
        "max_close_deviation_bps": float(max_close_deviation_bps),
        "mismatch_count": 0,
    }


def _fetch(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(url, params=params, headers=headers)
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"REDIRECT_REJECTED:{response.status_code}")
            response.raise_for_status()
            return response
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise RuntimeError(f"PUBLIC_READ_FAILED:{url}:{type(last_error).__name__}") from last_error


def _dates(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


def _download_archive_day(
    client: httpx.Client,
    output: Path,
    symbol: str,
    day: date,
) -> tuple[list[KlineRow], dict[str, object]]:
    stamp = day.isoformat()
    filename = f"{symbol}-{INTERVAL}-{stamp}.zip"
    csv_name = filename.removesuffix(".zip") + ".csv"
    url = f"{ARCHIVE_BASE}/{symbol}/{INTERVAL}/{filename}"
    checksum_url = f"{url}.CHECKSUM"
    archive_response = _fetch(client, url)
    checksum_response = _fetch(client, checksum_url)
    expected_sha256 = parse_checksum(checksum_response.content, filename)
    actual_sha256 = _sha256(archive_response.content)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"OFFICIAL_CHECKSUM_MISMATCH:{filename}")
    rows = parse_archive_zip(archive_response.content, csv_name)
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    validate_complete_rows(rows, start_ms=day_start, end_ms=day_start + 86_400_000, interval_ms=INTERVAL_MS)
    raw_root = output / "raw/archive" / symbol
    _write_bytes(raw_root / filename, archive_response.content)
    _write_bytes(raw_root / f"{filename}.CHECKSUM", checksum_response.content)
    return rows, {
        "date": stamp,
        "url": url,
        "checksum_url": checksum_url,
        "filename": filename,
        "rows": len(rows),
        "bytes": len(archive_response.content),
        "sha256": actual_sha256,
        "published_sha256": expected_sha256,
        "etag": archive_response.headers.get("etag", ""),
        "last_modified": archive_response.headers.get("last-modified", ""),
    }


def _download_api(
    client: httpx.Client,
    output: Path,
    symbol: str,
) -> tuple[list[KlineRow], dict[str, object], str]:
    params: dict[str, object] = {
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": START_MS,
        "endTime": END_MS - 1,
        "limit": 1000,
    }
    response = _fetch(client, API_TRANSPORT_ENDPOINT, params=params, headers={"Host": LOGICAL_API_HOST})
    retrieved_at = _utc_now()
    rows = parse_api_payload(response.content)
    validate_complete_rows(rows, start_ms=START_MS, end_ms=END_MS, interval_ms=INTERVAL_MS)
    raw_path = output / "raw/api" / f"{symbol}-{INTERVAL}.json"
    _write_bytes(raw_path, response.content)
    return rows, {
        "logical_endpoint": LOGICAL_API_ENDPOINT,
        "logical_request_url": f"{LOGICAL_API_ENDPOINT}?{urlencode(params)}",
        "logical_host_header": LOGICAL_API_HOST,
        "transport_endpoint": API_TRANSPORT_ENDPOINT,
        "transport_host": API_TRANSPORT_HOST,
        "transport_basis_evidence": TRANSPORT_BASIS_EVIDENCE,
        "retrieved_at": retrieved_at,
        "rows": len(rows),
        "bytes": len(response.content),
        "sha256": _sha256(response.content),
        "content_type": response.headers.get("content-type", ""),
    }, retrieved_at


def _frame(rows: list[KlineRow]) -> pd.DataFrame:
    return pd.DataFrame([row.frame_record() for row in rows])


def acquire(output: Path) -> dict[str, object]:
    allowed_root = (REPOSITORY_ROOT / "artifacts/datasets").resolve()
    resolved_output = output.resolve()
    if allowed_root not in resolved_output.parents:
        raise ValueError("OUTPUT_OUTSIDE_DATASET_ARTIFACT_ROOT")
    if output.exists():
        raise FileExistsError(f"OUTPUT_ALREADY_EXISTS:{output}")
    if datetime.now(timezone.utc) < END:
        raise ValueError("DATASET_END_IN_FUTURE")
    output.mkdir(parents=True)

    timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=30.0)
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
    index: dict[str, object] = {
        "schema_version": "1.0",
        "status": "IN_PROGRESS",
        "venue": "BINANCE_USDM",
        "symbols": list(SYMBOLS),
        "interval": INTERVAL,
        "clock": "UTC",
        "start_inclusive": START.isoformat().replace("+00:00", "Z"),
        "end_exclusive": END.isoformat().replace("+00:00", "Z"),
        "expected_rows_per_symbol": EXPECTED_ROWS,
        "created_at": _utc_now(),
        "datasets": {},
    }
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            limits=limits,
            trust_env=False,
            headers={"User-Agent": "beidou-read-only-market-data/1.0"},
        ) as client:
            api_results = {symbol: _download_api(client, output, symbol) for symbol in SYMBOLS}
            archive_results: dict[str, list[tuple[list[KlineRow], dict[str, object]]]] = {
                symbol: [] for symbol in SYMBOLS
            }
            jobs: dict[object, tuple[str, date]] = {}
            with ThreadPoolExecutor(max_workers=8) as executor:
                for symbol in SYMBOLS:
                    for day in _dates(START.date(), END.date()):
                        future = executor.submit(_download_archive_day, client, output, symbol, day)
                        jobs[future] = (symbol, day)
                for future in as_completed(jobs):
                    symbol, _ = jobs[future]
                    archive_results[symbol].append(future.result())

        datasets: dict[str, object] = {}
        for symbol in SYMBOLS:
            api_rows, api_evidence, retrieved_at = api_results[symbol]
            ordered_archives = sorted(archive_results[symbol], key=lambda item: str(item[1]["date"]))
            archive_rows = [row for daily_rows, _ in ordered_archives for row in daily_rows]
            archive_evidence = [metadata for _, metadata in ordered_archives]
            validate_complete_rows(archive_rows, start_ms=START_MS, end_ms=END_MS, interval_ms=INTERVAL_MS)
            comparison = reconcile_rows(api_rows, archive_rows)
            checked_at = _utc_now()
            reconciliation = {
                "schema_version": "1.0",
                "status": "PASS",
                "symbol": symbol,
                "interval": INTERVAL,
                "start_inclusive": START.isoformat().replace("+00:00", "Z"),
                "end_exclusive": END.isoformat().replace("+00:00", "Z"),
                "sample_scope": "EXTREMA_AND_RANDOM",
                "coverage": "FULL_RANGE_INCLUDES_EXTREMA_AND_RANDOM",
                "api": api_evidence,
                "archive_files": archive_evidence,
                "comparison": comparison,
                "checked_at": checked_at,
                "limitations": [
                    "Both source classes are operated by Binance; independence is path/class, not institutional.",
                    (
                        "The API transport uses the official CloudFront CNAME with Host fapi.binance.com "
                        "because local DNS/SNI is blocked."
                    ),
                ],
            }
            reconciliation_bytes = _json_bytes(reconciliation)
            reconciliation_path = output / "reconciliation" / f"{symbol}-{INTERVAL}.json"
            _write_bytes(reconciliation_path, reconciliation_bytes)
            reconciliation_sha256 = _sha256(reconciliation_bytes)

            frame = _frame(api_rows)
            parquet_path = output / "frozen" / symbol / f"{INTERVAL}.parquet"
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(parquet_path, index=False)
            persisted_frame = pd.read_parquet(parquet_path)
            provenance = MarketDataProvenance.from_endpoint(LOGICAL_API_ENDPOINT, retrieved_at=retrieved_at)
            cross_source = CrossSourceValidation(
                status="PASS",
                reference_endpoint=str(archive_evidence[0]["url"]),
                reference_source_class="OFFICIAL_PUBLIC_ARCHIVE",
                checked_at=checked_at,
                sample_scope="EXTREMA_AND_RANDOM",
                sample_count=len(api_rows),
                max_close_deviation_bps=float(comparison["max_close_deviation_bps"]),
                evidence_sha256=reconciliation_sha256,
            )
            manifest = DatasetManifest.compute(
                persisted_frame,
                symbol,
                INTERVAL,
                provenance=provenance,
                cross_source_validation=cross_source,
            )
            assessment = DatasetManifest.assess_economic_research(persisted_frame, manifest, symbol, INTERVAL)
            manifest_path = output / "frozen" / symbol / f"{INTERVAL}.manifest.json"
            DatasetManifest.write(manifest_path, manifest)
            if not assessment.eligible:
                raise ValueError(f"ECONOMIC_RESEARCH_GATE_FAILED:{symbol}:{','.join(assessment.reasons)}")
            datasets[symbol] = {
                "status": "PASS",
                "rows": len(persisted_frame),
                "first_open_time": int(persisted_frame["open_time"].iloc[0]),
                "last_open_time": int(persisted_frame["open_time"].iloc[-1]),
                "parquet_path": str(parquet_path.relative_to(REPOSITORY_ROOT)),
                "parquet_sha256": _sha256(parquet_path.read_bytes()),
                "manifest_path": str(manifest_path.relative_to(REPOSITORY_ROOT)),
                "manifest_sha256": _sha256(manifest_path.read_bytes()),
                "manifest_hash": DatasetManifest.hash_of(manifest),
                "reconciliation_path": str(reconciliation_path.relative_to(REPOSITORY_ROOT)),
                "reconciliation_sha256": reconciliation_sha256,
                "assessment": assessment.to_economic_truth_evidence(),
            }
        index["status"] = "PASS"
        index["completed_at"] = _utc_now()
        index["datasets"] = datasets
        index_bytes = _json_bytes(index)
        _write_bytes(output / "dataset-index.json", index_bytes)
        return index
    except Exception as exc:
        failure = {
            **index,
            "status": "FAIL",
            "failed_at": _utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        _write_bytes(output / "RUN_FAILED.json", _json_bytes(failure))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    args = parser.parse_args()
    result = acquire(args.output)
    summary = {"status": result["status"], "output": str(args.output), "datasets": result["datasets"]}
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
