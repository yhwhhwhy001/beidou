"""The pool entry gate at the selection seam: both halves skip the same names and fill the same slots.

Operator ruling 2026-09-30 ("faithful" option): a refresh skips a name the book could not open, and the next
name by volume takes the slot.  Which names those are is the book's business (`beidou_alpha.portfolio.enterable`);
this layer only applies the answer - the names the gate BLOCKS - the same way in research
(`point_in_time_membership`) and live (`LivePool.select` via `refresh_selection`): in date order, against the
previous refresh's members, and never to a pin.  A name the gate cannot judge is not blocked.
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


def test_research_asks_the_gate_in_date_order_and_fills_the_slot_it_frees() -> None:
    base = point_in_time_membership(VOLUME, CONFIG, refresh="D")
    asked: list[tuple[pd.Timestamp, list[str]]] = []

    def gate(date: pd.Timestamp, previous: list[str]) -> set[str]:
        asked.append((date, list(previous)))
        blocked = {"A"}  # a pin: never gated, whatever the gate says
        if date >= pd.Timestamp("2024-01-31", tz="UTC"):
            blocked.add("B")  # from here the book can no longer open B
        if date == pd.Timestamp("2024-02-10", tz="UTC"):
            blocked.add("C")  # one refresh only
        return blocked

    gated = point_in_time_membership(VOLUME, CONFIG, refresh="D", gate=gate)
    assert _members(base, "2024-01-31") == ["A", "B", "C"]
    assert _members(gated, "2024-01-30") == ["A", "B", "C"]
    assert _members(gated, "2024-01-31") == ["A", "C", "D"], "B is skipped and D, next by volume, takes the slot"
    assert _members(gated, "2024-02-10") == ["A", "D", "E"], "C blocked for one refresh: E fills"
    assert _members(gated, "2024-02-11") == ["A", "C", "D"], "C is back the next day; E, ranked last, leaves"
    dates = [date for date, _previous in asked]
    assert dates == sorted(dates) and len(dates) == len(gated.index), "once per refresh, in date order"
    assert asked[gated.index.get_loc(pd.Timestamp("2024-02-11", tz="UTC"))][1] == ["A", "D", "E"], (
        "the gate is told the members the previous refresh chose - what the book holds when it decides"
    )
    pd.testing.assert_frame_equal(point_in_time_membership(VOLUME, CONFIG, refresh="D", gate=None), base)


def test_live_and_research_skip_the_same_names_given_the_same_answer() -> None:
    now_ms = 1_700_000_000_000
    ages = dict.fromkeys(VOLUME.columns, 400)
    volumes = {symbol: float(VOLUME[symbol].iloc[0]) * 1e6 for symbol in VOLUME.columns}
    asked: list[list[str]] = []

    async def gate(symbols: list[str]) -> set[str]:
        asked.append(symbols)
        return {"A", "B"}  # A is pinned and stays; B cannot be opened

    pool = LivePool(_FakeDailyClient(ages, volumes, now_ms), CONFIG)
    update = asyncio.run(pool.select(("A", "B", "C"), _rules(list(ages)), gate=gate))
    assert asked == [["A", "B", "C", "D", "E"]], "the gate is asked about every measured candidate, once"
    assert list(update.symbols) == ["A", "C", "D"] and update.left == ("B",) and update.entered == ("D",)
    assert update.gated == ("B",) and update.to_dict()["gated"] == ["B"]

    ungated = asyncio.run(pool.select(("A", "B", "C"), _rules(list(ages))))
    assert list(ungated.symbols) == ["A", "B", "C"] and ungated.gated == ()

    # Research, handed the same answer, picks the same three names.
    research = point_in_time_membership(VOLUME, CONFIG, refresh="D", gate=lambda date, previous: {"A", "B"})
    assert _members(research, "2024-02-29") == sorted(update.symbols)
    direct = refresh_selection(volumes, _rules(list(ages)), CONFIG, ["A", "B", "C"], 1, blocked={"A", "B"})
    assert direct.symbols == update.symbols
