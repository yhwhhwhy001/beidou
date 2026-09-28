"""The live exit overlay is the backtest's exit overlay: one symbol, one input, bar for bar.

D-012 says the backtest loop and the live loop call the same `exit_step`, and each half is held on its
own: `tests/alpha/test_the_vectorised_exit_engine_is_the_same_machine.py` holds `apply_exits` to
`exit_step` called once per symbol-bar, and the live tests hold a few hand-built `ExitOverlay.apply`
calls to worked answers.  Nothing put the two WRAPPERS on one input, and the wrappers are where the
halves differ: live takes the side from the venue position and runs `_reconcile` before `exit_step`,
rebuilds sigma from the bars it is handed, counts bars in open-time milliseconds instead of rows, and
carries its state from one cycle to the next as a dict.

How the live side is driven, each rule taken from the code it stands in for:

- ``positions``: the side of the weight the overlay emitted on the previous bar, so every order lands.
  The entry price is the close of the bar that side was first entered on and survives same-side
  resizes - `_reconcile`'s own rule (E-047, its docstring in `beidou_live/exits.py`).  Here the stored
  state always points the same way, so `_reconcile` never adopts it (measured on this input: only its
  flat and keep branches run); it is built the venue's way so that a change which starts reading it
  meets the right number.
- ``states``: merged, not replaced - `{**self.state.exit_states, **exit_states}` in `engine.py`.
- ``bars`` / ``bar_open_ms``: the close history through bar t, and bar t's open time, which is what
  the engine passes (`last_closed_bar_open_ms`).  The hours are contiguous, so the live cooldown
  `bar + cooldown_bars * interval_ms` fences exactly the rows the backtest's `t + cooldown_bars` does.
- events: `apply_exits` never records a COOLDOWN, while live records every blocked cycle, so COOLDOWN
  is dropped from the live side first - what `test_noise_scale_does_not_count_a_cooldown_as_an_exit`
  already does for the report.  Each exit is then compared on its bar, rule, entry price, price and
  move in daily sigmas (`_units` says why the last one is there).

Weights are compared with `_bit_for_bit`, not `assert_frame_equal`'s default rtol of 1e-5: an exit is a
threshold comparison, and a drift far inside that tolerance moves a whole segment of the path.  The
parameter sets are two rows of that file's `PARAM_SETS`: the one marked ``# shipped`` (stop 6 / take 6 /
cooldown 24, what `config/live.demo.yaml` runs) and the one marked ``# trailing alone, no cooldown``.

Out of scope, each for its own reason:

- An exit the venue did not execute (D-045).  Live-only on purpose, and held by
  `test_a_failed_exit_does_not_re_enter_the_position.py`; here every order lands.
- A bar with no close.  The halves disagree on purpose, and `beidou_live/staleness.py` tables it.
- The live loop's bounded history.  Handed the whole history, live computes the backtest's sigma; the
  loop's 1,442-bar window starts its EWMA later, which is a question about data, not about the overlay.
- Flipping straight back into the side an exit cooled before its cooldown ends (exit a long, go short,
  go long again inside `cooldown_bars`).  The halves do differ there, found by this test on 2026-09-28:
  `exit_step`'s sign-flip branch re-enters without consulting the cooldown, and on the next cycle
  `_reconcile`'s D-045 clause (`held == cooldown_direction` inside the window) reads the position the
  overlay itself just opened as an exit that failed to land - live emits 0 with COOLDOWN where the
  backtest keeps holding.  Every side run in the input outlasts the shipped cooldown, and the test
  asserts that no weight lands on a cooled side inside its window, so the exclusion cannot widen
  silently.  The difference itself is pinned by the strict xfail at the bottom of this file and
  recorded in `docs/RESEARCH_LOG.md` under `RESEARCH_LOG_SECTION`; the fix is the operator's call.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.overlays.exits import COOLDOWN, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP, ExitParams, apply_exits
from beidou_live.exits import ExitOverlay
from beidou_shared.types import Position
from tests.alpha.test_causality import _bit_for_bit
from tests.alpha.test_the_vectorised_exit_engine_is_the_same_machine import PARAM_SETS

HOUR = 3_600_000
SYMBOL = "BTCUSDT"
ROOT = Path(__file__).resolve().parents[2]
# Where the flip-back difference is recorded.  RESEARCH_LOG sections are cited by title, never by number.
RESEARCH_LOG_SECTION = (
    "2026-09-28 · WP-C8：实盘 exit overlay 在 cooldown 内『反向→翻回』会被 D-045 分支当成没成交的退出"
    "——潜伏、0 次，修法待操作者裁定"
)

# (bars, decision weight, drift per bar).  The drift legs are what make the shipped thresholds reachable:
# six daily sigmas is about 29 hourly ones (6 x sqrt(24)), a distance a driftless walk needs on the order
# of 29^2 ~ 850 bars to cover, not 30.  Every side run lasts at least 30 bars, longer than the shipped
# 24-bar cooldown (the last scope item in the module docstring).
SCRIPT = [
    (60, 0.0, 0.0),  # warm-up: `daily_vol` has no sigma before 48 returns
    (30, 0.10, 0.0),  # enter long from flat
    (30, 0.25, 0.0),  # add
    (30, 0.15, 0.0),  # reduce
    (30, -0.15, 0.0),  # flip straight to short
    (30, -0.30, 0.0),  # add to the short
    (60, -0.30, 0.025),  # a rally against it: stop, the cooldown blocks the same side, then re-entry
    (30, 0.0, 0.0),  # the model goes flat
    (30, 0.20, 0.0),  # enter long
    (30, 0.20, 0.025),  # a rally with it: take-profit, then the cooldown blocks the same side
    (30, -0.10, 0.0),  # the other side enters at once, inside the long's cooldown
    (40, 0.10, 0.0),  # flip back to long, past the cooldown
    (30, 0.0, 0.0),  # the model closes it
]


def _input(seed: int = 7) -> tuple[pd.Series, pd.Series]:
    """The fixed-seed random walk of the vectorised-engine test, one column of it, plus the script's drift."""
    rng = np.random.default_rng(seed)
    decisions = np.concatenate([np.full(bars, weight) for bars, weight, _ in SCRIPT])
    drift = np.concatenate([np.full(bars, step) for bars, _, step in SCRIPT])
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, size=len(drift)) + drift))
    index = pd.date_range("2024-01-01", periods=len(close), freq="h", tz="UTC")
    return pd.Series(close, index=index, name=SYMBOL), pd.Series(decisions, index=index, name=SYMBOL)


def _live(close: pd.Series, decisions: pd.Series, params: ExitParams) -> tuple[pd.Series, list[dict[str, Any]]]:
    """`ExitOverlay.apply` once per bar, fed the way the engine feeds it (module docstring)."""
    overlay = ExitOverlay(params, interval_ms=HOUR)
    bars = close.to_frame("close")
    states: dict[str, dict[str, Any]] = {}
    weights: list[float] = []
    events: list[dict[str, Any]] = []
    held, entry = 0.0, math.nan  # the weight emitted last bar, and the close its side was first entered at
    for t, opened in enumerate(close.index):
        side = float(np.sign(held))
        price = float(close.iloc[t])
        positions = {} if side == 0.0 else {SYMBOL: Position(SYMBOL, qty=side, entry_price=entry, mark_price=price)}
        adjusted, new_states, fired = overlay.apply(
            {SYMBOL: float(decisions.iloc[t])},
            positions=positions,
            bars={SYMBOL: bars.iloc[: t + 1]},
            states=states,
            bar_open_ms=int(opened.timestamp() * 1000),
        )
        states = {**states, **new_states}
        events.extend({**event, "bar": t, "held": side} for event in fired)
        held = adjusted[SYMBOL]
        if np.sign(held) != side:
            entry = price if held != 0.0 else math.nan
        weights.append(held)
    return pd.Series(weights, index=close.index, name=SYMBOL), events


def _units(event: dict[str, Any], params: ExitParams) -> float:
    """The move at the exit in daily sigmas, from a live event, in `_run_stepwise`'s order of operations.

    The sigma a position is measured in reaches the weights only when it moves an exit across a bar, so the
    backtest's `units` column is rebuilt from the unit the live event reports and compared on its own.  This
    is `_unit_price` under ``unit_mode="entry"``, which both parameter sets here use.
    """
    unit_price = max(event["unit"], params.min_unit) * event["entry_price"]
    return (event["price"] - event["entry_price"]) * event["held"] / unit_price


def _moves(weights: np.ndarray, exits: set[int]) -> Counter[str]:
    """What each bar did to the position, read off the emitted weights: the branches the input reached."""
    moves: Counter[str] = Counter()
    for t in range(1, len(weights)):
        before, after = weights[t - 1], weights[t]
        if before == 0.0 and after != 0.0:
            moves["enter long" if after > 0.0 else "enter short"] += 1
        elif before * after < 0.0:
            moves["flip"] += 1
        elif before * after > 0.0 and abs(after) != abs(before):
            moves["add" if abs(after) > abs(before) else "reduce"] += 1
        elif before != 0.0 and after == 0.0 and t not in exits:
            moves["close"] += 1  # the model went flat; a rule firing is counted by the events
    return moves


@pytest.mark.parametrize(
    "params", [pytest.param(PARAM_SETS[0], id="shipped"), pytest.param(PARAM_SETS[2], id="trailing")]
)
def test_the_live_overlay_emits_the_backtest_weights_and_exits_bar_for_bar(params: ExitParams) -> None:
    close, decisions = _input()
    backtest = apply_exits(decisions.to_frame(), close.to_frame(), params)
    live_weights, live_events = _live(close, decisions, params)

    exit_bars = close.index.get_indexer(backtest.events["time"]).tolist()
    emitted = backtest.weights[SYMBOL].to_numpy()
    for exit_bar, side in zip(exit_bars, backtest.events["direction"], strict=True):
        cooled = emitted[exit_bar + 1 : exit_bar + params.cooldown_bars]
        assert not (np.sign(cooled) == side).any(), f"bar {exit_bar}: the input re-entered a cooled side (scope)"

    _bit_for_bit(live_weights, backtest.weights[SYMBOL])
    exits = [
        (e["bar"], e["rule"], e["entry_price"], e["price"], _units(e, params))
        for e in live_events
        if e["rule"] != COOLDOWN
    ]
    columns = [backtest.events[key].tolist() for key in ("rule", "entry_price", "price", "units")]
    assert exits == list(zip(exit_bars, *columns, strict=True))

    # Two idle overlays agree too, so the input has to show it reached the machinery: every rule the set
    # enables fired, and every kind of move `SCRIPT` sets up happened at least once.
    counts = Counter(e["rule"] for e in live_events)
    enabled = {
        STOP_LOSS: params.stop_loss,
        TRAILING_STOP: params.trailing_stop,
        TAKE_PROFIT: params.take_profit,
        COOLDOWN: params.cooldown_bars,
    }
    assert all(counts[rule] > 0 for rule, setting in enabled.items() if setting > 0), counts
    moves = _moves(emitted, set(exit_bars))
    assert set(moves) == {"enter long", "enter short", "add", "reduce", "flip", "close"}, moves


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "live `_reconcile`'s D-045 clause takes a position the overlay re-opened by a sign flip inside the "
        f"cooldown for an exit that failed to land, and goes flat where the backtest holds: 「{RESEARCH_LOG_SECTION}」"
    ),
)
def test_a_flip_back_into_the_cooled_side_is_held_live_as_it_is_in_the_backtest() -> None:
    """The excluded sequence, pinned as it behaves today: a strict xfail, so a fix on either side turns it red.

    Shipped parameters.  Sixty calm bars give sigma a value; then a long is entered at 100, a crash to 70
    stops it at bar 61 (cooldown on the long side until bar 85), the model goes short at 62 and back to
    long at 63.  Both halves hold the long at 63; from 64 live emits 0 and the backtest keeps holding.

    Only the final comparison may fail as expected, hence ``raises=AssertionError``.  The two checks before
    it use `pytest.fail`, which is not an AssertionError, so a missing log section or an input that stopped
    reaching the sequence fails outright instead of being counted as the known difference.  When the
    operator's fix lands this XPASSes: delete it, drop the exclusion from the test above and add the flip
    back to `SCRIPT`.  The log section lists the same steps, plus a new section to record the fix.
    """
    if f"## {RESEARCH_LOG_SECTION}\n" not in (ROOT / "docs" / "RESEARCH_LOG.md").read_text(encoding="utf-8"):
        pytest.fail("the RESEARCH_LOG section this xfail points at is gone")
    params = PARAM_SETS[0]
    closes = [100.0 * (1 + 0.004 * (-1) ** i) for i in range(60)] + [100.0, 70.0, 70.0, 70.0, 70.0, 70.0, 70.0]
    index = pd.date_range("2024-01-01", periods=len(closes), freq="h", tz="UTC")
    close = pd.Series(closes, index=index, name=SYMBOL)
    decisions = pd.Series([0.0] * 60 + [0.1, 0.1, -0.1, 0.1, 0.1, 0.1, 0.1], index=index, name=SYMBOL)
    backtest = apply_exits(decisions.to_frame(), close.to_frame(), params)
    live_weights, live_events = _live(close, decisions, params)

    stops = (
        [e["bar"] for e in live_events if e["rule"] == STOP_LOSS],
        close.index.get_indexer(backtest.events["time"][backtest.events["rule"] == STOP_LOSS]).tolist(),
    )
    flips = [tuple(side.iloc[62:64]) for side in (live_weights, backtest.weights[SYMBOL])]
    if stops != ([61], [61]) or flips != [(-0.1, 0.1)] * 2:
        pytest.fail(f"the input no longer reaches the flip back inside the cooldown: stops {stops}, flips {flips}")
    _bit_for_bit(live_weights.iloc[60:], backtest.weights[SYMBOL].iloc[60:])
