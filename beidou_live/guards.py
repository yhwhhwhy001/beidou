"""Minimal guards that protect the validity of the experiment (D-004).  They never add risk."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


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
    clamped = {
        symbol: max(-params.max_weight, min(params.max_weight, float(weight)))
        for symbol, weight in decision.targets.items()
    }
    gross = sum(abs(weight) for weight in clamped.values())
    if gross > params.max_gross > 0:
        factor = params.max_gross / gross
        clamped = {symbol: weight * factor for symbol, weight in clamped.items()}
        decision.reasons.append("GROSS_CAPPED")
    if not decision.allow_increase:
        clamped = {symbol: _no_increase(weight, current_weights.get(symbol, 0.0)) for symbol, weight in clamped.items()}
    decision.targets = clamped
    return decision


def _no_increase(target: float, current: float) -> float:
    """Only allow moves toward zero: same sign and smaller magnitude, or flat."""
    if current == 0.0 or target == 0.0 or (target > 0) != (current > 0):
        return 0.0
    return target if abs(target) < abs(current) else current
