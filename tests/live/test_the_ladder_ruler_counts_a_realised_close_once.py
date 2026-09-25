"""R8's ruler counted a close twice.  Each income now lands on the row of the cycle that read it.

`run_cycle` takes the venue snapshot - row k's `unrealized` - BEFORE cycle k's orders, and the income
those orders realise is read by the NEXT cycle, which `_ingest_income` stamps with `state.last_bar_ms`:
bar k.  Added at row k, the close sat in that row's mark as open P&L and in the income as realised P&L,
and a winning close left its spike in the running peak.  W5 measured it on the live record on
2026-09-25: 331 of 364 readings moved, 0.27pp deeper on average and 1.58pp deeper at most.

The rows copy the live ledgers' field names and cadence: a cycle appends its row ~31 s after its bar
closes, and reads the previous cycle's income ~5 s before that.  None of the accounts below carries
collateral, which gives every test the same yardstick: with nothing to reprice, the book's own
mark-to-market IS the equity, so the ruler's marks must be the equity column, row for row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from beidou_live.risk_budget import RiskBudgetParams, attributed_drawdown_state

BAR = 3_600_000
T13 = 1_789_995_600_000  # 2026-09-21T13:00Z, the bar of the first cycle below
PARAMS = RiskBudgetParams()


def _at(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


def _bar(index: int) -> int:
    return T13 + index * BAR


def _priced(
    index: int, equity: float, unrealized: float, *, trades: str | None = None, after_s: int = 31, transfer: float = 0.0
) -> dict[str, Any]:
    """A row as `_finish_cycle` appends it: bar `index` closed, snapshot taken, orders (if any) placed."""
    bar = _bar(index)
    return {
        "at": _at(bar + BAR + after_s * 1000),
        "bar": _at(bar),
        "bar_open_ms": bar,
        "dry_run": False,
        "equity": equity,
        "external_flows": {
            "by_type": {"TRANSFER": transfer} if transfer else {},
            "rebaselined": bool(transfer),
            "rows": 2 if transfer else 0,
            "total": transfer,
        },
        "orders": []
        if trades is None
        else [{"client_order_id": f"bd-{bar}-{trades}", "side": "SELL", "status": "FILLED", "symbol": trades}],
        "skip": False,
        "unrealized": unrealized,
    }


def _skipped(index: int, *, after_s: int) -> dict[str, Any]:
    """What a restart writes for a bar the loop already traded (`_record_missed_rebalance`)."""
    bar = _bar(index)
    return {
        "at": _at(bar + BAR + after_s * 1000),
        "bar": _at(bar),
        "bar_open_ms": bar,
        "dry_run": False,
        "late_seconds": float(after_s),
        "missed_rebalances": 0,
        "orders": [],
        "phase": "SKIPPED",
        "reason": "restart outside the rebalance window; this bar was already rebalanced",
        "window_seconds": 73.509,
    }


def _error(index: int, *, after_s: int = 62) -> dict[str, Any]:
    """`guarded_cycle`'s row for a cycle that raised - after `_ingest_income`, so its income was read."""
    bar = _bar(index)
    return {
        "at": _at(bar + BAR + after_s * 1000),
        "bar": _at(bar),
        "bar_open_ms": bar,
        "consecutive_errors": 1,
        "dry_run": False,
        "error": "ProxyError: 503 Service Unavailable",
        "leaving": [],
        "orders": [],
        "phase": "ERROR",
        "quarantined": [],
        "risk_ladder": {},
        "universe_update": None,
        "window_seconds": 86.184,
    }


def _income(
    stamped: int, read_by: int, *, realised: float = 0.0, fee: float = 0.0, funding: float = 0.0, after_s: int = 26
) -> dict[str, Any]:
    """`_ingest_income`'s row: read by the cycle of bar `read_by`, stamped `stamped` (`state.last_bar_ms`)."""
    total = realised + fee + funding
    read_at = _bar(read_by) + BAR + after_s * 1000
    return {
        "at": _at(read_at),
        "bar_open_ms": _bar(stamped),
        "basis": "net_exposure",
        "by_strategy": {"tsmom": total},
        "by_symbol": {
            "1000PEPEUSDT": {
                "COMMISSION": fee,
                "FUNDING_FEE": funding,
                "INSURANCE_CLEAR": 0.0,
                "REALIZED_PNL": realised,
                "total": total,
            }
        },
        "cancelled": {},
        "external_flows": {"by_type": {}, "rows": 0, "total": 0.0},
        "foreign": {"by_symbol": {}, "reconciled": True, "rows": 0, "total": 0},
        "since_ms": read_at - BAR,
        "total": total,
        "unattributed": 0.0,
        "until_ms": read_at,
    }


def _foreign_only(stamped: int, read_by: int) -> dict[str, Any]:
    """A row whose every fill someone else placed: the book's `total` is 0 (live 2026-09-14T17:00:47Z)."""
    fill = {"COMMISSION": -0.00561295, "FUNDING_FEE": 0.0, "INSURANCE_CLEAR": 0.0, "REALIZED_PNL": -0.18395247}
    return _income(stamped, read_by) | {
        "by_strategy": {},
        "by_symbol": {},
        "foreign": {
            "by_symbol": {"ENAUSDT": fill | {"total": -0.18956542}},
            "reconciled": True,
            "rows": 2,
            "total": -0.18956542,
        },
        "total": 0,
    }


def _marks_are_the_equity(reading: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    equity = [float(row["equity"]) for row in rows if "equity" in row]
    assert reading["peak"] == pytest.approx(max(equity), abs=1e-9)
    assert reading["value"] == pytest.approx(equity[-1] / max(equity) - 1.0, abs=1e-12)


# --- the defect: one winning close ---------------------------------------------------------------


def _winning_close() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cycle 13:00 opens a long (fee 2); marked +300 at 14:00, that cycle closes it (+300, fee 3)."""
    rows = [
        _priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"),
        _priced(1, 10_298.0, 300.0, trades="1000PEPEUSDT"),
        _priced(2, 10_295.0, 0.0),
    ]
    return rows, [_income(0, 1, fee=-2.0), _income(1, 2, realised=300.0, fee=-3.0)]


def test_a_winning_close_does_not_lift_the_high_water_mark() -> None:
    """Stamped on bar 14:00, the +297 used to join that row's +300 mark: peak 10_595, reading -2.83%."""
    rows, income = _winning_close()
    reading = attributed_drawdown_state(rows, income, PARAMS)

    assert reading["peak"] == pytest.approx(10_298.0, abs=1e-9), "the mark with the long still open"
    assert reading["value"] == pytest.approx(10_295.0 / 10_298.0 - 1.0, abs=1e-12), "only the close's fee"
    assert reading["path"] == pytest.approx(10_295.0, abs=1e-9) and reading["rows"] == 2
    _marks_are_the_equity(reading, rows)


def test_the_loop_reads_the_close_one_snapshot_back_not_twice() -> None:
    """The ladder runs before its cycle writes its row, so the income that cycle just read is pending.

    Rows through 14:00 and both incomes is what the 15:00 cycle's ladder sees: the close is in the 14:00
    mark as open P&L, and nowhere else yet.
    """
    rows, income = _winning_close()
    reading = attributed_drawdown_state(rows[:2], income, PARAMS)

    assert reading["enforced"] and reading["value"] == 0.0
    assert reading["peak"] == pytest.approx(10_298.0, abs=1e-9)
    assert reading["pending_rows"] == 1 and reading["pending_pnl"] == pytest.approx(297.0)
    assert reading["rows"] == 1 and reading["in_base_rows"] == 0


def test_the_cycle_that_reads_a_losing_close_sees_it_once_as_if_it_were_still_held() -> None:
    """2026-09-13's "a flatten no longer moves R8" held only on a fixture that stamped the loss one bar late.

    `scratchpad/r8_is_realisation_invariant.py` stamped the booked loss with the bar whose row already
    shows the position gone.  `_ingest_income` stamps the bar that placed the close, whose row still
    marks it: there the old ruler read -8% for a book down 4%, on the reading the ladder acts on.
    """
    held = [_priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"), _priced(1, 9_598.0, -400.0)]
    flattened = [held[0], _priced(1, 9_598.0, -400.0, trades="1000PEPEUSDT")]
    opening = _income(0, 1, fee=-2.0)

    kept = attributed_drawdown_state(held, [opening], PARAMS)
    closed = attributed_drawdown_state(flattened, [opening, _income(1, 2, realised=-400.0, fee=-3.0)], PARAMS)
    assert closed["value"] == kept["value"] == pytest.approx(9_598.0 / 10_000.0 - 1.0, abs=1e-12)
    assert closed["pending_rows"] == 1

    after = attributed_drawdown_state(
        [*flattened, _priced(2, 9_595.0, 0.0)], [opening, _income(1, 2, realised=-400.0, fee=-3.0)], PARAMS
    )
    assert after["max_drawdown"] == pytest.approx(9_595.0 / 10_000.0 - 1.0, abs=1e-12), "never -8%"


def test_a_book_that_closes_nothing_reads_bit_for_bit_what_it_read_before() -> None:
    """The control: the book holds, marks move, and the only income rows are someone else's fills.

    Nothing the book realised can land anywhere, so where income lands cannot move this path.  The
    expected values are the arithmetic the ruler performed before the change, written out: exact
    equality, because "the same" is the claim.  This test passes on 54da72a8, the pre-change code, too.
    """
    rows = [
        _priced(0, 10_000.0, 0.0),
        _priced(1, 10_150.0, 150.0),
        _priced(2, 10_400.0, 400.0),
        _priced(3, 10_250.0, 250.0),
    ]
    reading = attributed_drawdown_state(rows, [_foreign_only(0, 1), _foreign_only(2, 3)], PARAMS)

    assert reading["enforced"] and reading["ruler"] == "attributed_pnl+unrealized" and reading["marked_rows"] == 4
    assert reading["peak"] == 10_000.0 + (400.0 - 0.0)
    assert reading["value"] == (10_000.0 + (250.0 - 0.0)) / (10_000.0 + (400.0 - 0.0)) - 1.0
    assert reading["max_drawdown"] == reading["value"]
    assert reading["path"] == 10_000.0 and reading["attributed"] == 0.0
    assert reading["equity_over_peak"] == 10_250.0 / (10_000.0 + (400.0 - 0.0))
    assert reading["bars"] == 4 and reading["base"] == 10_000.0


# --- the three edge cases: unpriced rows, one bar written twice, re-baselining ---------------------


def test_skipped_and_error_rows_never_read_an_income_onto_the_path() -> None:
    """A restart's SKIPPED row, then a cycle that read the close and raised: the next priced row takes both.

    The ERROR cycle never reached `_finish_cycle`, so `state.last_bar_ms` was still 14:00 when the 16:00
    cycle read the hour's funding - both rows carry the 14:00 stamp.  The old ruler added both at 14:00,
    whose mark still held the long.
    """
    rows = [
        _priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"),
        _priced(1, 10_298.0, 300.0, trades="1000PEPEUSDT"),
        _skipped(1, after_s=600),
        _error(2),
        _priced(3, 10_294.0, 0.0),
    ]
    income = [_income(0, 1, fee=-2.0), _income(1, 2, realised=300.0, fee=-3.0), _income(1, 3, funding=-1.0)]
    reading = attributed_drawdown_state(rows, income, PARAMS)

    assert reading["rows"] == 3 and reading["pending_rows"] == 0
    assert reading["bars"] == 3, "the SKIPPED and ERROR rows are not bars of this path"
    _marks_are_the_equity(reading, rows)


def test_the_order_income_rows_are_handed_over_in_does_not_move_the_reading() -> None:
    """The per-bar dict this replaced did not care about file order; a caller that filters or joins must not."""
    rows = [
        _priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"),
        _priced(1, 10_298.0, 300.0, trades="1000PEPEUSDT"),
        _error(2),
        _priced(3, 10_294.0, 0.0),
    ]
    income = [_income(0, 1, fee=-2.0), _income(1, 2, realised=300.0, fee=-3.0), _income(1, 3, funding=-1.0)]
    reading = attributed_drawdown_state(rows, income, PARAMS)

    assert attributed_drawdown_state(rows, income[::-1], PARAMS) == reading
    _marks_are_the_equity(reading, rows)


def test_a_rerun_of_the_stamped_bar_reads_the_income_only_if_written_after_it() -> None:
    """Restarts re-ran bars until 2026-09-06: 16 bars of the live record have two to five priced rows.

    Income a re-run read carries the bar it re-ran, like its first run.  The re-run at 15:20:15 read the
    close at 15:20:10 and is its reader.  Written in the same second, the two cannot be ordered and the
    income waits for the next bar: the stamped bar's first row can never be the reader, and a tie read
    the other way would put it back on that row (the next test).
    """
    opened, closed = _priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"), _priced(1, 10_298.0, 300.0)
    rerun = _priced(1, 10_295.0, 0.0, after_s=20 * 60 + 15)
    later = _priced(2, 10_295.0, 0.0)
    close = _income(1, 1, realised=300.0, fee=-3.0, after_s=20 * 60 + 10)
    rows = [opened, closed, rerun, later]
    reading = attributed_drawdown_state(rows, [_income(0, 1, fee=-2.0), close], PARAMS)
    _marks_are_the_equity(reading, rows)
    assert reading["max_drawdown"] == pytest.approx(10_295.0 / 10_298.0 - 1.0, abs=1e-12)

    tied = close | {"at": rerun["at"]}
    reading = attributed_drawdown_state(rows, [_income(0, 1, fee=-2.0), tied], PARAMS)
    assert reading["value"] == pytest.approx(10_295.0 / 10_298.0 - 1.0, abs=1e-12), "landed on the 15:00 row"
    assert reading["max_drawdown"] == pytest.approx(9_998.0 / 10_298.0 - 1.0, abs=1e-12), "absent at the re-run"


def test_income_written_in_the_second_its_stamped_row_was_is_still_not_read_by_that_row() -> None:
    """What an engine test's clock produces: the row and the next cycle's income share one `at` second."""
    rows, income = _winning_close()
    income[1]["at"] = rows[1]["at"]
    reading = attributed_drawdown_state(rows, income, PARAMS)

    assert reading["peak"] == pytest.approx(10_298.0, abs=1e-9)
    _marks_are_the_equity(reading, rows)


def test_a_rebaselining_cycle_keeps_what_it_read_inside_its_base() -> None:
    """A demo top-up arrives with the close; the next cycle's fee arrives after the new base.

    The 15:00 cycle read the close and 5,000.01 of TRANSFER in one window, so its equity - the new base
    - already holds the +297; adding it again would lift the fresh peak by 297.  The fee for the long
    that cycle opened was paid after its snapshot and lands on the 16:00 row, not on the baseline.
    """
    rows = [
        _priced(0, 10_000.0, 0.0, trades="1000PEPEUSDT"),
        _priced(1, 10_298.0, 300.0, trades="1000PEPEUSDT"),
        _priced(2, 15_295.01, 0.0, trades="ADAUSDT", transfer=5_000.01),
        _priced(3, 15_413.51, 120.0),
    ]
    income = [_income(0, 1, fee=-2.0), _income(1, 2, realised=300.0, fee=-3.0), _income(2, 3, fee=-1.5)]

    at_reset = attributed_drawdown_state(rows[:3], income[:2], PARAMS)
    assert at_reset["base"] == at_reset["peak"] == at_reset["path"] == 15_295.01 and at_reset["value"] == 0.0
    assert at_reset["in_base_rows"] == 1 and at_reset["baseline_at"] == rows[2]["at"]

    reading = attributed_drawdown_state(rows, income, PARAMS)
    assert reading["path"] == pytest.approx(15_293.51, abs=1e-9)
    assert reading["rows"] == 2 and reading["in_base_rows"] == 1 and reading["pending_rows"] == 0
    _marks_are_the_equity(reading, rows[2:])


def test_the_first_recorded_row_is_a_baseline_too() -> None:
    """Income the first recorded cycle read is inside the equity that becomes the base."""
    rows = [_priced(1, 10_298.0, 300.0, trades="1000PEPEUSDT"), _priced(2, 10_295.0, 0.0)]
    income = [_income(0, 1, fee=-2.0), _income(1, 2, realised=300.0, fee=-3.0)]
    reading = attributed_drawdown_state(rows, income, PARAMS)

    assert reading["in_base_rows"] == 1 and reading["rows"] == 1
    _marks_are_the_equity(reading, rows)
