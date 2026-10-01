"""The pool entry gate's research half: the pool walked forward day by day, as the live loop decides it.

Operator ruling 2026-09-30 ("faithful" option); 2026-10-01, "逐日前向模拟".  At each refresh, in date order,
`ForwardGate` advances the main book over the bars the previous refresh covered - on the members it chose
(`MainBook`) - and blocks the names whose would-be weight misses `entry_line`; `Shortlist` keeps out what the
live pool never measures.  One pass; the result is then checked against the real model run on the finished
table, the book cell by cell and every decision.  `beidou data pool gate` writes it; research reads it only while
it is current.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import numpy as np
import pandas as pd
import pytest

from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, asset_vol, per_name_risk
from beidou_alpha.registry import MAIN_BOOK, StrategyEntry
from beidou_alpha.signals.tsmom import TsmomParams, compute, crowding_mask, tsmom_scores
from beidou_cli.research_panel import _membership, _pit_table
from beidou_cli.research_pool_gate import (
    ForwardGate,
    MainBook,
    Shortlist,
    checked,
    gate_key,
    read_gated,
    shortlist_size,
    walk,
    write_gated,
)
from beidou_data.pool import MEMBERSHIP_FILE, LivePool, membership_at_bars, point_in_time_membership
from beidou_data.store import write_parquet_atomically
from beidou_data.universe import UniverseConfig
from beidou_shared.config import load_yaml
from tests.data.test_pool import _FakeDailyClient, _rules
from tests.live.test_live_loop import SYMBOLS, _model

CONFIG = UniverseConfig(
    top_n=3, enter_rank=3, exit_rank=3, min_age_days=3, volume_lookback_days=3, always_include=("BTCUSDT",)
)


def _volume(panel: Panel) -> pd.DataFrame:
    days = pd.date_range(pd.Timestamp(panel.index[0]).normalize(), pd.Timestamp(panel.index[-1]).normalize(), freq="D")
    ranked = {"BTCUSDT": 400.0, "SOLUSDT": 300.0, "ETHUSDT": 200.0, "BNBUSDT": 100.0}  # SOL is the one to gate
    return pd.DataFrame({s: ranked[s] for s in SYMBOLS}, index=days)


def _crowded() -> tuple[Panel, AlphaModel, pd.DataFrame]:
    """Eight names with funding, one listed late and one missing a bar; every name leaves and re-enters."""
    rng = np.random.default_rng(20261001)
    bars = pd.date_range("2024-01-01", periods=30 * 24, freq="h", tz="UTC")
    names = [f"N{i}USDT" for i in range(8)]
    walks = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, (len(bars), len(names))), axis=0))
    close = pd.DataFrame(walks, index=bars, columns=names)
    close.iloc[200, 2] = np.nan
    frames = {
        s: pd.DataFrame(dict.fromkeys(("open", "high", "low", "close"), close[s]) | {"volume": 1.0}) for s in names
    }
    frames["N7USDT"] = frames["N7USDT"].iloc[4 * 24 :]  # listed on day five, a member from day one
    settlements = bars[::8]
    funding = pd.DataFrame(rng.normal(0.0, 1e-4, (len(settlements), len(names))), index=settlements, columns=names)
    panel = Panel.from_frames(frames, interval="1h", funding=funding)
    params = TsmomParams(vol_window=48).__dict__ | {
        "horizons": [5, 20, 50],
        "horizon_weights": [0.2, 0.3, 0.5],
        "entry_threshold": 0.3,
        "crowding_window": 24,
        "crowding_cut": 0.5,
        "crowding_penalty": 0.5,
        "conviction_mode": "sign",
    }
    model = AlphaModel(
        entries=(StrategyEntry("tsmom", params=params),),
        portfolio=PortfolioParams(covariance_halflife=48, vol_halflife=24, max_weight=0.15, max_gross=0.6),
        interval="1h",
        min_history_bars=48,
    )
    days = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
    table = pd.DataFrame({s: [(d + j) % 4 != 0 for d in range(len(days))] for j, s in enumerate(names)}, index=days)
    return panel, model, table


def _advanced_on(table: pd.DataFrame, panel: Panel, model: AlphaModel) -> MainBook:
    """A book advanced through a fixed table the way `ForwardGate` advances it: each refresh's bars, its members."""
    book, previous = MainBook(panel, model), list[str]()
    for date, row in table.iterrows():
        book.advance(int(panel.index.searchsorted(date, side="left")) - 1, previous)
        previous = [str(s) for s, member in row.items() if member]
    book.advance(len(panel.index) - 1, previous)
    return book


def test_the_main_book_advances_bar_for_bar_as_the_model_computes_it() -> None:
    panel, model, table = _crowded()
    mask = membership_at_bars(table, panel.index)
    real = model.book_targets(model.strategy_targets(panel, mask))[MAIN_BOOK]
    np.testing.assert_array_equal(_advanced_on(table, panel, model).targets, real.to_numpy(dtype=float))

    # The comparison reached every part of the book that depends on membership.
    entry, eligible = model.entries[0], model.eligible(panel, mask)
    p = TsmomParams.from_mapping(entry.params)
    shrunk = crowding_mask(tsmom_scores(panel.close, p), panel.funding, p, eligible)
    assert shrunk is not None and bool((shrunk & eligible).to_numpy().any()), "the crowding rank decided cells"
    scores = compute(panel.with_reference(eligible), entry.params)
    held = eligible & (scores.abs() < entry.entry_threshold) & real.ne(0.0) & real.notna()
    assert bool(held.to_numpy().any()), "a sub-threshold score kept the previous target"
    assert bool((eligible.astype(int).diff() > 0).iloc[1:].to_numpy().sum() > len(table.columns)), "names re-entered"
    assert not bool(eligible["N7USDT"].iloc[: 4 * 24 + 48].any()), "a member without history is not eligible"


def test_a_main_book_it_cannot_reproduce_is_refused(august_panel: Panel) -> None:
    model = _model()
    with pytest.raises(click.ClickException, match="tsmom alone under the mean ensemble"):
        MainBook(august_panel, replace(model, ensemble_method="rolling_zscore"))
    with pytest.raises(click.ClickException, match=r"\['tsmom', 'xsmom'\]"):
        MainBook(august_panel, replace(model, entries=(*model.entries, StrategyEntry("xsmom"))))


def test_the_forward_gate_prices_the_book_the_way_the_model_does(august_panel: Panel) -> None:
    """Formula fidelity: the gate's risk per name is `per_name_risk` of the model's own main book."""
    model, panel = _model(), august_panel
    members = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    mask = pd.DataFrame(False, index=panel.index, columns=panel.close.columns)
    mask[members] = True
    sigma = asset_vol(panel.close, model.portfolio, panel.bars_per_year)
    book = model.book_weights(model.strategy_targets(panel, mask), panel.close, panel.bars_per_year)[MAIN_BOOK]
    expected = per_name_risk(book, sigma)
    gate, compared = ForwardGate(panel, model), 0
    for t in (120, 300, len(panel.index) - 2):
        date = pd.Timestamp(panel.index[t + 1])
        gate(date, members)
        simulated = gate.decisions[date][0]
        if np.isnan(expected.iloc[t]):
            assert simulated is None
            continue
        assert simulated == pytest.approx(float(expected.iloc[t]), rel=1e-12), t
        compared += 1
    assert compared >= 2, "the comparison must actually have compared something"


def test_a_walk_skips_the_name_the_book_cannot_open_and_agrees_with_the_model(august_panel: Panel) -> None:
    panel, volume = august_panel, _volume(august_panel)
    base = point_in_time_membership(volume, CONFIG, eligible=SYMBOLS, refresh="D")
    model = _model()
    # Place the line at SOL's median would-be weight on the ungated book, so the gate has something to decide.
    sigma = asset_vol(panel.close, model.portfolio, panel.bars_per_year)
    probe = ForwardGate(panel, model)
    point_in_time_membership(volume, CONFIG, eligible=SYMBOLS, refresh="D", gate=probe)  # line 0: blocks nothing
    stops = {d: int(panel.index.searchsorted(d, side="left")) - 1 for d in probe.decisions}
    sol = [risk / sigma["SOLUSDT"].iloc[stops[d]] for d, (risk, _) in probe.decisions.items() if risk]
    gated_model = replace(model, portfolio=replace(model.portfolio, no_trade_band=float(np.median(sol))))

    table, gate, unjudged = walk(base, volume, CONFIG, SYMBOLS, panel, gated_model)
    assert unjudged == 0
    assert checked(table, panel, gated_model, gate) == {"targets_cells_differing": 0, "decisions_disagreeing": 0}
    assert not table.equals(base.reindex(columns=table.columns, fill_value=False)), "the gate decided something"
    for date, (_risk, blocked) in gate.decisions.items():
        chosen = {str(s) for s, member in table.loc[date].items() if member}
        assert not (chosen & blocked) - set(CONFIG.always_include), date
    narrow, _gate, _unjudged = walk(base, volume, CONFIG, SYMBOLS, panel, gated_model, shortlist=1)
    assert not narrow.drop(columns="BTCUSDT").to_numpy().any(), "with a shortlist of one, only the pin is measured"


def test_the_walk_fills_only_from_what_the_live_pool_measures() -> None:
    """With the top of the pool blocked, live fills from its 24h shortlist alone; the walk keeps out the same names."""
    names, now_ms = ["A", "B", "C", "D", "E"], 1_700_000_000_000
    volumes = dict(zip(names, (5e8, 4e8, 3e8, 2e8, 1e8), strict=True))
    config = UniverseConfig(
        top_n=3, enter_rank=3, exit_rank=3, min_age_days=5, volume_lookback_days=5, always_include=("A",)
    )

    async def book(symbols: list[str]) -> set[str]:
        return {"B", "C"}

    pool = LivePool(_FakeDailyClient(dict.fromkeys(names, 400), volumes, now_ms), config, candidates=3)
    live, kept = (asyncio.run(pool.select(previous, _rules(names), gate=book)) for previous in ((), ("A", "D")))

    days = pd.date_range("2024-01-01", periods=20, freq="D", tz="UTC")
    volume = pd.DataFrame(volumes, index=days)
    unlisted = Shortlist(volume, names, 3)
    walked = point_in_time_membership(volume, config, refresh="D", gate=lambda d, p: {"B", "C"} | unlisted(d, p))
    unbounded = point_in_time_membership(volume, config, refresh="D", gate=lambda d, p: {"B", "C"})
    last = [sorted(s for s, member in table.iloc[-1].items() if member) for table in (walked, unbounded)]
    assert list(live.symbols) == ["A"] == last[0]
    assert last[1] == ["A", "D", "E"], "without the shortlist research fills from names live never measures"
    assert list(kept.symbols) == ["A", "D"] and unlisted(days[-1], ["A", "D"]) == {"E"}, "a member outside it stays"
    assert shortlist_size(load_yaml("config/live.demo.yaml"), config) == 45 and shortlist_size({}, config) == 9


def test_a_base_the_inputs_cannot_rebuild_is_refused(august_panel: Panel) -> None:
    volume = _volume(august_panel)
    tampered = point_in_time_membership(volume, CONFIG, eligible=SYMBOLS, refresh="D")
    tampered.iloc[-1, tampered.columns.get_loc("BNBUSDT")] = True
    with pytest.raises(click.ClickException, match="cannot be rebuilt"):
        walk(tampered, volume, CONFIG, SYMBOLS, august_panel, _model())


DAYS = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
VOLUME = pd.DataFrame({"A": 600.0, "B": 500.0, "C": 400.0, "D": 300.0, "E": 200.0}, index=DAYS)
SMALL = UniverseConfig(
    top_n=3, enter_rank=3, exit_rank=3, min_age_days=5, volume_lookback_days=5, always_include=("A",)
)


def _profile(gate: bool, **portfolio: Any) -> dict[str, Any]:
    profile = load_yaml("config/live.demo.yaml")
    profile["portfolio"] = {**profile["portfolio"], "pool_entry_gate": gate, **portfolio}
    return profile


def _root(tmp_path: Path, usable: bool = True) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    base = point_in_time_membership(VOLUME, SMALL, refresh="D")
    write_parquet_atomically(base, tmp_path / MEMBERSHIP_FILE, index=None)
    gated = point_in_time_membership(VOLUME, SMALL, refresh="D", gate=lambda date, previous: {"B"})
    write_gated(str(tmp_path), gated, {**gate_key(_profile(True), base), "usable": usable})
    return str(tmp_path), base, gated


def test_the_gated_table_is_read_only_while_its_record_describes_the_run(tmp_path: Path) -> None:
    root, base, gated = _root(tmp_path)
    pd.testing.assert_frame_equal(read_gated(root, _profile(True), base), gated, check_freq=False)
    with pytest.raises(click.ClickException, match="construction"):
        read_gated(root, _profile(True, vol_target=0.30), base)  # another book, another answer
    with pytest.raises(click.ClickException, match="shortlist"):
        read_gated(root, {**_profile(True), "pool": {"candidates": 60}}, base)  # another pool, other candidates
    moved = base.copy()
    moved.iloc[0, 0] = not bool(moved.iloc[0, 0])
    with pytest.raises(click.ClickException, match="base"):
        read_gated(root, _profile(True), moved)  # the stored table was rebuilt since
    (tmp_path / "unusable").mkdir()
    unusable_root, unusable_base, _gated = _root(tmp_path / "unusable", usable=False)
    with pytest.raises(click.ClickException, match="not usable"):
        read_gated(unusable_root, _profile(True), unusable_base)


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
