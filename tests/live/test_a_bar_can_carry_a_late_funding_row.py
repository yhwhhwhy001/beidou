"""Since 2026-09-27 one bar can own two attribution rows, and every reader has to add, not replace.

Funding settled under a book is read a cycle late (`engine.FUNDING_SETTLE_LAG_MS`) and written under the
bar that paid it, flagged ``late_funding``: the bar's own row came from the cycle after it, this one from
the cycle after that.  The readers that assumed one row per `bar_open_ms`, and what each would have done:

* `_series_by_strategy` assigned per bar, so the late row REPLACED the bar's trading P&L in M-010,
  the decay rule, M-G06 and M-014's correlation;
* O3 would have read every late row as a window ingested twice - its stretch is one a normal row covers;
* `_booked_fees` would have handed a flatten's commission key to the late row, written the same second
  under an older bar;
* `leg_split` would have counted a symbol that traded and paid funding in one bar as two symbol-bars.

Sums of `total` / `by_strategy` over rows (probe stop, R8's ladder, collateral drift, the daily totals,
governance replay) were already right and are not repeated here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from beidou_live.report_decay import _series_by_strategy, attribution_coverage, income_drift
from beidou_live.report_execution import _booked_fees
from beidou_live.report_risk import leg_split
from beidou_live.state import StateStore
from tests.live.test_the_attribution_series_is_checked_for_holes import BASE, HOUR, SECOND
from tests.live.test_the_attribution_series_is_checked_for_holes import _store as chained_store

LAG = 600 * SECOND


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


def test_a_bar_with_a_late_funding_row_is_one_point_carrying_both(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    for index in range(6):
        store.append_cycle({"bar_open_ms": BASE + index * HOUR, "equity": 10_000.0})
    store.append_attribution({"bar_open_ms": BASE + HOUR, "by_strategy": {"tsmom": -1.0, "flow": 0.25}})
    store.append_attribution({"bar_open_ms": BASE + 2 * HOUR, "by_strategy": {"tsmom": 0.5}})
    store.append_attribution({"bar_open_ms": BASE + HOUR, "late_funding": True, "by_strategy": {"tsmom": -0.25}})

    series = _series_by_strategy(store, None)

    assert series["tsmom"] == [(BASE + HOUR, -1.25), (BASE + 2 * HOUR, 0.5)], "one point per bar, both rows in it"
    assert series["flow"] == [(BASE + HOUR, 0.25), (BASE + 2 * HOUR, 0.0)]
    drift = income_drift(store, {}, equity=10_000.0, since_ms=None)
    assert drift["by_strategy"]["tsmom"]["pnl"] == pytest.approx(-0.75)
    assert drift["by_strategy"]["tsmom"]["bars"] == 2


def test_late_funding_rows_do_not_move_the_coverage_reading(tmp_path: Path) -> None:
    """O3 reads the main watermark's chain; a late row re-covers the tail of a stretch already in it."""
    store = chained_store(tmp_path, cycles=range(24), income_at={1, 2, 3, 8, 9, 20})
    before = attribution_coverage(store)
    for index in (2, 5, 9):  # 5 sits in a quiet stretch: on the old reading it would even shrink a gap
        read_at = BASE + index * HOUR + SECOND
        store.append_attribution(
            {
                "bar_open_ms": BASE + (index - 1) * HOUR,
                "since_ms": read_at - LAG,
                "until_ms": read_at,
                "late_funding": True,
                "by_strategy": {"tsmom": -0.1},
            }
        )
    assert attribution_coverage(store) == before


def test_a_late_funding_row_does_not_take_a_flattens_commission_key() -> None:
    """The flatten's commission went into the next cycle's OWN row; the late row beside it holds none."""
    bar = BASE + 5 * HOUR
    read_at = bar + 2 * HOUR + 27 * SECOND  # the cycle that wrote both rows below
    attribution = [
        {"at": _iso(read_at - HOUR), "bar_open_ms": bar - HOUR, "by_symbol": {"BTCUSDT": {"COMMISSION": -0.2}}},
        {
            "at": _iso(read_at),
            "bar_open_ms": bar - HOUR,
            "late_funding": True,
            "by_symbol": {"BTCUSDT": {"COMMISSION": 0.0, "FUNDING_FEE": -0.05}},
        },
        {"at": _iso(read_at), "bar_open_ms": bar, "by_symbol": {"BTCUSDT": {"COMMISSION": -0.9}}},
    ]
    flatten = [{"at": _iso(read_at - 30 * 60 * SECOND), "flatten": True, "symbol": "BTCUSDT"}]

    fees, mixed = _booked_fees(attribution, flatten)

    assert mixed == {(bar, "BTCUSDT")}
    assert fees == {(bar - HOUR, "BTCUSDT"): [-0.2], (bar, "BTCUSDT"): [-0.9]}


def test_a_symbol_bar_with_two_rows_is_one_symbol_bar_carrying_both_amounts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    store.append_cycle({"bar_open_ms": BASE, "equity": 10_000.0, "targets": {"BTCUSDT": 0.2, "ETHUSDT": -0.1}})
    store.append_attribution(
        {"bar_open_ms": BASE, "by_symbol": {"BTCUSDT": {"total": -1.0}, "ETHUSDT": {"total": 0.4}}}
    )
    store.append_attribution({"bar_open_ms": BASE, "late_funding": True, "by_symbol": {"BTCUSDT": {"total": -0.3}}})

    legs = leg_split(store, since_ms=None, equity=10_000.0)

    assert legs["pnl"] == pytest.approx({"long": -1.3, "short": 0.4, "flat": 0.0})
    assert legs["symbol_bars"] == {"long": 1, "short": 1, "flat": 0}
