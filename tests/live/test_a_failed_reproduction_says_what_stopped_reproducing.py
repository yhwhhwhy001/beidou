"""KILL-027's monitor fired six times in 48 hours and left no recoverable evidence any of them.

`run_check.sh` pages with `"$(echo "$output" | tail -n 3 | tr '\\n' ' ')"`, and this command's output is
pretty-printed JSON sorted by key.  Its last three lines are `"tolerance": 1e-09`, `}` and the error,
so every one of the six alerts between 2026-09-10T18:10Z and 2026-09-12T09:10Z read:

    FAIL verify:   "tolerance": 1e-09 } Error: 模型已无法复现上一周期的 contributions（...）

By the time anyone looked, `state.json` had been overwritten by the next cycle and the diff was gone.
The cause was only found by replaying 56 cycles offline against freshly fetched klines, which
reproduced all six exactly - every one a single symbol in the `flow` book, 3.3e-8 to 8.5e-7 against a
1e-9 tolerance, on the four names whose `taker_buy_quote / quote_volume` the flow signal reads.

Two things follow, and this file holds the first: the payload must name the offenders, in the note
itself, so the one line that survives the pager says which strategy and which symbol moved.
"""

from __future__ import annotations

import pandas as pd

from beidou_alpha.ensemble import TargetWeights
from beidou_live.state import LiveState
from beidou_live.verify import compare_targets

BAR = pd.Timestamp("2026-09-12T09:00:00Z")


def _pair(recorded: dict, computed: dict) -> dict:
    state = LiveState(
        last_bar_ms=int(BAR.timestamp() * 1000), last_contributions=recorded, last_targets={"BTCUSDT": 0.03}
    )
    targets = TargetWeights(as_of=BAR, weights={"BTCUSDT": 0.03}, contributions=computed, combined={})
    return compare_targets(targets, state)


def test_the_note_names_the_strategy_and_the_symbol_that_moved() -> None:
    """The real 2026-09-12T09:00 shape: one flow symbol, 3.3e-8, everything else bit-identical."""
    recorded = {"tsmom": {"BTCUSDT": 1.0, "TUTUSDT": -1.0}, "flow": {"TUTUSDT": -0.2057259135942554}}
    computed = {"tsmom": {"BTCUSDT": 1.0, "TUTUSDT": -1.0}, "flow": {"TUTUSDT": -0.2057259465942554}}
    result = _pair(recorded, computed)
    assert result["ok"] is False and result["bar_matched"] is True
    assert "flow/TUTUSDT" in result["note"], "the line that survives `tail -n 3` must carry the offender"
    assert result["worst_contributions"][0]["strategy"] == "flow"
    assert result["worst_contributions"][0]["symbol"] == "TUTUSDT"
    assert result["worst_contributions"][0]["diff"] < 1e-6


def test_the_offenders_are_ranked_worst_first_and_capped() -> None:
    """A config change moves everything; the operator needs the top of the list, not all of it."""
    recorded = {"tsmom": {f"S{i}USDT": 1.0 for i in range(10)}}
    computed = {"tsmom": {f"S{i}USDT": 1.0 + i / 100.0 for i in range(10)}}
    result = _pair(recorded, computed)
    diffs = [row["diff"] for row in result["worst_contributions"]]
    assert len(diffs) == 5, "capped so the alert line stays readable"
    assert diffs == sorted(diffs, reverse=True)
    assert result["worst_contributions"][0]["symbol"] == "S9USDT"


def test_a_faithful_reproduction_names_nobody() -> None:
    same = {"tsmom": {"BTCUSDT": 1.0}, "flow": {"TUTUSDT": -0.2}}
    result = _pair(same, same)
    assert result["ok"] is True and result["worst_contributions"] == []
