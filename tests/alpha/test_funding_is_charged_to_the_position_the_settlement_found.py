"""A settlement is paid by the position held INTO its bar, before that bar's rebalance (backtest-guard 2026-09-25).

`align_funding_to_bars` floors a settlement into the bar that contains it, so the 08:00 settlement is in bar
08:00, whose open is the settlement.  The live loop trades about 27 s after the close that decided it, so the
position the settlement finds is the one it held into 08:00 - the executed row BEFORE.  Until 2026-09-30 the
backtest charged it to the row AT 08:00, the position the loop only takes after paying.  One flip is enough
to see the difference: long into the settlement bar, short out of it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from beidou_alpha.backtest import CostModel, run_backtest
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_alpha.panel import Panel

INDEX = pd.date_range("2026-09-01 00:00", periods=6, freq="h", tz="UTC")
RATE = 0.001  # longs pay
SETTLEMENT_BAR = INDEX[2]


def _panel() -> Panel:
    flat = pd.DataFrame(100.0, index=INDEX, columns=["X"])  # no price move: net is minus costs
    funding = pd.DataFrame(0.0, index=INDEX, columns=["X"])
    funding.loc[SETTLEMENT_BAR, "X"] = RATE
    return Panel(interval="1h", open=flat, high=flat, low=flat, close=flat, volume=flat, funding=funding)


def _decided() -> pd.DataFrame:
    """Decided on bar 0 long, from bar 1 on short: executed long on bar 1, short from bar 2 - the settlement bar."""
    return pd.DataFrame({"X": [0.1, -0.1, -0.1, -0.1, -0.1, -0.1]}, index=INDEX)


def test_the_long_held_into_the_settlement_pays_it() -> None:
    result = run_backtest(_panel(), _decided(), CostModel(turnover_bps=0.0, use_funding=True))
    assert result.weights.loc[INDEX[1], "X"] == 0.1 and result.weights.loc[SETTLEMENT_BAR, "X"] == -0.1
    assert result.net.loc[SETTLEMENT_BAR, "X"] == pytest.approx(-0.1 * RATE, abs=1e-15), (
        "the short taken after the settlement was credited with it"
    )
    assert result.net.drop(SETTLEMENT_BAR)["X"].abs().max() == 0.0


def test_the_guard_replay_prices_its_equity_path_the_same_way() -> None:
    """The replay's daily-loss pause reads the equity it produces, so it must pay the settlement on the same row.

    A long of 1.0 into a 1% settlement against a -50 bps pause: paid by the long, equity falls 100 bps and the
    next bar pauses.  Credited to the short, as before 2026-09-30, equity rises and nothing pauses.
    """
    panel = _panel()
    panel.funding.loc[SETTLEMENT_BAR, "X"] = 0.01
    decided = _decided() * 10.0  # +-1.0 of equity
    guards = BookGuardParams(max_weight=1.0, max_gross=2.0, daily_loss_pause=-0.005)
    result = run_backtest(panel, decided, CostModel(turnover_bps=0.0, use_funding=True), guards=guards)
    assert result.guard_events is not None
    paused = result.guard_events["daily_loss_pause"]
    assert not paused.loc[:SETTLEMENT_BAR].any()
    assert paused.loc[INDEX[3]], "the settlement the long paid never reached the replay's equity"
