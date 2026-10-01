"""The pool entry gate's research half: a fixed point of pool and book, written once, read only while it is current.

Operator ruling 2026-09-30 ("faithful" option).  Which names the book could open depends on the book, and the book
depends on the pool, so research re-ranks until the membership stops moving (`research_pool_gate.fixed_point`).
`beidou data pool gate` writes the result with a record of what it was computed from; while the profile turns the
gate on, every research command reads that table instead of the stored one, and refuses when the record no longer
describes the construction, the main book or the base table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pandas as pd
import pytest

from beidou_cli.research_panel import _membership, _pit_table
from beidou_cli.research_pool_gate import fixed_point, gate_key, read_gated, write_gated
from beidou_data.pool import MEMBERSHIP_FILE, point_in_time_membership
from beidou_data.store import write_parquet_atomically
from beidou_data.universe import UniverseConfig
from beidou_shared.config import load_yaml

DAYS = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
VOLUME = pd.DataFrame({"A": 600.0, "B": 500.0, "C": 400.0, "D": 300.0, "E": 200.0}, index=DAYS)
CONFIG = UniverseConfig(
    top_n=3, enter_rank=3, exit_rank=3, min_age_days=5, volume_lookback_days=5, always_include=("A",)
)
SIGMA = {"A": 0.4, "B": 3.0, "C": 0.8, "D": 0.9, "E": 1.0}  # B is the one the book could not open
RISK = 0.0139  # risk per name: B's would-be weight is 0.46%, under a 1% line; everyone else clears it


def _reader(calls: list[pd.DataFrame]) -> Any:
    hourly = pd.date_range(DAYS[0], DAYS[-1], freq="h")

    def read_book(table: pd.DataFrame, judged: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
        calls.append(table)
        members = [str(s) for s in table.columns[table.any(axis=0)]]
        sigma = pd.DataFrame({s: SIGMA[s] for s in judged}, index=hourly)
        return RISK / sigma[members], sigma  # sign mode: every held name carries the same risk

    return read_book


def test_the_fixed_point_skips_the_name_the_book_cannot_open_and_stops_when_nothing_moves() -> None:
    base = point_in_time_membership(VOLUME, CONFIG, eligible=list(VOLUME.columns), refresh="D")
    calls: list[pd.DataFrame] = []
    table, log = fixed_point(base, VOLUME, CONFIG, list(VOLUME.columns), _reader(calls), 0.01)

    last = table.iloc[-1]
    assert sorted(last[last].index) == ["A", "C", "D"], "B is skipped, D takes its slot"
    assert sorted(base.iloc[-1][base.iloc[-1]].index) == ["A", "B", "C"]
    assert [row["cells_moved"] for row in log][-1] == 0 and len(log) == 2, log
    assert calls[0].equals(base), "the first reading is of the book on the stored table"


def test_a_base_the_inputs_cannot_rebuild_is_refused() -> None:
    base = point_in_time_membership(VOLUME, CONFIG, eligible=list(VOLUME.columns), refresh="D")
    tampered = base.copy()
    tampered.iloc[-1, tampered.columns.get_loc("E")] = True
    with pytest.raises(click.ClickException, match="cannot be rebuilt"):
        fixed_point(tampered, VOLUME, CONFIG, list(VOLUME.columns), _reader([]), 0.01)


def _profile(gate: bool, **portfolio: Any) -> dict[str, Any]:
    profile = load_yaml("config/live.demo.yaml")
    profile["portfolio"] = {**profile["portfolio"], "pool_entry_gate": gate, **portfolio}
    return profile


def _root(tmp_path: Path) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    base = point_in_time_membership(VOLUME, CONFIG, eligible=list(VOLUME.columns), refresh="D")
    write_parquet_atomically(base, tmp_path / MEMBERSHIP_FILE, index=None)
    gated, _log = fixed_point(base, VOLUME, CONFIG, list(VOLUME.columns), _reader([]), 0.01)
    record = {**gate_key(_profile(True), base), "converged": True, "rounds": _log}
    write_gated(str(tmp_path), gated, record)
    return str(tmp_path), base, gated


def test_the_gated_table_is_read_only_while_its_record_describes_the_run(tmp_path: Path) -> None:
    root, base, gated = _root(tmp_path)
    pd.testing.assert_frame_equal(read_gated(root, _profile(True), base), gated, check_freq=False)
    with pytest.raises(click.ClickException, match="construction"):
        read_gated(root, _profile(True, vol_target=0.30), base)  # another book, another answer
    moved = base.copy()
    moved.iloc[0, 0] = not bool(moved.iloc[0, 0])
    with pytest.raises(click.ClickException, match="base"):
        read_gated(root, _profile(True), moved)  # the stored table was rebuilt since


def test_research_commands_read_the_gated_table_only_while_the_profile_turns_the_gate_on(tmp_path: Path) -> None:
    root, base, gated = _root(tmp_path)
    off, on = _pit_table(root, _profile(False)), _pit_table(root, _profile(True))
    pd.testing.assert_frame_equal(off, base, check_freq=False)
    pd.testing.assert_frame_equal(on, gated, check_freq=False)
    assert off.iloc[-1]["B"] and not on.iloc[-1]["B"] and on.iloc[-1]["D"]
    assert _pit_table(root, None).equals(off), "a command that passes no profile reads the stored table, as before"

    class _Panel:
        index = pd.date_range(DAYS[-2], DAYS[-1], freq="h")

    mask = _membership(root, "pit", _Panel(), profile=_profile(True))  # type: ignore[arg-type]
    assert mask is not None and not bool(mask.iloc[-1]["B"]) and bool(mask.iloc[-1]["D"])
