"""#19 ingestion: the store where absence is expressible, and a downloader that does not blur it.

Every test here is offline (`httpx.MockTransport`).  The one fact the network supplied is recorded
rather than re-fetched: measured 2026-09-09, `futures/um/monthly/liquidationSnapshot/` and
`futures/um/daily/liquidationSnapshot/` list ZERO keys, and the only published liquidation archive is
coin-margined daily, 2023-06-25 .. 2024-10-14.  Pointed at `um`, this ingest stores nothing - and the
test that matters is that "stored nothing" reaches a signal as NaN rather than as calm markets.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

HOUR_MS = 60 * 60 * 1000
DAY = "2024-10-01"

CSV = (
    "time,side,order_type,time_in_force,original_quantity,price,average_price,order_status,"
    "last_fill_quantity,accumulated_fill_quantity\n"
    "1727740869082,BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
    "1727740869082,BUY,LIMIT,IOC,7,63661.4,63425.1,FILLED,7,7\n"
    "1727742058019,SELL,LIMIT,IOC,1,62779.5,63020.9,FILLED,1,1\n"
    "1727742058019,SELL,LIMIT,IOC,1,62779.5,63020.9,FILLED,1,1\n"
)
HEADER_ONLY = CSV.splitlines()[0] + "\n"


def _zip_bytes(text: str, name: str = f"BTCUSDT-liquidationSnapshot-{DAY}.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr(name, text)
    return buffer.getvalue()


def _transport(
    payload: bytes | None, *, checksum: str | None = None, status: int = 200
) -> tuple[httpx.MockTransport, list[str]]:
    """Answers with `payload`, or with `status` when it is None.  Also records what was asked for."""
    seen: list[str] = []
    body = payload or b""
    digest = checksum if checksum is not None else hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if payload is None:
            return httpx.Response(status)
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{digest}  x.zip\n")
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler), seen


def _client(transport: httpx.MockTransport, market: str = "um"):  # type: ignore[no-untyped-def]
    from beidou_data.liquidation_archive import LiquidationArchiveClient

    return LiquidationArchiveClient(market=market, transport=transport)


# --- the store ------------------------------------------------------------------------------------


def test_a_fetched_day_with_no_liquidations_still_leaves_a_file(tmp_path: Path) -> None:
    """The whole design in one assertion: an empty parquet is a fact, not an absence.

    Every other store here lets `max(timestamp)` be its own watermark, which works when a row exists
    for every period.  A quiet day has no rows and is still a day we know about.
    """
    from beidou_data.liquidations import LiquidationStore, empty_events

    store = LiquidationStore(tmp_path)
    rows = store.write_day("BTCUSDT", DAY, empty_events())

    assert rows == 0
    assert store.has_day("BTCUSDT", DAY)
    assert store.covered_days("BTCUSDT") == [DAY]


def test_a_day_that_was_never_fetched_is_not_covered(tmp_path: Path) -> None:
    from beidou_data.liquidations import LiquidationStore

    store = LiquidationStore(tmp_path)

    assert store.covered_days("BTCUSDT") == []
    assert not store.has_day("BTCUSDT", DAY)


def test_the_store_round_trips_events_in_time_order(tmp_path: Path) -> None:
    from beidou_data.liquidations import EVENT_COLUMNS, LiquidationStore, parse_archive_csv

    store = LiquidationStore(tmp_path)
    store.write_day("BTCUSDT", DAY, parse_archive_csv(CSV, "BTCUSDT"))
    loaded = store.load("BTCUSDT")

    assert list(loaded.columns) == list(EVENT_COLUMNS)
    assert len(loaded) == 2  # four archive rows, doubled, so two liquidations
    assert loaded["time"].is_monotonic_increasing


def test_markets_are_stored_apart(tmp_path: Path) -> None:
    """`um` and `cm` quantities mean different things (base asset against 100-USD contracts).

    One directory for both would let a coin-margined backfill answer a USDⓈ-M query, and the numbers
    would look entirely reasonable.
    """
    from beidou_data.liquidations import LiquidationStore, empty_events

    LiquidationStore(tmp_path, market="cm").write_day("BTCUSD_PERP", DAY, empty_events())

    assert LiquidationStore(tmp_path, market="um").covered_days("BTCUSD_PERP") == []
    assert LiquidationStore(tmp_path, market="cm").covered_days("BTCUSD_PERP") == [DAY]


def test_coverage_marks_only_the_bars_whose_days_were_fetched(tmp_path: Path) -> None:
    from beidou_data.liquidations import LiquidationStore, empty_events

    store = LiquidationStore(tmp_path)
    store.write_day("BTCUSDT", DAY, empty_events())
    bars = pd.date_range("2024-10-01 22:00", periods=4, freq="h", tz="UTC")

    covered = store.coverage("BTCUSDT", bars, interval_ms=HOUR_MS)

    # 22:00 and 23:00 are inside the fetched day; 00:00 and 01:00 of the 2nd were never fetched.
    assert covered.tolist() == [True, True, False, False]


def test_a_bar_wider_than_a_day_needs_every_day_it_touches(tmp_path: Path) -> None:
    """Otherwise a two-day bar would be called covered on the strength of its first hour."""
    from beidou_data.liquidations import LiquidationStore, empty_events

    store = LiquidationStore(tmp_path)
    store.write_day("BTCUSDT", DAY, empty_events())
    bars = pd.DatetimeIndex([pd.Timestamp(DAY, tz="UTC")])

    assert store.coverage("BTCUSDT", bars, interval_ms=2 * 86_400_000).tolist() == [False]


# --- the downloader -------------------------------------------------------------------------------


def test_a_published_day_is_verified_against_its_checksum_and_de_doubled(tmp_path: Path) -> None:
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    transport, seen = _transport(_zip_bytes(CSV))
    store = LiquidationStore(tmp_path)

    with _client(transport) as client:
        stored = sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")

    assert stored == {"BTCUSDT": 1}
    assert any(path.endswith(".CHECKSUM") for path in seen)
    assert len(store.load("BTCUSDT")) == 2


def test_a_corrupt_zip_is_refused(tmp_path: Path) -> None:
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    transport, _ = _transport(_zip_bytes(CSV), checksum="0" * 64)
    store = LiquidationStore(tmp_path)

    with _client(transport) as client:
        stored = sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")

    # Isolated per symbol, like `sync_metrics`: reported as nothing stored, and nothing written.
    assert stored == {}
    assert store.covered_days("BTCUSDT") == []


def test_a_day_the_venue_never_published_leaves_no_file(tmp_path: Path) -> None:
    """The inversion this downloader exists for.

    A 404 is unknown; an empty published day is a genuine zero.  Writing a file for the 404 would
    invent coverage, and every bar in it would then read 0.0 instead of NaN.
    """
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    transport, _ = _transport(None, status=404)
    store = LiquidationStore(tmp_path)

    with _client(transport) as client:
        stored = sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")

    assert stored == {}
    assert store.covered_days("BTCUSDT") == []


def test_a_published_but_empty_day_does_leave_a_file(tmp_path: Path) -> None:
    """The other half of the same distinction: header-only csv is data saying "nothing happened"."""
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    transport, _ = _transport(_zip_bytes(HEADER_ONLY))
    store = LiquidationStore(tmp_path)

    with _client(transport) as client:
        sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")

    assert store.covered_days("BTCUSDT") == [DAY]
    assert store.load("BTCUSDT").empty


def test_a_day_already_held_is_not_requested_again(tmp_path: Path) -> None:
    """The store is the watermark; a re-run is cheap rather than a re-download."""
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    transport, seen = _transport(_zip_bytes(CSV))
    store = LiquidationStore(tmp_path)

    with _client(transport) as client:
        sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")
        before = len(seen)
        sync_liquidations(client, store, ["BTCUSDT"], start=DAY, end="2024-10-02")

    assert len(seen) == before


def test_the_url_names_the_market_and_the_day(tmp_path: Path) -> None:
    """Recorded because the plan named a path that does not exist (`um/monthly/liquidationSnapshot`)."""
    from beidou_data.liquidation_archive import liquidation_path

    assert liquidation_path("BTCUSDT", DAY) == (
        f"/data/futures/um/daily/liquidationSnapshot/BTCUSDT/BTCUSDT-liquidationSnapshot-{DAY}.zip"
    )
    assert "/futures/cm/daily/" in liquidation_path("BTCUSD_PERP", DAY, market="cm")


def test_a_file_that_stops_being_doubled_fails_the_symbol_instead_of_halving_it(tmp_path: Path) -> None:
    """`collapse_archive_duplication` raises, and the sync turns that into a reported skip."""
    from beidou_data.liquidation_archive import sync_liquidations
    from beidou_data.liquidations import LiquidationStore

    odd = CSV + "1727742058019,SELL,LIMIT,IOC,1,62779.5,63020.9,FILLED,1,1\n"
    transport, _ = _transport(_zip_bytes(odd))
    store = LiquidationStore(tmp_path)
    notes: list[str] = []

    with _client(transport) as client:
        stored = sync_liquidations(
            client, store, ["BTCUSDT"], start=DAY, end="2024-10-02", progress=lambda s, *_: notes.append(s)
        )

    assert stored == {}
    assert any("FAILED" in note and "Anomaly" in note for note in notes)


# --- what an unfetched universe reaches a signal as ------------------------------------------------


def test_an_ingest_that_downloaded_nothing_reads_as_unknown_everywhere(tmp_path: Path) -> None:
    """The USDⓈ-M case, end to end, as measured on 2026-09-09: the archive has no keys at all.

    Nothing is stored, so every bar is NaN.  The failure this pins is the plausible alternative - a
    column of zeros, which is a perfectly usable signal input meaning "no liquidations ever".
    """
    from beidou_data.liquidations import AGGREGATE_COLUMNS, LiquidationStore, to_panel_columns

    store = LiquidationStore(tmp_path)
    bars = pd.date_range("2024-10-01", periods=3, freq="h", tz="UTC")

    columns = to_panel_columns(store, ["BTCUSDT", "ETHUSDT"], bars, interval_ms=HOUR_MS)

    assert set(columns) == set(AGGREGATE_COLUMNS)
    for frame in columns.values():
        assert frame.shape == (3, 2)
        assert frame.isna().all().all()


def test_one_symbol_fetched_and_one_not_do_not_average_into_each_other(tmp_path: Path) -> None:
    from beidou_data.liquidations import LiquidationStore, parse_archive_csv, to_panel_columns

    store = LiquidationStore(tmp_path)
    store.write_day("BTCUSDT", DAY, parse_archive_csv(CSV, "BTCUSDT"))
    bars = pd.date_range(DAY, periods=2, freq="h", tz="UTC")

    columns = to_panel_columns(store, ["BTCUSDT", "ETHUSDT"], bars, interval_ms=HOUR_MS)
    shorts = columns["liq_notional_short"]

    assert shorts["BTCUSDT"].notna().all()
    assert shorts["ETHUSDT"].isna().all()
    # 00:41:09 UTC, a BUY of 7 at an average 63425.1 - a SHORT liquidated, in the 00:00 bar.
    assert shorts["BTCUSDT"].iloc[0] == pytest.approx(7 * 63425.1)
    assert shorts["BTCUSDT"].iloc[1] == 0.0


def test_the_aggregate_columns_are_reported_but_not_combined(tmp_path: Path) -> None:
    """No ratio column: a ratio cannot be taken apart, and its 0/0 case would be invented here."""
    from beidou_data.liquidations import AGGREGATE_COLUMNS

    assert not any("ratio" in name or "imbalance" in name for name in AGGREGATE_COLUMNS)
    assert np.all([name.startswith("liq_") for name in AGGREGATE_COLUMNS])
