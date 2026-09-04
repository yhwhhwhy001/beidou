"""M1: the two book-level guards, replayed on the backtest's own equity path.

The test that matters is the first one.  At the shipped vol target neither guard is reachable - 5.6
years of the live book produced a worst UTC day of -3.5% against a -5% pause, and gross never crossed
2.0 - so if the replay is not provably inert there, every number this project has published would move
for no reason.  The rest check that it stops being inert exactly where it should.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.backtest import CostModel, run_backtest
from beidou_alpha.overlays.exposure import BookGuardParams, clamp_book, hold_or_reduce
from beidou_alpha.panel import Panel

BARS = 240
SYMBOLS = ["AAAUSDT", "BBBUSDT"]


def _panel(returns: np.ndarray) -> Panel:
    index = pd.date_range("2024-01-01", periods=BARS, freq="1h", tz="UTC")
    close = pd.DataFrame(100.0 * np.cumprod(1.0 + returns, axis=0), index=index, columns=SYMBOLS)
    frames = {
        symbol: pd.DataFrame(
            {
                "open": close[symbol].shift(1).fillna(100.0),
                "high": close[symbol],
                "low": close[symbol],
                "close": close[symbol],
                "volume": 1.0,
                "quote_volume": 1.0,
            },
            index=index,
        )
        for symbol in SYMBOLS
    }
    return Panel.from_frames(frames, interval="1h")


def test_replay_is_bit_for_bit_inert_when_neither_guard_binds() -> None:
    rng = np.random.default_rng(7)
    panel = _panel(rng.normal(0.0, 0.002, size=(BARS, len(SYMBOLS))))
    weights = pd.DataFrame(0.05, index=panel.close.index, columns=SYMBOLS)
    cost = CostModel(turnover_bps=7.0)
    off = run_backtest(panel, weights, cost)
    on = run_backtest(panel, weights, cost, guards=BookGuardParams())
    pd.testing.assert_frame_equal(off.weights, on.weights)
    pd.testing.assert_series_equal(off.portfolio_net, on.portfolio_net)
    assert on.summary()["guards"] == {
        "replayed": True,
        "gross_capped_bars": 0,
        "daily_loss_pause_bars": 0,
        "bars": len(on.weights),
    }
    assert off.summary().get("guards") is None


def test_a_losing_day_stops_the_book_adding_risk_for_the_rest_of_that_utc_day() -> None:
    returns = np.zeros((BARS, len(SYMBOLS)))
    returns[30:36, :] = -0.02  # a -11.4% stretch inside the second UTC day
    panel = _panel(returns)
    weights = pd.DataFrame(0.5, index=panel.close.index, columns=SYMBOLS)
    # caps wide enough to leave the 0.5 weights alone, so the pause is the only guard under test
    guards = BookGuardParams(max_weight=0.5, max_gross=2.0)
    result = run_backtest(panel, weights, CostModel(turnover_bps=0.0), guards=guards)
    events = result.guard_events
    assert events is not None
    paused = events["daily_loss_pause"]
    assert paused.any(), "the pause never fired on a -11% day"
    fired = paused[paused].index
    # The day is the DECISION bar's, not the execution bar's - the live loop rolls `day_start_equity`
    # in `_roll_day(bar_open_ms, ...)`, i.e. on the bar the weights were decided on.  So the last paused
    # execution bar is 00:00 of the next day, whose decision bar is 23:00 of the losing one.  Asserting
    # on execution timestamps here would demand the off-by-one-bar bug instead of catching it.
    decisions = fired - pd.Timedelta(hours=1)
    assert decisions.normalize().nunique() == 1
    assert fired.normalize().nunique() == 2
    held = result.weights.loc[fired]
    assert (held.abs() <= 0.5 + 1e-12).all().all()


def test_the_gross_cap_binds_and_is_recorded() -> None:
    panel = _panel(np.zeros((BARS, len(SYMBOLS))))
    weights = pd.DataFrame(0.14, index=panel.close.index, columns=SYMBOLS)
    result = run_backtest(
        panel, weights, CostModel(turnover_bps=0.0), guards=BookGuardParams(max_weight=0.15, max_gross=0.2)
    )
    assert result.guard_events is not None
    assert result.guard_events["gross_capped"].all()
    assert result.weights.abs().sum(axis=1).max() == pytest.approx(0.2)


def test_clamp_and_hold_or_reduce_are_the_primitives_live_shares() -> None:
    row, capped = clamp_book(np.array([0.3, -0.3]), 0.15, 2.0)
    assert row.tolist() == [0.15, -0.15] and not capped
    row, capped = clamp_book(np.array([0.15, -0.15]), 0.15, 0.2)
    assert capped and abs(row).sum() == pytest.approx(0.2)
    # only moves toward zero: same sign and smaller, or flat
    moved = hold_or_reduce(np.array([0.2, 0.05, -0.1, 0.1]), np.array([0.1, 0.1, 0.2, -0.1]))
    assert moved.tolist() == [0.1, 0.05, 0.0, 0.0]
