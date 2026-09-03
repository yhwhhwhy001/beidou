"""T-P01/T-P02: hysteresis ranking and point-in-time membership (pure)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from beidou_data.pool import membership_at_bars, membership_summary, point_in_time_membership, refresh_selection
from beidou_data.universe import UniverseConfig, rank_with_hysteresis
from beidou_shared.types import InstrumentRules


def _rules(symbols: list[str]) -> dict[str, InstrumentRules]:
    return {s: InstrumentRules(s, Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("5")) for s in symbols}


def test_rank_with_hysteresis_enter_exit_and_pins() -> None:
    """T-P01: rank <= 15 enters, an incumbent survives to rank 20, rank 21 leaves, pins survive any rank."""
    symbols = [f"S{i:02d}" for i in range(1, 31)]
    volumes = {s: float(100 - i) for i, s in enumerate(symbols)}  # S01 is rank 1 ... S30 rank 30
    previous = ["S18", "S21", "S05"]
    chosen = rank_with_hysteresis(
        volumes, symbols, previous, enter_rank=15, exit_rank=20, top_n=15, always_include=("S30",)
    )
    assert set(symbols[:15]) <= set(chosen)
    assert "S18" in chosen and "S21" not in chosen and "S30" in chosen
    assert chosen[0] == "S01" and len(chosen) <= 21
    update = refresh_selection(volumes, _rules(symbols), UniverseConfig(top_n=15), previous, at_ms=1)
    assert "S21" in update.left and "S01" in update.entered and update.symbols[0] == "S01"


def test_point_in_time_membership_is_causal_and_respects_listing_age() -> None:
    """T-P02: a symbol cannot be a member before it has min_age_days of bars, and a refresh only sees earlier days."""
    days = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
    volume = pd.DataFrame({"A": 100.0, "B": 90.0, "C": np.nan, "D": 10.0}, index=days)
    volume.loc[days[60:], "C"] = 1000.0  # listed on day 60 with huge volume
    volume.loc[days[95:], "D"] = 5000.0  # D becomes the most liquid name only from day 95
    config = UniverseConfig(top_n=2, enter_rank=2, exit_rank=3, min_age_days=30, volume_lookback_days=30)
    membership = point_in_time_membership(volume, config, refresh="MS")
    assert list(membership.index) == [
        pd.Timestamp("2024-01-31", tz="UTC"),
        *pd.date_range("2024-02-01", "2024-04-01", freq="MS", tz="UTC"),
    ]
    march = membership.loc["2024-03-01"]
    assert bool(march["A"]) and bool(march["B"]) and not bool(march["C"])  # C listed 2024-03-01: no age yet
    april = membership.loc["2024-04-01"]
    assert bool(april["C"]) and not bool(april["D"])  # D's surge starts after the April refresh: unseen
    assert not bool(april["B"]) or bool(april["A"])  # exactly top-2 plus hysteresis survivors
    hourly = pd.date_range("2024-01-20", periods=24 * 80, freq="h", tz="UTC")
    at_bars = membership_at_bars(membership, hourly)
    assert not at_bars.loc["2024-01-20 00:00", "A"]  # before the first refresh nobody is in
    assert at_bars.loc["2024-02-15 12:00", "A"] and not at_bars.loc["2024-02-15 12:00", "C"]
    assert at_bars.loc["2024-04-02 00:00", "C"]
    summary = membership_summary(membership)
    assert summary["refreshes"] == len(membership) and "C" in summary["union"]
