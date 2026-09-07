"""Exit overlay: stop-loss, trailing stop and take-profit in units of daily volatility.

Evaluated at bar close on closed bars only (never intra-bar; D-012).  Every
threshold is ``k`` daily standard deviations, where the unit is the symbol's
EWMA volatility scaled to one day - fixed at entry by default, or re-read
from the current bar under ``unit_mode="current"`` (EXP-EX3), which is the
only difference between the two modes - so the same ``k`` means the
same statistical distance on BTC and on a meme coin.  After an exit the same
direction is suppressed for ``cooldown_bars``; the opposite direction may
enter immediately.  Same-direction resizes keep the original entry.

``exit_step`` is the single source of truth for one symbol-bar; ``apply_exits``
runs it over a whole weight frame (vectorised across symbols, looped over
bars) and is what backtests use; the live loop calls ``exit_step`` per symbol
with the venue's entry price as the reference.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import ewm_vol

STOP_LOSS = "STOP_LOSS"
TRAILING_STOP = "TRAILING_STOP"
TAKE_PROFIT = "TAKE_PROFIT"
COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class ExitParams:
    stop_loss: float = 0.0  # k x sigma_1d adverse move from entry; 0 disables
    trailing_stop: float = 0.0  # k x sigma_1d retracement from the favourable extreme; 0 disables
    take_profit: float = 0.0  # k x sigma_1d favourable move from entry; 0 disables
    cooldown_bars: int = 24
    vol_halflife: int = 48
    bars_per_day: int = 24
    min_unit: float = 0.005  # floor on sigma_1d (fraction) so a dead-quiet series cannot make a 1-tick stop
    unit_mode: str = "entry"  # entry | current: which sigma_1d the k-units are measured in (EXP-EX3)

    def __post_init__(self) -> None:
        if min(self.stop_loss, self.trailing_stop, self.take_profit) < 0:
            raise ValueError("exit thresholds must be >= 0")
        if self.cooldown_bars < 0 or self.vol_halflife <= 0 or self.bars_per_day <= 0 or self.min_unit <= 0:
            raise ValueError("invalid exit parameters")
        if self.unit_mode not in {"entry", "current"}:
            raise ValueError("unit_mode must be 'entry' or 'current'")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ExitParams:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)

    @property
    def enabled(self) -> bool:
        return max(self.stop_loss, self.trailing_stop, self.take_profit) > 0


@dataclass(frozen=True)
class ExitState:
    """Per-symbol overlay state.  ``bar`` fields are opaque monotone integers (bar index or bar open ms)."""

    direction: int = 0  # sign of the held position; 0 = flat
    entry_price: float = math.nan
    extreme: float = math.nan  # best close since entry (max for longs, min for shorts)
    unit: float = math.nan  # sigma_1d (fraction) fixed at entry
    cooldown_until: int = -1  # exclusive
    cooldown_direction: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry_price": None if math.isnan(self.entry_price) else self.entry_price,
            "extreme": None if math.isnan(self.extreme) else self.extreme,
            "unit": None if math.isnan(self.unit) else self.unit,
            "cooldown_until": self.cooldown_until,
            "cooldown_direction": self.cooldown_direction,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExitState:
        def number(key: str) -> float:
            value = payload.get(key)
            return math.nan if value is None else float(value)

        return cls(
            direction=int(payload.get("direction", 0) or 0),
            entry_price=number("entry_price"),
            extreme=number("extreme"),
            unit=number("unit"),
            cooldown_until=int(payload.get("cooldown_until", -1)),
            cooldown_direction=int(payload.get("cooldown_direction", 0) or 0),
        )


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _unit_price(state: ExitState, sigma_1d: float, params: ExitParams) -> float:
    """The price distance one k-unit is worth on this bar.

    ``entry``: the sigma frozen when the position opened (D-012's definition - the same k is the same
    statistical distance, measured once).  ``current``: this bar's sigma, so the thresholds breathe with
    volatility - tighter when the market calms, wider when it wakes - which is what the Chandelier / ATR
    family does by default and what EXP-EX3 tests.  A missing current sigma falls back to the entry unit.

    Both modes multiply by ``state.entry_price``, never the current price: every comparison this feeds
    (``adverse``, ``favourable``, ``retrace``) has a numerator that is a price difference measured FROM
    the entry anchor, so dividing by ``sigma * entry_price`` yields "the move from entry, expressed in
    daily sigmas".  Using the current price in the denominator would silently mix two reference points -
    the numerator anchored at entry, the denominator anchored at now - and change what ``k`` means from
    bar to bar even under ``unit_mode="entry"``.
    """
    unit = state.unit
    if params.unit_mode == "current" and not math.isnan(sigma_1d) and sigma_1d > 0:
        unit = sigma_1d
    return max(unit, params.min_unit) * state.entry_price


def exit_step(
    state: ExitState,
    target: float,
    price: float,
    sigma_1d: float,
    bar: int,
    params: ExitParams,
    *,
    bar_step: int = 1,
) -> tuple[ExitState, float, str]:
    """One symbol, one closed bar: (new state, weight to hold from here, reason).

    ``target`` is the model's weight (already vol-targeted), ``price`` the bar
    close, ``sigma_1d`` the current daily vol (fraction).  ``bar_step`` is the
    increment of ``bar`` per bar (1 for indices, interval_ms for timestamps).
    """
    if not params.enabled:
        return replace(state, direction=_sign(target)), target, ""
    if math.isnan(target):
        target = 0.0
    wanted = _sign(target)
    held = state.direction
    if held != 0 and not math.isnan(state.entry_price) and price > 0:
        extreme = max(state.extreme, price) if held > 0 else min(state.extreme, price)
        if math.isnan(extreme):
            extreme = price
        unit_price = _unit_price(state, sigma_1d, params)
        adverse = (state.entry_price - price) * held / unit_price
        favourable = -adverse
        retrace = (extreme - price) * held / unit_price
        reason = ""
        if params.stop_loss > 0 and adverse >= params.stop_loss:
            reason = STOP_LOSS
        elif params.trailing_stop > 0 and retrace >= params.trailing_stop:
            reason = TRAILING_STOP
        elif params.take_profit > 0 and favourable >= params.take_profit:
            reason = TAKE_PROFIT
        if reason:
            cooled = ExitState(
                direction=0, cooldown_until=bar + params.cooldown_bars * bar_step, cooldown_direction=held
            )
            return cooled, 0.0, reason
        if wanted == held:
            return replace(state, extreme=extreme), target, ""
        if wanted == 0:
            return ExitState(cooldown_until=state.cooldown_until, cooldown_direction=state.cooldown_direction), 0.0, ""
        # sign flip: a brand-new position in the other direction
        return _enter(state, wanted, price, sigma_1d, params), target, ""
    if wanted == 0:
        return replace(state, direction=0), 0.0, ""
    if bar < state.cooldown_until and wanted == state.cooldown_direction:
        return replace(state, direction=0), 0.0, COOLDOWN
    return _enter(state, wanted, price, sigma_1d, params), target, ""


def _enter(state: ExitState, direction: int, price: float, sigma_1d: float, params: ExitParams) -> ExitState:
    unit = sigma_1d if (not math.isnan(sigma_1d) and sigma_1d > 0) else params.min_unit
    return ExitState(
        direction=direction,
        entry_price=price,
        extreme=price,
        unit=max(unit, params.min_unit),
        cooldown_until=state.cooldown_until,
        cooldown_direction=state.cooldown_direction,
    )


def daily_vol(close: pd.DataFrame, params: ExitParams) -> pd.DataFrame:
    """EWMA per-bar vol scaled to one day (fraction), floored at ``min_unit``."""
    return (ewm_vol(close, halflife=params.vol_halflife) * math.sqrt(params.bars_per_day)).clip(lower=params.min_unit)


@dataclass
class ExitResult:
    weights: pd.DataFrame
    events: pd.DataFrame  # columns: time, symbol, rule, entry_price, price, units, direction

    def summary(self) -> dict[str, Any]:
        counts = self.events["rule"].value_counts().to_dict() if len(self.events) else {}
        return {"exits": len(self.events), "by_rule": {str(k): int(v) for k, v in counts.items()}}


def apply_exits(
    weights: pd.DataFrame, close: pd.DataFrame, params: ExitParams, *, sigma_1d: pd.DataFrame | None = None
) -> ExitResult:
    """Run ``exit_step`` over a decision-time weight frame (index = decision bars, columns = symbols)."""
    empty_events = pd.DataFrame(columns=["time", "symbol", "rule", "entry_price", "price", "units", "direction"])
    if not params.enabled:
        return ExitResult(weights.copy(), empty_events)
    aligned_close = close.reindex(index=weights.index, columns=weights.columns)
    vol = (sigma_1d if sigma_1d is not None else daily_vol(close, params)).reindex(
        index=weights.index, columns=weights.columns
    )
    values = weights.to_numpy(dtype=float)
    prices = aligned_close.to_numpy(dtype=float)
    vols = vol.to_numpy(dtype=float)
    out = np.full_like(values, np.nan)
    states = [ExitState() for _ in weights.columns]
    events: list[dict[str, Any]] = []
    for t in range(values.shape[0]):
        row = values[t]
        if np.all(np.isnan(row)):
            continue
        for j, state in enumerate(states):
            before = state
            new_state, weight, reason = exit_step(state, row[j], prices[t, j], vols[t, j], t, params)
            states[j] = new_state
            out[t, j] = weight
            if reason and reason != COOLDOWN:
                unit_price = _unit_price(before, vols[t, j], params)
                events.append(
                    {
                        "time": weights.index[t],
                        "symbol": weights.columns[j],
                        "rule": reason,
                        "entry_price": before.entry_price,
                        "price": prices[t, j],
                        "units": float((prices[t, j] - before.entry_price) * before.direction / unit_price),
                        "direction": before.direction,
                    }
                )
    frame = pd.DataFrame(out, index=weights.index, columns=weights.columns)
    return ExitResult(frame, pd.DataFrame(events) if events else empty_events)
