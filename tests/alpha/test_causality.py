"""T-A02 / T-A03: no lookahead in signals; backtest executes at t+1 and random signals do not earn."""

from __future__ import annotations

import numpy as np
import pandas as pd

from beidou_alpha.backtest import CostModel, run_backtest
from beidou_alpha.panel import Panel
from beidou_alpha.signals.tsmom import TsmomParams, tsmom_scores


def _shuffle_future(panel: Panel, cutoff: int, seed: int = 7) -> Panel:
    rng = np.random.default_rng(seed)
    fields = {}
    for field in ("open", "high", "low", "close", "volume"):
        frame = getattr(panel, field).copy()
        future = frame.iloc[cutoff:].to_numpy().copy()
        rng.shuffle(future, axis=0)
        frame.iloc[cutoff:] = future * rng.uniform(0.5, 1.5, size=future.shape)
        fields[field] = frame
    return Panel(interval=panel.interval, **fields)


def test_tsmom_is_causal(august_panel: Panel) -> None:
    cutoff = 300
    params = TsmomParams(vol_window=100)
    before = tsmom_scores(august_panel.close, params).iloc[:cutoff]
    after = tsmom_scores(_shuffle_future(august_panel, cutoff).close, params).iloc[:cutoff]
    pd.testing.assert_frame_equal(before, after)


def test_backtest_executes_next_bar(august_panel: Panel) -> None:
    idx = august_panel.close.index
    weights = pd.DataFrame(0.0, index=idx, columns=august_panel.symbols)
    weights.iloc[100, 0] = 1.0  # decide at bar 100 on BTC
    result = run_backtest(august_panel, weights, CostModel(turnover_bps=0.0), execution="open_to_close")
    executed = result.weights
    assert executed.loc[idx[101], "BTCUSDT"] == 1.0
    assert executed.loc[idx[100], "BTCUSDT"] == 0.0 if idx[100] in executed.index else True
    expected = august_panel.close.loc[idx[101], "BTCUSDT"] / august_panel.open.loc[idx[101], "BTCUSDT"] - 1.0
    assert abs(result.gross.loc[idx[101], "BTCUSDT"] - expected) < 1e-12
    assert result.gross.drop(index=idx[101]).abs().sum().sum() == 0.0


def test_random_signals_do_not_earn_after_costs(august_panel: Panel) -> None:
    rng = np.random.default_rng(42)
    nets = []
    for _ in range(50):
        weights = (
            pd.DataFrame(
                rng.choice([-1.0, 0.0, 1.0], size=august_panel.close.shape),
                index=august_panel.close.index,
                columns=august_panel.symbols,
            )
            / 4.0
        )
        nets.append(run_backtest(august_panel, weights, CostModel(turnover_bps=7.0)).summary()["net_return"])
    assert float(np.mean(nets)) < 0.0
