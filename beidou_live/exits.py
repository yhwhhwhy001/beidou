"""Live adapter of the exit overlay: venue positions are the truth for direction and entry price (D-012, KILL-022)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pandas as pd

from beidou_alpha.overlays.exits import COOLDOWN, ExitParams, ExitState, daily_vol, exit_step
from beidou_shared.types import Position


class ExitOverlay:
    def __init__(self, params: ExitParams, interval_ms: int) -> None:
        self.params = params
        self.interval_ms = interval_ms

    @property
    def enabled(self) -> bool:
        return self.params.enabled

    def apply(
        self,
        targets: Mapping[str, float],
        *,
        positions: Mapping[str, Position],
        bars: Mapping[str, pd.DataFrame],
        states: Mapping[str, Mapping[str, Any]],
        bar_open_ms: int,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Return (adjusted targets, new per-symbol states, exit events)."""
        adjusted = dict(targets)
        new_states: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        if not self.enabled:
            return adjusted, {}, events
        for symbol, target in targets.items():
            frame = bars.get(symbol)
            if frame is None or len(frame) < 2:
                continue
            close = float(frame["close"].iloc[-1])
            sigma = self._sigma(frame)
            state = self._reconcile(ExitState.from_dict(states.get(symbol, {})), positions.get(symbol), close, sigma)
            before = state
            state, weight, reason = exit_step(
                state, float(target), close, sigma, bar_open_ms, self.params, bar_step=self.interval_ms
            )
            adjusted[symbol] = weight
            new_states[symbol] = state.to_dict()
            if reason:
                events.append(
                    {
                        "symbol": symbol,
                        "rule": reason,
                        "target": float(target),
                        "price": close,
                        "entry_price": None if math.isnan(before.entry_price) else before.entry_price,
                        "unit": None if math.isnan(before.unit) else before.unit,
                        "cooldown_until_ms": state.cooldown_until if reason != COOLDOWN else before.cooldown_until,
                    }
                )
        return adjusted, new_states, events

    def _sigma(self, frame: pd.DataFrame) -> float:
        closes = pd.DataFrame({"x": frame["close"].astype(float).to_numpy()})
        value = daily_vol(closes, self.params)["x"].iloc[-1]
        return float(value) if pd.notna(value) else float("nan")

    def _reconcile(self, state: ExitState, position: Position | None, close: float, sigma: float) -> ExitState:
        """Adopt the venue's position (direction + VWAP entry) as the reference; drop stale held state."""
        held = 0 if position is None or position.qty == 0.0 else (1 if position.qty > 0 else -1)
        if held == 0:
            return replace(state, direction=0)
        entry = position.entry_price if position is not None and position.entry_price > 0 else close
        if state.direction != held or math.isnan(state.entry_price):
            unit = sigma if (not math.isnan(sigma) and sigma > 0) else self.params.min_unit
            return ExitState(
                direction=held,
                entry_price=entry,
                extreme=entry,
                unit=max(unit, self.params.min_unit),
                cooldown_until=state.cooldown_until,
                cooldown_direction=state.cooldown_direction,
            )
        extreme = state.extreme if not math.isnan(state.extreme) else entry
        extreme = max(extreme, entry) if held > 0 else min(extreme, entry)
        return replace(state, entry_price=entry, extreme=extreme)
