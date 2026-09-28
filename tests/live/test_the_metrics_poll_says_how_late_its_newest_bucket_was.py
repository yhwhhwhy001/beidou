"""The metrics poll records how far behind the bar close its newest bucket was (backtest-guard 2026-09-29).

Research gives a bar the 5m bucket that closed AT the bar's close (`beidou_data.metrics.align_to_bars`), and
`beidou_data.alignment.METRICS` says the venue's latency past that close "belongs in a health report" - which
did not exist.  REST publishes a bucket two to three minutes after it closes (measured 2026-09-07), and the
loop polls about 30 s past the close, after the cycle's targets.  So the reading is expected to be one bucket,
and nothing reads metrics to decide yet.  These tests pin the record (`snapshot_metrics`), the count
(`report_data.snapshot_lag`) and the line the daily report prints.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from beidou_data.metrics_snapshot import snapshot_metrics
from beidou_data.store import MetricsStore
from beidou_live.report_data import SNAPSHOT_LAG_CYCLES, _data_family_lines, snapshot_lag

FIVE = 300_000
HOUR = 3_600_000
BAR = 1_790_604_000_000  # 2026-09-28T14:00Z opens; it closes at BAR + HOUR
CLOSE_STAMPED = {
    "openInterestHist",
    "topLongShortAccountRatio",
    "topLongShortPositionRatio",
    "globalLongShortAccountRatio",
}
FIELDS = {
    "openInterestHist": {"sumOpenInterest": "1.0", "sumOpenInterestValue": "2.0"},
    "topLongShortAccountRatio": {"longShortRatio": "1.7"},
    "topLongShortPositionRatio": {"longShortRatio": "2.1"},
    "globalLongShortAccountRatio": {"longShortRatio": "1.6"},
    "takerlongshortRatio": {"buySellRatio": "0.9"},
}


class _Client:
    """Each symbol's newest bucket OPENS at `newest[symbol]`; each page stamps it its own way (open or close)."""

    def __init__(self, newest: dict[str, int], empty: set[tuple[str, str]] = frozenset()) -> None:
        self.newest = newest
        self.empty = empty

    async def get(self, path: str, params: Any = None) -> Any:
        name, symbol = path.rsplit("/", 1)[-1], params["symbol"]
        if (name, symbol) in self.empty:
            return []
        opened = self.newest[symbol]
        stamps = (opened - FIVE, opened)
        return [
            {"symbol": symbol, **FIELDS[name], "timestamp": stamp + (FIVE if name in CLOSE_STAMPED else 0)}
            for stamp in stamps
        ]


def _poll(tmp_path: Path, client: _Client, symbols: list[str]) -> dict[str, Any]:
    return asyncio.run(snapshot_metrics(client, MetricsStore(tmp_path, kind="metrics_snapshot"), symbols))


def test_the_poll_records_the_oldest_and_freshest_newest_bucket_per_page(tmp_path: Path) -> None:
    """Five pages, two conventions: the span is in bucket OPENS whichever stamp the page used."""
    newest = {"BTCUSDT": BAR + HOUR - FIVE, "ETHUSDT": BAR + HOUR - 2 * FIVE}
    result = _poll(tmp_path, _Client(newest), ["BTCUSDT", "ETHUSDT"])
    assert set(result["newest_open_ms"]) == set(FIELDS)
    for span in result["newest_open_ms"].values():
        assert span == [BAR + HOUR - 2 * FIVE, BAR + HOUR - FIVE]


def test_a_page_that_answered_nothing_for_a_symbol_does_not_enter_its_span(tmp_path: Path) -> None:
    newest = {"BTCUSDT": BAR + HOUR - FIVE, "ETHUSDT": BAR + HOUR - 3 * FIVE}
    client = _Client(newest, empty={("takerlongshortRatio", "ETHUSDT")})
    result = _poll(tmp_path, client, ["BTCUSDT", "ETHUSDT"])
    assert result["newest_open_ms"]["takerlongshortRatio"] == [BAR + HOUR - FIVE] * 2
    assert result["newest_open_ms"]["openInterestHist"] == [BAR + HOUR - 3 * FIVE, BAR + HOUR - FIVE]
    assert result["missing"] == {"ETHUSDT": ["takerlongshortRatio"]}


def _row(bar: int, oldest: int, freshest: int | None = None) -> dict[str, Any]:
    span = [oldest, oldest if freshest is None else freshest]
    return {"bar_open_ms": bar, "metrics_snapshot": {"stored": {}, "newest_open_ms": {"openInterestHist": span}}}


def test_the_lag_is_counted_in_buckets_behind_the_close_for_the_slowest_symbol() -> None:
    """0 means REST had research's bucket; 1 means the bucket closing at the bar close was not out yet."""
    rows = [
        _row(BAR, BAR + HOUR - FIVE),  # the bucket closing AT the close: research's, lag 0
        _row(BAR + HOUR, BAR + 2 * HOUR - 2 * FIVE, BAR + 2 * HOUR - FIVE),  # slowest symbol one behind
        {"bar_open_ms": BAR + 2 * HOUR, "metrics_snapshot": {"stored": {}}},  # before the field existed
    ]
    assert snapshot_lag(rows, HOUR, FIVE) == {"cycles": 2, "by_page": {"openInterestHist": {"0": 1, "1": 1}}}


def test_only_the_newest_day_of_polls_is_read() -> None:
    rows = [_row(BAR + i * HOUR, BAR + (i + 1) * HOUR - (2 if i < 6 else 1) * FIVE) for i in range(30)]
    lag = snapshot_lag(rows, HOUR, FIVE)
    assert lag["cycles"] == SNAPSHOT_LAG_CYCLES
    assert lag["by_page"]["openInterestHist"] == {"0": SNAPSHOT_LAG_CYCLES}


def test_the_daily_line_says_the_lag_or_that_there_is_no_reading_yet() -> None:
    base = {"readable": True, "symbols": 16, "request_window": 1442, "families": []}
    counted = {"cycles": 24, "by_page": {"openInterestHist": {"1": 23, "0": 1}}}
    line = _data_family_lines({**base, "snapshot_lag": counted})["快照时的桶延迟"]
    assert line.startswith("openInterestHist 落后 0 桶 1 次、落后 1 桶 23 次") and "最近 24 个周期" in line
    assert "还没有读数" in _data_family_lines({**base, "snapshot_lag": {"cycles": 0, "by_page": {}}})["快照时的桶延迟"]
