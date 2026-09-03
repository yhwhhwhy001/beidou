"""Every registered signal: causal, bounded, and directionally sane on synthetic data (incl. T-A07 carry)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.panel import Panel
from beidou_alpha.signals import SIGNALS
from beidou_alpha.signals.base import scores_to_targets
from beidou_alpha.signals.carry import CarryParams, carry_scores
from beidou_alpha.signals.meanrev import MeanrevParams, meanrev_scores
from beidou_alpha.signals.residual import ResidualParams, residual_scores
from beidou_alpha.signals.xsmom import XsmomParams, xsmom_scores


def _synthetic_panel(seed: int = 0, n_symbols: int = 6, n_bars: int = 800, with_funding: bool = True) -> Panel:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=n_bars, freq="h", tz="UTC")
    symbols = ["BTCUSDT", *[f"S{i}USDT" for i in range(1, n_symbols)]]
    steps = rng.normal(0.0, 0.01, size=(n_bars, n_symbols))
    close = 100.0 * np.exp(np.cumsum(steps, axis=0))
    frames = {}
    for j, symbol in enumerate(symbols):
        c = close[:, j]
        o = np.concatenate(([c[0]], c[:-1]))
        volume = rng.uniform(100, 200, size=n_bars)
        frames[symbol] = pd.DataFrame(
            {
                "open_time": ((index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).to_numpy(),
                "open": o,
                "high": np.maximum(o, c) * 1.002,
                "low": np.minimum(o, c) * 0.998,
                "close": c,
                "volume": volume,
                "quote_volume": volume * c,
                "trades": 10,
                "taker_buy_base": volume * rng.uniform(0.3, 0.7, size=n_bars),
                "taker_buy_quote": volume * c * rng.uniform(0.3, 0.7, size=n_bars),
            }
        )
    funding = None
    if with_funding:
        funding = pd.DataFrame(0.0, index=index, columns=symbols)
        funding.iloc[::8] = rng.normal(0.0001, 0.0002, size=(len(funding.iloc[::8]), n_symbols))
    return Panel.from_frames(frames, "1h", funding=funding)


@pytest.mark.parametrize("signal_id", sorted(SIGNALS))
def test_signals_are_causal_and_bounded(signal_id: str) -> None:
    spec = SIGNALS[signal_id]
    panel = _synthetic_panel()
    scores = spec.compute(panel, spec.default_params)
    assert scores.shape == panel.close.shape
    assert ((scores.abs() <= 1.0) | scores.isna()).all().all()
    assert scores.iloc[-50:].notna().any().any(), f"{signal_id} produced no scores"
    cutoff = 600
    shuffled = _synthetic_panel(seed=99)
    mixed_frames = {}
    for symbol in panel.symbols:
        mixed_frames[symbol] = pd.DataFrame(
            {
                field: np.concatenate(
                    [
                        getattr(panel, field)[symbol].to_numpy()[:cutoff],
                        getattr(shuffled, field)[symbol].to_numpy()[cutoff:],
                    ]
                )
                for field in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_volume",
                    "trades",
                    "taker_buy_base",
                    "taker_buy_quote",
                )
            },
            index=panel.index,
        )
    funding = None
    if panel.funding is not None and shuffled.funding is not None:
        funding = pd.concat([panel.funding.iloc[:cutoff], shuffled.funding.iloc[cutoff:]])
    mixed = Panel.from_frames(mixed_frames, "1h", funding=funding)
    later = spec.compute(mixed, spec.default_params)
    pd.testing.assert_frame_equal(scores.iloc[:cutoff], later.iloc[:cutoff])


def test_carry_direction_and_missing_funding() -> None:
    """T-A07: constant positive funding -> short; missing funding -> NaN, never 0."""
    index = pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC")
    columns = pd.Index(["A", "B"])
    funding = pd.DataFrame(0.0, index=index, columns=columns)
    funding.loc[funding.index[::8], "A"] = 0.0001
    scores = carry_scores(funding, index, columns, CarryParams(window_bars=24, cross_sectional=False))
    assert (scores["A"].dropna() < 0).all()
    assert scores["B"].dropna().eq(0.0).all()  # zero funding observed -> flat, but B never settles -> NaN
    assert scores["B"].isna().all()
    assert carry_scores(None, index, columns).isna().all().all()
    relative = carry_scores(funding, index, columns, CarryParams(window_bars=24, cross_sectional=True))
    assert (relative["A"].dropna() < 0).all()


def test_meanrev_trades_against_dislocation_and_exits() -> None:
    index = pd.date_range("2024-01-01", periods=300, freq="h", tz="UTC")
    base = np.full(300, 100.0)
    base[200] = 130.0  # spike up: expect a short entry, then exit as the window absorbs it
    close = pd.DataFrame({"A": base + np.sin(np.arange(300)) * 0.5}, index=index)
    scores = meanrev_scores(close, MeanrevParams(window=48, z_entry=1.5, z_exit=0.5, trend_gate_z=50.0))
    assert scores["A"].iloc[200] < 0
    targets = scores_to_targets(scores, 0.2)
    assert targets["A"].iloc[200] < 0
    assert targets["A"].iloc[-1] == 0.0  # explicit exit once |z| < z_exit


def test_xsmom_ranks_relative_winners() -> None:
    index = pd.date_range("2024-01-01", periods=400, freq="h", tz="UTC")
    trend = np.exp(np.linspace(0, 0.5, 400))
    close = pd.DataFrame({"UP": 100 * trend, "FLAT": np.full(400, 100.0), "DOWN": 100 / trend}, index=index)
    scores = xsmom_scores(close, XsmomParams(horizons=(24, 72, 168), horizon_weights=(0.2, 0.3, 0.5)))
    last = scores.iloc[-1]
    assert last["UP"] > last["FLAT"] > last["DOWN"]
    assert last["UP"] > 0 > last["DOWN"]


def test_residual_is_zero_for_a_pure_beta_follower() -> None:
    rng = np.random.default_rng(1)
    index = pd.date_range("2024-01-01", periods=800, freq="h", tz="UTC")
    bench = np.exp(np.cumsum(rng.normal(0, 0.01, 800)))
    close = pd.DataFrame({"BTCUSDT": 100 * bench, "FOLLOWER": 50 * bench**1.5}, index=index)
    scores = residual_scores(close, ResidualParams(horizons=(24, 72), beta_window=200, scale=0.05))
    assert scores["BTCUSDT"].isna().all()
    assert scores["FOLLOWER"].iloc[-100:].abs().max() < 0.05


def test_explicit_zero_exit_semantics() -> None:
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC")
    scores = pd.DataFrame({"A": [np.nan, 0.5, 0.1, 0.0, np.nan]}, index=index)
    held = scores_to_targets(scores, 0.2)
    assert held["A"].tolist()[1:] == [0.5, 0.5, 0.0, 0.0]
    assert np.isnan(held["A"].iloc[0])
