"""Round 6 (E-042 / KILL-027): warmup under registry params, the NO_ACTION hold seed, live == research parity."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals import get_signal
from beidou_alpha.signals.base import scores_to_targets
from beidou_alpha.signals.tsmom import TsmomParams, tsmom_scores
from beidou_live.composition import build_model, load_registry
from beidou_shared.config import load_yaml
from tests.alpha.test_signal_suite import _synthetic_panel

ROOT = Path(__file__).resolve().parents[2]
WEEKLY = {"horizons": [168, 336, 720], "horizon_weights": [0.2, 0.3, 0.5]}
HOURLY = {"horizons": [5, 20, 50], "horizon_weights": [0.2, 0.3, 0.5]}


def _frames(panel: Panel) -> dict[str, pd.DataFrame]:
    """Per-symbol frames the live loop would hand to ``AlphaModel.targets`` (no funding)."""
    fields = ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote")
    out: dict[str, pd.DataFrame] = {}
    for symbol in panel.symbols:
        out[symbol] = pd.DataFrame(
            {field: getattr(panel, field)[symbol] for field in fields if getattr(panel, field) is not None}
        )
    return out


def test_warmup_follows_registry_params_not_signal_defaults() -> None:
    weekly = AlphaModel(entries=(StrategyEntry("tsmom", params=WEEKLY),), portfolio=PortfolioParams(), interval="1h")
    hourly = AlphaModel(entries=(StrategyEntry("tsmom", params=HOURLY),), portfolio=PortfolioParams(), interval="1h")
    assert get_signal("tsmom").warmup_for(WEEKLY) == 721 and get_signal("tsmom").warmup_for(HOURLY) == 51
    assert weekly.warmup_bars == 722  # a 720-bar horizon, plus one bar for the first decision
    assert hourly.warmup_bars == 97  # the covariance half-life dominates the 5/20/50 defaults
    shipped = build_model(load_registry(ROOT / "config/alpha_registry.yaml"), load_yaml(ROOT / "config/live.demo.yaml"))
    assert shipped.warmup_bars >= 722  # the live window is sized from this number (E-042)


def test_hold_seed_keeps_the_previous_target_through_a_sub_threshold_window() -> None:
    index = pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC")
    scores = pd.DataFrame(
        {"A": [0.1, 0.15, -0.1, 0.05], "B": [np.nan, np.nan, 0.3, 0.1], "C": [np.nan] * 4}, index=index
    )
    plain = scores_to_targets(scores, 0.2)
    assert plain["A"].tolist() == [0.0, 0.0, 0.0, 0.0]  # nothing actionable in the window -> flat
    seeded = scores_to_targets(scores, 0.2, initial={"A": 0.5, "B": -0.4, "C": 0.9})
    assert seeded["A"].tolist() == [0.5, 0.5, 0.5, 0.5]  # held from before the window (D-005)
    assert seeded["B"].iloc[:2].isna().all() and seeded["B"].tolist()[2:] == [0.3, 0.3]  # actionable overrides
    assert seeded["C"].isna().all()  # never scored -> not tradable whatever the seed
    exits = scores.copy()
    exits.loc[index[1], "A"] = 0.0
    assert scores_to_targets(exits, 0.2, initial={"A": 0.5})["A"].tolist() == [0.5, 0.0, 0.0, 0.0]  # explicit exit
    assert scores_to_targets(scores, 0.2, hold=False, initial={"A": 0.5})["A"].tolist() == [0.0] * 4


def test_live_targets_hold_previous_strategy_targets(august_panel: Panel) -> None:
    entry = StrategyEntry("tsmom", params={**TsmomParams(vol_window=100).__dict__, **HOURLY, "entry_threshold": 1.0})
    model = AlphaModel(
        entries=(entry,),
        portfolio=PortfolioParams(covariance_halflife=48, vol_halflife=24),
        interval="1h",
        min_history_bars=0,
    )
    frames = _frames(august_panel)
    flat = model.targets(frames, {})
    assert all(value == 0.0 for value in flat.contributions["tsmom"].values())  # |score| >= 1.0 never happens
    held = model.targets(frames, {}, previous={"tsmom": {"BTCUSDT": 0.5}, "stale_strategy": {"ETHUSDT": 1.0}})
    assert held.contributions["tsmom"]["BTCUSDT"] == 0.5 and held.weights["BTCUSDT"] > 0
    assert held.contributions["tsmom"]["ETHUSDT"] == 0.0 and held.weights["ETHUSDT"] == 0.0


def test_shipped_registry_live_path_matches_research_path() -> None:
    """KILL-027 guard: the live path (no funding frame) must produce the research path's targets and weights."""
    registry = load_registry(ROOT / "config/alpha_registry.yaml")
    model = build_model(registry, load_yaml(ROOT / "config/live.demo.yaml"))
    panel = _synthetic_panel(seed=3, n_symbols=6, n_bars=1600, with_funding=True)
    weights, _combined, per_strategy = model.evaluate(panel)
    live = model.targets(_frames(panel), {})
    assert live.as_of == panel.index[-1]
    for strategy, frame in per_strategy.items():
        expected = frame.iloc[-1].fillna(0.0)
        for symbol in panel.symbols:
            assert live.contributions[strategy][symbol] == pytest.approx(float(expected[symbol]), abs=1e-12), (
                strategy,
                symbol,
            )
    last = weights.iloc[-1].fillna(0.0)
    for symbol in panel.symbols:
        assert live.weights[symbol] == pytest.approx(float(last[symbol]), abs=1e-12)


def test_tsmom_vol_scaled_mode_penalises_the_noisier_path_with_the_same_return() -> None:
    n = 400
    t = np.arange(n)
    index = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    smooth = 100.0 * np.exp(0.0005 * t)
    noisy = smooth * (1.0 + 0.02 * np.where(t % 2 == 0, 1.0, -1.0))  # identical returns over every even horizon
    close = pd.DataFrame({"A": smooth, "B": noisy}, index=index)
    horizons = {"horizons": (10, 20, 50), "horizon_weights": (0.2, 0.3, 0.5), "vol_window": 100}
    fixed = tsmom_scores(close, TsmomParams(momentum_mode="fixed", **horizons))
    scaled = tsmom_scores(close, TsmomParams(momentum_mode="vol_scaled", return_scale=1.0, **horizons))
    assert fixed["A"].iloc[-1] == pytest.approx(fixed["B"].iloc[-1])  # the validated form ignores the path's vol
    assert scaled["A"].iloc[-1] > scaled["B"].iloc[-1] > 0.0  # the t-statistic form does not
    assert (scaled.abs().dropna() <= 1.0).all().all()
    with pytest.raises(ValueError):
        TsmomParams(momentum_mode="bogus")
