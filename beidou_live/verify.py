"""M-009: reproduce the last live cycle's model output offline and diff it against ``state.json`` (KILL-027 monitor).

The reproduction builds its inputs through ``beidou_live.inputs`` (the same code the
engine uses), seeds the hold with the state's own contributions and compares:

* contributions - the pure model output per strategy; these must match to the tolerance;
* targets - ``state.last_targets`` are the post-throttle / post-exit / post-guard targets, so a
  difference there is informational unless the cycle record shows none of those acted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from beidou_live.inputs import model_inputs
from beidou_live.ports import MarketData, SignalModel, TargetSet
from beidou_live.state import LiveState


def _diff(reference: Mapping[str, float], candidate: Mapping[str, float]) -> dict[str, float]:
    keys = set(reference) | set(candidate)
    return {key: abs(float(reference.get(key, 0.0)) - float(candidate.get(key, 0.0))) for key in sorted(keys)}


def compare_targets(targets: TargetSet, state: LiveState, tolerance: float = 1e-9) -> dict[str, Any]:
    """Diff a freshly computed ``TargetSet`` against the persisted state of the last cycle."""
    as_of_ms = int(targets.as_of.timestamp() * 1000)
    matched = state.last_bar_ms is not None and as_of_ms == int(state.last_bar_ms)
    contributions: dict[str, dict[str, float]] = {}
    worst_contribution = 0.0
    for strategy, recorded in state.last_contributions.items():
        computed = targets.contributions.get(strategy)
        if computed is None:
            contributions[strategy] = {"<missing>": float("inf")}
            worst_contribution = float("inf")
            continue
        diffs = _diff(recorded, computed)
        contributions[strategy] = {symbol: value for symbol, value in diffs.items() if value > tolerance}
        worst_contribution = max(worst_contribution, max(diffs.values(), default=0.0))
    missing = sorted(set(targets.contributions) - set(state.last_contributions))
    weight_diffs = _diff(state.last_targets, targets.weights)
    worst_weight = max(weight_diffs.values(), default=0.0)
    ok = matched and worst_contribution <= tolerance and not missing
    return {
        "as_of_ms": as_of_ms,
        "state_bar_ms": state.last_bar_ms,
        "bar_matched": matched,
        "max_contribution_diff": worst_contribution,
        "contribution_diffs": contributions,
        "strategies_not_in_state": missing,
        "max_target_diff": worst_weight,
        "target_diffs": {symbol: value for symbol, value in weight_diffs.items() if value > tolerance},
        "tolerance": tolerance,
        "ok": ok,
        "note": (
            "contributions reproduce the last cycle"
            if ok
            else "state.json is from another bar; re-run after the next cycle"
            if not matched
            else "the model no longer reproduces the last cycle's contributions (config, code or data changed)"
        ),
    }


async def verify_live_targets(
    model: SignalModel,
    market: MarketData,
    symbols: Sequence[str],
    interval: str,
    history_bars: int,
    state: LiveState,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    inputs = await model_inputs(market, model, symbols, interval, history_bars)
    targets = model.targets(
        inputs.bars, inputs.funding, previous=state.last_contributions, funding_history=inputs.funding_history
    )
    return {"inputs": inputs.to_dict(), **compare_targets(targets, state, tolerance)}


__all__ = ["compare_targets", "verify_live_targets"]
