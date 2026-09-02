from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent / "tools"
sys.path.insert(0, str(TOOLS))

from acquire_cost_capacity_evidence import (  # noqa: E402
    calibrated_market_envelope,
    parse_book_depth_archive,
    parse_checksum,
    parse_depth_snapshot,
    parse_funding_payload,
    walk_book,
)


def _depth_archive(rows: list[str], name: str = "BTCUSDT-bookDepth-2026-08-01.csv") -> bytes:
    payload = "timestamp,percentage,depth,notional\n" + "\n".join(rows) + "\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(name, payload)
    return stream.getvalue()


def test_official_depth_checksum_and_exact_member_are_required() -> None:
    filename = "BTCUSDT-bookDepth-2026-08-01.zip"
    digest = hashlib.sha256(b"depth").hexdigest()
    assert parse_checksum(f"{digest}  {filename}\n".encode(), filename) == digest
    with pytest.raises(ValueError, match="CHECKSUM_FILENAME_MISMATCH"):
        parse_checksum(f"{digest}  other.zip\n".encode(), filename)
    with pytest.raises(ValueError, match="BOOK_DEPTH_MEMBER_MISMATCH"):
        parse_book_depth_archive(_depth_archive([], "other.csv"), filename.removesuffix(".zip") + ".csv")


def test_depth_archive_extracts_complete_two_sided_20bps_samples() -> None:
    rows = [
        "2026-08-01 00:00:00,-0.20,10,1000000",
        "2026-08-01 00:00:00,0.20,11,1100000",
        "2026-08-01 00:00:00,-1.00,20,2000000",
        "2026-08-01 00:00:30,-0.20,12,1200000",
        "2026-08-01 00:00:30,0.20,9,900000",
    ]
    parsed = parse_book_depth_archive(_depth_archive(rows), "BTCUSDT-bookDepth-2026-08-01.csv")
    assert len(parsed) == 2
    assert parsed[0].two_sided_notional_20bps == Decimal("1000000")
    assert parsed[1].two_sided_notional_20bps == Decimal("900000")

    with pytest.raises(ValueError, match="BOOK_DEPTH_SIDE_INCOMPLETE"):
        parse_book_depth_archive(
            _depth_archive(rows[:1]), "BTCUSDT-bookDepth-2026-08-01.csv"
        )


def test_public_funding_parser_rejects_wrong_symbol_and_out_of_order_rows() -> None:
    rows = [
        {"symbol": "BTCUSDT", "fundingTime": 1785542400001, "fundingRate": "0.0001"},
        {"symbol": "BTCUSDT", "fundingTime": 1785571200000, "fundingRate": "-0.0002"},
    ]
    assert len(parse_funding_payload(json.dumps(rows).encode(), expected_symbol="BTCUSDT")) == 2
    changed = [dict(rows[0]), dict(rows[1])]
    changed[1]["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="FUNDING_SYMBOL_MISMATCH"):
        parse_funding_payload(json.dumps(changed).encode(), expected_symbol="BTCUSDT")
    with pytest.raises(ValueError, match="FUNDING_TIME_RANGE_OR_ORDER_INVALID"):
        parse_funding_payload(json.dumps(list(reversed(rows))).encode(), expected_symbol="BTCUSDT")


def test_depth_snapshot_walk_includes_spread_and_book_impact() -> None:
    payload = {
        "lastUpdateId": 1,
        "E": 1788276767368,
        "T": 1788276767365,
        "bids": [["99.9", "10"], ["99.8", "20"]],
        "asks": [["100.1", "10"], ["100.2", "20"]],
    }
    parsed = parse_depth_snapshot(json.dumps(payload).encode(), expected_symbol="BTCUSDT")
    result = walk_book(bids=parsed["bids"], asks=parsed["asks"], notional=Decimal("1500"))
    assert result["status"] == "PASS"
    assert result["buy_adverse_bps_vs_mid"] > 10.0
    assert result["sell_adverse_bps_vs_mid"] > 10.0


def test_capacity_envelope_is_constrained_by_depth_or_participation() -> None:
    result = calibrated_market_envelope(
        two_sided_depth_notional=[1_000_000, 1_100_000, 900_000],
        hourly_quote_volume=[2_000_000, 3_000_000, 4_000_000],
        participation_rate=0.10,
    )
    assert result["market_capacity_envelope_notional"] < result["depth_20bps_p05_notional"]
    assert result["capacity_constraint"] == "HOURLY_VOLUME"
    assert result["strategy_capacity_status"] == "NOT_VERIFIABLE_NO_CANDIDATE_NET_RETURN"
