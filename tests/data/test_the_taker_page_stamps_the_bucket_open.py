"""The taker page stamps a bucket by its OPEN; the other four metrics pages stamp it by its CLOSE.

Found 2026-09-27, the first time M-011 compared the ratio columns: the snapshot's
`sum_taker_long_short_vol_ratio` matched the archive five minutes LATER - a median 45% apart bucket for
bucket, 2e-4 once shifted.  `parse_rest_rows` took one period off every page's `timestamp`, a convention
measured on `openInterestHist` alone (166/166, 2026-09-07); the ratio pages added on 2026-09-12 were
checked against the archive's value RANGES, never its stamps.

Measured the same day with `verify_stamp_offset` against the venue - BTCUSDT, the 2026-09-25 archive
against the REST window: open interest PASSES under `METRICS` at 1e-12 and the three account/position
ratios at 1e-3, while the taker page FAILS under it at every tolerance (0 to 3 of 146) and PASSES at
offset 0 (145/145 within 1e-3, rivals 1/144 and 1/146).  The samples below are 24 of those buckets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_data.alignment import FAIL, METRICS, PASS, contract_for, verify_stamp_offset
from beidou_data.metrics import PERIOD_MS, REST_SOURCES, parse_rest_rows, rest_stamp_offset_ms
from beidou_data.metrics_snapshot import snapshot_metrics
from beidou_data.store import MetricsStore

TAKER = "sum_taker_long_short_vol_ratio"
FIVE_MIN = PERIOD_MS["5m"]

#: BTCUSDT 2026-09-25 21:00-22:55Z from the daily archive: (`create_time` as epoch ms, value).
ARCHIVE = [
    (1790370000000, 0.306043), (1790370300000, 0.291834), (1790370600000, 0.792037), (1790370900000, 0.269919),
    (1790371200000, 2.299335), (1790371500000, 5.654423), (1790371800000, 1.898107), (1790372100000, 1.484853),
    (1790372400000, 1.615827), (1790372700000, 1.796339), (1790373000000, 0.318808), (1790373300000, 0.500503),
    (1790373600000, 1.65506), (1790373900000, 1.373363), (1790374200000, 0.600427), (1790374500000, 0.930971),
    (1790374800000, 0.886206), (1790375100000, 0.579943), (1790375400000, 1.593659), (1790375700000, 1.592391),
    (1790376000000, 1.222685), (1790376300000, 0.801636), (1790376600000, 2.327837), (1790376900000, 0.527371),
]  # fmt: skip
#: The same span as `/futures/data/takerlongshortRatio` served it on 2026-09-27: (`timestamp`, `buySellRatio`).
REST = [
    (1790369700000, 0.5153), (1790370000000, 0.3061), (1790370300000, 0.2919), (1790370600000, 0.7919),
    (1790370900000, 0.2699), (1790371200000, 2.2995), (1790371500000, 5.6542), (1790371800000, 1.8981),
    (1790372100000, 1.4848), (1790372400000, 1.616), (1790372700000, 1.7963), (1790373000000, 0.3188),
    (1790373300000, 0.5006), (1790373600000, 1.6546), (1790373900000, 1.3734), (1790374200000, 0.6004),
    (1790374500000, 0.931), (1790374800000, 0.8862), (1790375100000, 0.5799), (1790375400000, 1.5939),
    (1790375700000, 1.5923), (1790376000000, 1.2228), (1790376300000, 0.8016), (1790376600000, 2.3275),
    (1790376900000, 0.5273), (1790377200000, 0.9056),
]  # fmt: skip


def _archive() -> pd.DataFrame:
    stamps, values = zip(*ARCHIVE, strict=True)
    return pd.DataFrame({"create_time": pd.to_datetime(stamps, unit="ms", utc=True), TAKER: values})


def _rest() -> pd.DataFrame:
    stamps, values = zip(*REST, strict=True)
    return pd.DataFrame({"timestamp": stamps, TAKER: values})


def test_the_taker_contract_holds_on_the_venue_and_the_shared_one_does_not() -> None:
    """The 24/24 half and the 1/24 half, on the venue's own numbers; REST prints four decimals."""
    declared = verify_stamp_offset(contract_for(TAKER), _archive(), _rest(), value_columns=[TAKER], tolerance=1e-3)
    shared = verify_stamp_offset(METRICS, _archive(), _rest(), value_columns=[TAKER], tolerance=1e-3)

    assert declared.verdict == PASS, declared.reason
    assert declared.matched == declared.compared == 24
    assert all(rival.matched <= 1 for rival in declared.rivals), declared.rivals
    assert shared.verdict == FAIL, "the convention every page used to share, on this page"


def test_every_page_is_parsed_at_the_offset_its_columns_contract_declares() -> None:
    """The parser and the contracts, held against each other page by page, so neither moves alone."""
    for path, mapping in REST_SOURCES:
        for column in mapping.values():
            assert rest_stamp_offset_ms(path, FIVE_MIN) == contract_for(column).stamp_offset_ms, (path, column)


def test_the_loop_files_each_page_under_the_bucket_the_archive_names(tmp_path: Path) -> None:
    """Through `snapshot_metrics`, the path the loop takes: the taker value lands on its own bucket."""
    opening = 1790372400000  # 2026-09-25T22:00Z

    class Venue:
        async def get(self, path: str, params: Any = None) -> Any:
            if path.endswith("takerlongshortRatio"):
                return [
                    {"buySellRatio": "1.616", "timestamp": opening},
                    {"buySellRatio": "1.7963", "timestamp": opening + FIVE_MIN},
                ]
            field = "sumOpenInterest" if path.endswith("openInterestHist") else "longShortRatio"
            return [{"symbol": "BTCUSDT", field: "1.0", "timestamp": opening + FIVE_MIN}]

    store = MetricsStore(tmp_path, kind="metrics_snapshot")
    asyncio.run(snapshot_metrics(Venue(), store, ["BTCUSDT"]))
    row = store.load("BTCUSDT").set_index("open_time").loc[opening]

    assert row[TAKER] == 1.616, "the archive's 22:00 bucket reads 1.615827"


def test_the_default_is_still_the_close_stamp_every_other_page_uses() -> None:
    frame = parse_rest_rows([{"symbol": "BTCUSDT", "sumOpenInterest": "1.0", "timestamp": 600_000}], FIVE_MIN)
    opened = parse_rest_rows(
        [{"buySellRatio": "1.0", "timestamp": 600_000}], FIVE_MIN, {"buySellRatio": TAKER}, stamp_offset_ms=0
    )

    assert frame["open_time"].tolist() == [300_000]
    assert opened["open_time"].tolist() == [600_000]
