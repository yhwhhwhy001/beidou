from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent / "tools"
sys.path.insert(0, str(TOOLS))

from acquire_public_klines import (  # noqa: E402
    parse_api_payload,
    parse_archive_zip,
    parse_checksum,
    reconcile_rows,
    validate_complete_rows,
)

HOUR_MS = 3_600_000
T0 = 1_785_542_400_000


def _api_row(open_time: int = T0, close: str = "100.50") -> list[object]:
    return [
        open_time,
        "100.00",
        "101.00",
        "99.00",
        close,
        "12.50",
        open_time + HOUR_MS - 1,
        "1250.00",
        42,
        "6.25",
        "625.00",
        "0",
    ]


def _archive(rows: list[list[object]], name: str = "BTCUSDT-1h-2026-08-01.csv") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        body = (
            "open_time,open,high,low,close,volume,close_time,quote_volume,"
            "count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
        )
        body += "\n".join(",".join(str(value) for value in row) for row in rows) + "\n"
        bundle.writestr(name, body)
    return stream.getvalue()


def test_checksum_requires_exact_sha256_and_filename() -> None:
    filename = "BTCUSDT-1h-2026-08-01.zip"
    digest = hashlib.sha256(b"archive").hexdigest()

    assert parse_checksum(f"{digest}  {filename}\n".encode(), filename) == digest
    with pytest.raises(ValueError, match="CHECKSUM_FILENAME_MISMATCH"):
        parse_checksum(f"{digest}  other.zip\n".encode(), filename)
    with pytest.raises(ValueError, match="CHECKSUM_FORMAT_INVALID"):
        parse_checksum(b"not-a-checksum\n", filename)


def test_archive_and_api_parsers_produce_identical_typed_rows() -> None:
    row = _api_row()
    archive_rows = parse_archive_zip(_archive([row]), "BTCUSDT-1h-2026-08-01.csv")
    api_rows = parse_api_payload(json.dumps([row]).encode())

    assert archive_rows == api_rows
    assert archive_rows[0].open_time == T0
    assert str(archive_rows[0].close) == "100.50"


def test_archive_parser_normalizes_exact_microseconds_to_milliseconds() -> None:
    row = _api_row()
    row[0] = T0 * 1000
    row[6] = (T0 + HOUR_MS - 1) * 1000 + 999

    parsed = parse_archive_zip(_archive([row]), "BTCUSDT-1h-2026-08-01.csv")

    assert parsed[0].open_time == T0
    assert parsed[0].close_time == T0 + HOUR_MS - 1


def test_complete_range_rejects_gap_duplicate_and_unclosed_boundary() -> None:
    rows = parse_api_payload(json.dumps([_api_row(T0), _api_row(T0 + HOUR_MS)]).encode())
    validate_complete_rows(rows, start_ms=T0, end_ms=T0 + 2 * HOUR_MS, interval_ms=HOUR_MS)

    with pytest.raises(ValueError, match="BAR_INTERVAL_DISCONTINUITY"):
        validate_complete_rows(
            [rows[0], parse_api_payload(json.dumps([_api_row(T0 + 2 * HOUR_MS)]).encode())[0]],
            start_ms=T0,
            end_ms=T0 + 2 * HOUR_MS,
            interval_ms=HOUR_MS,
        )
    with pytest.raises(ValueError, match="BAR_TIME_DUPLICATE"):
        validate_complete_rows(
            [rows[0], rows[0]], start_ms=T0, end_ms=T0 + 2 * HOUR_MS, interval_ms=HOUR_MS
        )
    with pytest.raises(ValueError, match="DATASET_RANGE_MISMATCH"):
        validate_complete_rows(rows[:1], start_ms=T0, end_ms=T0 + 2 * HOUR_MS, interval_ms=HOUR_MS)


def test_reconciliation_compares_every_bar_and_ohlcv_field() -> None:
    primary = parse_api_payload(json.dumps([_api_row(T0), _api_row(T0 + HOUR_MS)]).encode())
    matched = reconcile_rows(primary, list(primary))

    assert matched["status"] == "PASS"
    assert matched["compared_rows"] == 2
    assert matched["max_close_deviation_bps"] == 0.0

    changed = parse_api_payload(json.dumps([_api_row(T0), _api_row(T0 + HOUR_MS, close="100.60")]).encode())
    with pytest.raises(ValueError, match="CROSS_SOURCE_VALUE_MISMATCH:close"):
        reconcile_rows(primary, changed)
