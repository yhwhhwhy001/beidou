"""D-045: the traps that produced four different answers to one question, each pinned by a test.

Every case here is a mistake this repository actually made on 2026-09-19 while answering "is the live
book's return the market's or the signal's", not a hypothetical.  The numbers in the assertions are
the shapes of those mistakes, so a regression reads as the wrong answer coming back.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from beidou_live.benchmark import (
    MIN_BARS,
    beta_decomposition,
    foreign_bars,
    pit_benchmark,
    series_from_cycles,
    signal_state,
)

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000


def _bars(n: int, start: int = 1_789_000_000_000) -> list[int]:
    return [start + i * HOUR for i in range(n)]


def test_a_symbol_that_leaves_the_universe_still_counts_while_it_was_held() -> None:
    """The fixed-basket bug: it dropped AKEUSDT entirely, and AKEUSDT was up 26.4% while held.

    Here LEAVER doubles over the first half and is then dropped from the universe.  A benchmark that
    kept only the symbols present at both ends would read 0%; the point-in-time one has to carry the
    gain it made while it was in the book.
    """
    bars = _bars(5)
    universe = {bar: (["STAY", "LEAVER"] if i < 3 else ["STAY"]) for i, bar in enumerate(bars)}
    prices = {
        "STAY": dict.fromkeys(bars, 100.0),
        # LEAVER: +20% per bar while held, then it is gone from the universe
        "LEAVER": {bar: 100.0 * (1.2**i) for i, bar in enumerate(bars)},
    }

    benchmark = pit_benchmark(bars, universe, prices)

    assert benchmark["level"][-1] > 1.0, "a symbol that left took its gain with it"
    # Two bars held at equal weight with a flat partner: (1 + 0.20/2)^2
    assert benchmark["level"][-1] == pytest.approx(1.1**2, rel=1e-9)
    assert benchmark["prices_missing"] == 0


def test_a_symbol_entering_the_universe_is_not_a_jump() -> None:
    """A newcomer contributes from its first FULL bar, never from the price it happened to enter at."""
    bars = _bars(4)
    universe = {bars[0]: ["A"], bars[1]: ["A"], bars[2]: ["A", "B"], bars[3]: ["A", "B"]}
    prices = {"A": dict.fromkeys(bars, 50.0), "B": {bars[2]: 10.0, bars[3]: 20.0}}

    level = pit_benchmark(bars, universe, prices)["level"]

    assert level[1] == pytest.approx(1.0) and level[2] == pytest.approx(1.0)
    # B doubles across the last bar and shares the basket with a flat A: +50%
    assert level[3] == pytest.approx(1.5, rel=1e-9)


def test_a_missing_price_is_counted_rather_than_absorbed() -> None:
    """A basket quietly thinning out is the fixed-basket bug wearing a different hat."""
    bars = _bars(3)
    universe = {bar: ["A", "B"] for bar in bars}
    prices = {"A": dict.fromkeys(bars, 10.0), "B": {bars[0]: 10.0}}  # B has no price after bar 0

    benchmark = pit_benchmark(bars, universe, prices)

    assert benchmark["prices_missing"] == 2
    assert benchmark["symbols_per_bar"] == pytest.approx(1.0), "两根 bar 都只剩 A 一个可用"


def test_the_equity_line_is_the_usdt_one_not_total_equity() -> None:
    """A-GB01: collateral repricing moves total equity without the book having traded."""
    bars = _bars(2)
    rows = [
        {"bar_open_ms": bars[0], "collateral": {"usdt_equity": 5_000.0, "equity": 10_000.0}, "targets": {"A": 1.0}},
        {"bar_open_ms": bars[1], "collateral": {"usdt_equity": 5_500.0, "equity": 20_000.0}, "targets": {"A": 1.0}},
    ]

    series = series_from_cycles(rows)

    assert series["equity"] == [5_000.0, 5_500.0], "total equity doubling must not enter the return series"
    # Exposure is rescaled from total equity onto the USDT line: 1.0 x 10,000 / 5,000
    assert series["exposure"][0] == pytest.approx(2.0)


def test_a_restart_writing_the_same_bar_twice_keeps_the_one_that_traded() -> None:
    bars = _bars(2)
    rows = [
        {"bar_open_ms": bars[0], "collateral": {"usdt_equity": 100.0, "equity": 100.0}, "targets": {}},
        {"bar_open_ms": bars[0], "collateral": {"usdt_equity": 111.0, "equity": 111.0}, "targets": {}},
        {"bar_open_ms": bars[1], "collateral": {"usdt_equity": 120.0, "equity": 120.0}, "targets": {}},
    ]

    series = series_from_cycles(rows)

    assert series["bars"] == bars
    assert series["equity"] == [111.0, 120.0]


def test_foreign_fills_are_read_from_the_attribution_rows_not_the_cycles() -> None:
    """The bug this pins: `foreign` is on attribution.jsonl only.

    Reading it off the cycle records returns an empty list with no error, which lets the operator's
    own 2026-09-10 flatten (+268.83 U through one bar) into the strategy's series.
    """
    cycles = [{"bar_open_ms": 1, "foreign": {"total": 268.83}}]
    attribution = [
        {"bar_open_ms": 7, "foreign": {"total": 268.83}},
        {"bar_open_ms": 8, "foreign": {"total": 0.0}},
        {"bar_open_ms": 9, "foreign": {}},
    ]

    assert foreign_bars(attribution) == [7]
    assert foreign_bars(cycles) == [1], "the function itself is agnostic; the caller must pass the right file"


def test_the_signal_section_covers_the_same_bars_as_the_decomposition() -> None:
    """`contributions` predates `collateral.usdt_equity`, so an unbounded read dilutes the share."""
    bars = _bars(4)
    rows = [{"bar_open_ms": bar, "contributions": {"tsmom": {"A": 1.0, "B": 1.0}}} for bar in bars]
    rows[0]["contributions"] = {"tsmom": {"A": 1.0, "B": -1.0}}

    windowed = signal_state(rows, "tsmom", bars[1:])
    unbounded = signal_state(rows, "tsmom")

    assert windowed["bars"] == 3 and windowed["all_long_share"] == pytest.approx(1.0)
    assert unbounded["bars"] == 4 and unbounded["all_long_share"] == pytest.approx(0.75)


def test_an_all_long_stretch_is_named_as_the_constant_long_comparator() -> None:
    """Over these bars no decomposition can attribute anything to the signal, and the report says so."""
    bars = _bars(3)
    rows = [{"bar_open_ms": bar, "contributions": {"tsmom": {"A": 1.0, "B": 1.0, "C": 1.0}}} for bar in bars]

    state = signal_state(rows, "tsmom", bars)

    assert state["all_long_bars"] == 3
    assert state["short_positions"] == 0
    assert state["values"] == {"+1.0": 9}


def test_the_newey_west_t_is_smaller_than_ols_on_autocorrelated_residuals() -> None:
    """OLS put alpha at t=1.83 where NW(48h) put it at 0.61; the first number is an artefact.

    A drifting residual with no market exposure: the market term is pure noise, and the strategy is a
    slow trend.  OLS sees hundreds of independent draws, NW sees a handful of two-day swings.
    """
    n = 300
    bars = _bars(n + 1)
    market = [math.sin(i / 11.0) * 0.004 for i in range(n)]
    drift = [0.0005 + 0.004 * math.sin(i / 40.0) for i in range(n)]  # autocorrelated, mean positive
    strategy_level = [1.0]
    benchmark_level = [1.0]
    for i in range(n):
        strategy_level.append(strategy_level[-1] * math.exp(drift[i]))
        benchmark_level.append(benchmark_level[-1] * math.exp(market[i]))

    result = beta_decomposition(strategy_level, benchmark_level, [1.0] * n, bars=bars)

    assert result["measured"]
    # The point of the test: a positive mean drift with strong autocorrelation must NOT come back as a
    # confident alpha.  Left uncorrected this residual reads as several sigma.
    assert abs(result["constant"]["alpha_t"]) < 2.0


def test_the_annualisation_switch_follows_the_t_in_both_directions() -> None:
    """11 days of noise at t=0.73 compounds to +1766%/yr, which is a decision the data did not make.

    Both directions on purpose: a switch only ever tested on the side it blocks is indistinguishable
    from a field that is always ``None``.
    """
    n = MIN_BARS * 3
    bars = _bars(n + 1)

    def build(mean: float) -> dict:
        rng = random.Random(20260919)
        strategy_level = [1.0]
        benchmark_level = [1.0]
        for _ in range(n):
            strategy_level.append(strategy_level[-1] * math.exp(mean + rng.gauss(0.0, 0.005)))
            benchmark_level.append(benchmark_level[-1] * math.exp(rng.gauss(0.0, 0.005)))
        return beta_decomposition(strategy_level, benchmark_level, [1.0] * n, bars=bars)

    quiet = build(0.00002)
    assert abs(quiet["constant"]["alpha_t"]) < 2.0
    assert quiet["constant"]["alpha_annualised"] is None, "an insignificant alpha must not be annualised"

    loud = build(0.004)
    assert abs(loud["constant"]["alpha_t"]) > 2.0
    assert loud["constant"]["alpha_annualised"] is not None, "a significant one still gets annualised"


def test_a_window_too_short_to_read_refuses_instead_of_returning_a_number() -> None:
    bars = _bars(6)
    result = beta_decomposition([1.0] * 6, [1.0] * 6, [1.0] * 5, bars=bars)

    assert result["measured"] is False
    assert str(MIN_BARS) in result["reason"]


def test_excluded_bars_leave_the_regression_rather_than_being_zeroed() -> None:
    """A bar whose P&L is not the book's must not contribute a data point at all."""
    n = MIN_BARS * 2
    bars = _bars(n + 1)
    strategy_level = [1.0]
    benchmark_level = [1.0]
    for i in range(n):
        # One enormous bar in the middle, the shape of the operator's flatten
        step = 0.30 if i == n // 2 else 0.001
        strategy_level.append(strategy_level[-1] * math.exp(step))
        benchmark_level.append(benchmark_level[-1] * math.exp(0.001))

    with_it = beta_decomposition(strategy_level, benchmark_level, [1.0] * n, bars=bars)
    without = beta_decomposition(
        strategy_level, benchmark_level, [1.0] * n, excluded_bars=[bars[n // 2 + 1]], bars=bars
    )

    assert without["bars"] == with_it["bars"] - 1
    assert without["strategy_return"] < with_it["strategy_return"]
    assert without["constant"]["alpha_bps_per_hour"] < with_it["constant"]["alpha_bps_per_hour"]


@pytest.mark.skipif(
    not (ROOT / ".beidou" / "live" / "cycles.jsonl").exists(),
    reason="live state is not in this checkout (CI and worktrees)",
)
def test_the_field_names_match_what_the_running_loop_actually_writes() -> None:
    """The fixture trap: invented keys give a dozen tests that agree with each other and with nothing.

    Every assertion above runs on records this file made up.  This one runs on the loop's own output,
    so a rename in `cycle_record` surfaces here instead of in a report that silently reads zeros.
    """
    rows = [
        json.loads(line)
        for line in (ROOT / ".beidou" / "live" / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    series = series_from_cycles(rows)

    assert len(series["bars"]) > MIN_BARS, "the loop has written enough bars to decompose"
    assert all(value > 0 for value in series["equity"]), "usdt_equity is present and positive"
    assert series["prices"], "`closes` is being written"
    assert any(row.get("contributions") for row in rows), "`contributions` is being written"
    state = signal_state(rows, "tsmom", series["bars"])
    assert state["measured"] and state["bars"] > 0
    assert set(state["values"]) <= {"+1.0", "-1.0"}, "tsmom's contributions are still sign-only"
