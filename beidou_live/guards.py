"""Minimal guards that protect the validity of the experiment (D-004).  They never add risk.

The two guards that bind the whole book - the per-symbol cap plus the gross cap, and the daily-loss
pause - are defined once in ``beidou_alpha.overlays.exposure`` and called from here, so the backtest
replay and this loop cannot drift.  Only the cycle-level policy lives here: what makes a cycle skip,
what turns ``allow_increase`` off, and what gets recorded as a reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from beidou_alpha.overlays.exposure import clamp_book, hold_or_reduce


@dataclass(frozen=True)
class GuardParams:
    daily_loss_pause: float = -0.05
    stale_bars_max: int = 2
    max_gross: float = 2.0
    max_weight: float = 0.15


@dataclass
class GuardDecision:
    targets: dict[str, float]
    allow_increase: bool = True
    skip_cycle: bool = False
    reasons: list[str] = field(default_factory=list)


def evaluate_guards(
    targets: Mapping[str, float],
    *,
    current_weights: Mapping[str, float],
    kill_switch: bool,
    equity: float,
    day_start_equity: float | None,
    latest_bar_ms: int | None,
    expected_bar_ms: int,
    interval_ms: int,
    params: GuardParams,
) -> GuardDecision:
    decision = GuardDecision(targets=dict(targets))
    if latest_bar_ms is None or expected_bar_ms - latest_bar_ms > params.stale_bars_max * interval_ms:
        decision.skip_cycle = True
        decision.reasons.append("STALE_MARKET_DATA")
        return decision
    if kill_switch:
        decision.allow_increase = False
        decision.reasons.append("KILL_SWITCH")
    if day_start_equity and equity > 0 and equity / day_start_equity - 1.0 < params.daily_loss_pause:
        decision.allow_increase = False
        decision.reasons.append("DAILY_LOSS_PAUSE")
    symbols = list(decision.targets)
    values = np.array([float(decision.targets[symbol]) for symbol in symbols], dtype=float)
    capped, gross_capped = clamp_book(values, params.max_weight, params.max_gross)
    if gross_capped:
        decision.reasons.append("GROSS_CAPPED")
    if not decision.allow_increase:
        held = np.array([float(current_weights.get(symbol, 0.0)) for symbol in symbols], dtype=float)
        capped = hold_or_reduce(capped, held)
    decision.targets = dict(zip(symbols, (float(value) for value in capped), strict=True))
    return decision
