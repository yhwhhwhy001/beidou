"""The pool entry gate at the selection seam: both halves skip the same names and fill the same slots.

Operator ruling 2026-09-30 ("faithful" option): a refresh skips a name the book could not open, and the next
name by volume takes the slot.  Which names those are is the book's business (`beidou_alpha.portfolio.enterable`);
this layer only has to apply the answer the same way in research (`point_in_time_membership`) and live
(`LivePool.select` via `refresh_selection`), causally, and never to a pin.
"""

from __future__ import annotations

import asyncio

import pandas as pd

from beidou_data.pool import LivePool, point_in_time_membership, refresh_selection
from beidou_data.universe import UniverseConfig
from tests.data.test_pool import _FakeDailyClient, _rules

DAYS = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
VOLUME = pd.DataFrame({"A": 500.0, "B": 400.0, "C": 300.0, "D": 200.0, "E": 100.0}, index=DAYS)
CONFIG = UniverseConfig(
    top_n=3, enter_rank=3, exit_rank=3, min_age_days=5, volume_lookback_days=5, always_include=("A",)
)


def _members(membership: pd.DataFrame, day: str) -> list[str]:
    row = membership.loc[pd.Timestamp(day, tz="UTC")]
    return sorted(row[row].index)


def test_research_skips_a_gated_name_and_the_next_by_volume_takes_the_slot() -> None:
    base = point_in_time_membership(VOLUME, CONFIG, refresh="D")
    hourly = pd.date_range(DAYS[0], DAYS[-1], freq="h")
    gate = pd.DataFrame(True, index=hourly, columns=VOLUME.columns)
    gate.loc[pd.Timestamp("2024-01-30", tz="UTC") :, "B"] = False  # the book can no longer open B
    gate.loc[pd.Timestamp("2024-02-10", tz="UTC"), "C"] = False  # exactly at a refresh instant, one bar only
    gate.loc[:, "A"] = False  # a pin is never gated
    gated = point_in_time_membership(VOLUME, CONFIG, refresh="D", enterable=gate)

    assert _members(base, "2024-01-31") == ["A", "B", "C"]
    assert _members(gated, "2024-01-30") == ["A", "B", "C"], "a refresh sees only readings strictly before it"
    assert _members(gated, "2024-01-31") == ["A", "C", "D"], "B is skipped and D, next by volume, takes the slot"
    assert _members(gated, "2024-02-10") == ["A", "C", "D"], "the 02-10T00:00 reading is not before the refresh"
    assert _members(gated, "2024-02-11") == ["A", "C", "D"], "one bar later C is judged enterable again"
    pd.testing.assert_frame_equal(point_in_time_membership(VOLUME, CONFIG, refresh="D", enterable=None), base)


def test_live_and_research_skip_the_same_names_given_the_same_answer() -> None:
    now_ms = 1_700_000_000_000
    ages = dict.fromkeys(VOLUME.columns, 400)
    volumes = {symbol: float(VOLUME[symbol].iloc[0]) * 1e6 for symbol in VOLUME.columns}
    asked: list[list[str]] = []

    async def gate(symbols: list[str]) -> set[str]:
        asked.append(symbols)
        return {"C", "D", "E"}  # the book could open these; A is pinned, B cannot be opened

    pool = LivePool(_FakeDailyClient(ages, volumes, now_ms), CONFIG)
    update = asyncio.run(pool.select(("A", "B", "C"), _rules(list(ages)), gate=gate))
    assert asked == [["A", "B", "C", "D", "E"]], "the gate is asked about every measured candidate, once"
    assert list(update.symbols) == ["A", "C", "D"] and update.left == ("B",) and update.entered == ("D",)
    assert update.gated == ("B",) and update.to_dict()["gated"] == ["B"]

    ungated = asyncio.run(pool.select(("A", "B", "C"), _rules(list(ages))))
    assert list(ungated.symbols) == ["A", "B", "C"] and ungated.gated == ()

    # Research, handed the same answer as a frame, picks the same three names.
    frame = pd.DataFrame(True, index=DAYS, columns=VOLUME.columns)
    frame["B"] = False
    research = point_in_time_membership(VOLUME, CONFIG, refresh="D", enterable=frame)
    assert _members(research, "2024-02-29") == sorted(update.symbols)
    direct = refresh_selection(volumes, _rules(list(ages)), CONFIG, ["A", "B", "C"], 1, enterable={"C", "D", "E"})
    assert direct.symbols == update.symbols
