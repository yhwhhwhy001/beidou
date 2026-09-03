"""T-X01..T-X05 exits overlay and T-S04 drawdown throttle (pure, synthetic paths)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.overlays import (
    DrawdownThrottleParams,
    ExitParams,
    ExitState,
    apply_drawdown_throttle,
    apply_exits,
    drawdown_scalar,
    exit_step,
)
from beidou_alpha.overlays.exits import COOLDOWN, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP


def _frames(
    path: list[float], target: float = 0.1, sigma: float = 0.02
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    index = pd.date_range("2024-01-01", periods=len(path), freq="h", tz="UTC")
    close = pd.DataFrame({"A": path}, index=index)
    weights = pd.DataFrame({"A": target}, index=index)
    vol = pd.DataFrame({"A": sigma}, index=index)
    return weights, close, vol


def test_stop_loss_exits_next_bar_then_cools_down_then_re_enters() -> None:
    """T-X01/T-X03: entry at 100 with sigma_1d 2%; stop at 2 units = 96; cooldown 5 bars; hold target re-enters after."""
    path = [100.0] * 3 + [100.0 * 0.99**k for k in range(1, 40)]
    weights, close, vol = _frames(path)
    params = ExitParams(stop_loss=2.0, cooldown_bars=5)
    result = apply_exits(weights, close, params, sigma_1d=vol)
    first_trigger = next(i for i, price in enumerate(path) if price <= 96.0)
    out = result.weights["A"]
    assert out.iloc[:first_trigger].eq(0.1).all()
    assert out.iloc[first_trigger : first_trigger + 5].eq(0.0).all()  # exit bar + cooldown
    assert out.iloc[first_trigger + 5] == 0.1  # re-entered on the hold target after the cooldown
    events = result.events
    assert events.iloc[0]["rule"] == STOP_LOSS and events.iloc[0]["entry_price"] == 100.0
    assert events.iloc[0]["units"] <= -2.0
    assert len(events) >= 2  # the path keeps falling, so the re-entry is stopped again
    assert result.summary()["by_rule"][STOP_LOSS] == len(events)


def test_take_profit_and_trailing_stop() -> None:
    """T-X01: take-profit at +3 units (106); trailing stop at 2 units below the extreme."""
    up = [100.0 * 1.01**k for k in range(0, 12)]
    weights, close, vol = _frames(up)
    result = apply_exits(weights, close, ExitParams(take_profit=3.0, cooldown_bars=100), sigma_1d=vol)
    trigger = next(i for i, price in enumerate(up) if price >= 106.0)
    assert result.events.iloc[0]["rule"] == TAKE_PROFIT
    assert result.weights["A"].iloc[trigger:].eq(0.0).all()
    hump = [100.0, 104.0, 108.0, 110.0, 109.0, 107.0, 105.5, 104.0]
    weights, close, vol = _frames(hump)
    result = apply_exits(weights, close, ExitParams(trailing_stop=2.0, cooldown_bars=100), sigma_1d=vol)
    assert result.events.iloc[0]["rule"] == TRAILING_STOP
    assert result.events.iloc[0]["time"] == close.index[6]  # 110 -> 105.5 is a 4.5-point (2.25 unit) retrace
    assert result.weights["A"].tolist()[:6] == [0.1] * 6


def test_cooldown_blocks_same_direction_only() -> None:
    """T-X02: after a long is stopped, a short target may enter immediately, a long may not."""
    params = ExitParams(stop_loss=1.0, cooldown_bars=10)
    state = ExitState()
    state, w, reason = exit_step(state, 0.1, 100.0, 0.02, 0, params)
    assert w == 0.1 and reason == "" and state.direction == 1 and state.entry_price == 100.0
    state, w, reason = exit_step(state, 0.1, 97.0, 0.02, 1, params)
    assert w == 0.0 and reason == STOP_LOSS and state.cooldown_until == 11 and state.cooldown_direction == 1
    blocked, w, reason = exit_step(state, 0.1, 97.0, 0.02, 2, params)
    assert w == 0.0 and reason == COOLDOWN and blocked.direction == 0
    flipped, w, reason = exit_step(state, -0.1, 97.0, 0.02, 2, params)
    assert w == -0.1 and reason == "" and flipped.direction == -1 and flipped.entry_price == 97.0
    after, w, reason = exit_step(state, 0.1, 97.0, 0.02, 11, params)
    assert w == 0.1 and after.direction == 1
    assert ExitState.from_dict(state.to_dict()) == state


def test_resize_keeps_entry_and_disabled_params_pass_through() -> None:
    params = ExitParams(stop_loss=2.0)
    state, _, _ = exit_step(ExitState(), 0.1, 100.0, 0.02, 0, params)
    bigger, w, _ = exit_step(state, 0.14, 101.0, 0.03, 1, params)
    assert w == 0.14 and bigger.entry_price == 100.0 and bigger.unit == 0.02 and bigger.extreme == 101.0
    off = ExitParams()
    assert not off.enabled
    weights, close, vol = _frames([100.0, 50.0, 25.0])
    result = apply_exits(weights, close, off, sigma_1d=vol)
    pd.testing.assert_frame_equal(result.weights, weights)
    with pytest.raises(ValueError):
        ExitParams(stop_loss=-1.0)


def test_exits_are_causal() -> None:
    """T-X05: decisions up to bar t do not depend on later prices."""
    rng = np.random.default_rng(5)
    path = list(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, size=80))))
    weights, close, vol = _frames(path)
    params = ExitParams(stop_loss=1.5, trailing_stop=2.0, take_profit=3.0, cooldown_bars=6)
    full = apply_exits(weights, close, params, sigma_1d=vol).weights
    cut = 40
    truncated = apply_exits(weights.iloc[:cut], close.iloc[:cut], params, sigma_1d=vol.iloc[:cut]).weights
    pd.testing.assert_frame_equal(full.iloc[:cut], truncated)


def test_drawdown_scalar_shape_and_throttle_path() -> None:
    """T-S04: 1 above start, linear to the floor at stop, never below the floor; disabled = identity."""
    params = DrawdownThrottleParams(start=0.05, stop=0.20, floor=0.25, enabled=True)
    assert drawdown_scalar(0.0, params) == 1.0 and drawdown_scalar(0.05, params) == 1.0
    assert abs(drawdown_scalar(0.125, params) - 0.625) < 1e-12
    assert drawdown_scalar(0.20, params) == 0.25 and drawdown_scalar(0.9, params) == 0.25
    index = pd.date_range("2024-01-01", periods=30, freq="h", tz="UTC")
    weights = pd.DataFrame({"A": 0.5}, index=index)
    returns = pd.Series([-0.01] * 30, index=index)
    scaled, scalars = apply_drawdown_throttle(weights, returns, params)
    assert scalars.is_monotonic_decreasing and scalars.iloc[-1] < 1.0 and scalars.min() >= 0.25
    assert (scaled["A"] == 0.5 * scalars).all()
    same, ones = apply_drawdown_throttle(weights, returns, DrawdownThrottleParams())
    pd.testing.assert_frame_equal(same, weights)
    assert ones.eq(1.0).all()
    with pytest.raises(ValueError):
        DrawdownThrottleParams(start=0.3, stop=0.2)
