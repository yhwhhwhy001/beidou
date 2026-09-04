"""M-011: reproduce the last live cycle's model output offline and diff it against ``state.json`` (KILL-027 monitor).

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
from beidou_live.state import LiveState, StateStore


def _diff(reference: Mapping[str, float], candidate: Mapping[str, float]) -> dict[str, float]:
    keys = set(reference) | set(candidate)
    return {key: abs(float(reference.get(key, 0.0)) - float(candidate.get(key, 0.0))) for key in sorted(keys)}


def compare_targets(
    targets: TargetSet,
    state: LiveState,
    tolerance: float = 1e-9,
    recorded_as_of_ms: int | None = None,
) -> dict[str, Any]:
    """Diff a freshly computed ``TargetSet`` against the persisted state of the last cycle.

    ``recorded_as_of_ms`` is the last cycle's ``as_of_ms`` — the bar the *data* was
    stamped with.  Prefer it over ``state.last_bar_ms``, which is the bar the loop
    *labelled* the cycle with from the host clock: when the two disagree the host
    clock has drifted away from the venue's, and comparing against the label would
    report a spurious mismatch for a cycle that in fact reproduces.
    """
    as_of_ms = int(targets.as_of.timestamp() * 1000)
    reference = recorded_as_of_ms if recorded_as_of_ms is not None else state.last_bar_ms
    matched = reference is not None and as_of_ms == int(reference)
    label_skew = (
        None
        if recorded_as_of_ms is None or state.last_bar_ms is None
        else int(recorded_as_of_ms) - int(state.last_bar_ms)
    )
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
        "recorded_as_of_ms": recorded_as_of_ms,
        "bar_label_skew_ms": label_skew,
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
        "clock_note": (
            None
            if not label_skew
            else f"the cycle was labelled {label_skew / 3_600_000:+.2f} h away from its own data: the host clock has "
            "drifted from the venue's, so cycles.jsonl `bar` and heartbeat `at` are wrong (the trading is not)"
        ),
    }


def last_cycle(store: StateStore) -> Mapping[str, Any] | None:
    """The newest non-dry-run cycle record, or ``None`` when the loop has not completed one."""
    for record in reversed(store.read_jsonl(store.cycles_path)):
        if not record.get("dry_run"):
            return record
    return None


def last_recorded_as_of_ms(store: StateStore) -> int | None:
    """``as_of_ms`` of the newest non-dry-run cycle: the bar the data carried, independent of the host clock."""
    for record in reversed(store.read_jsonl(store.cycles_path)):
        if record.get("dry_run"):
            continue
        value = record.get("as_of_ms")
        if isinstance(value, int | float):
            return int(value)
    return None


def cycle_clock(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the last cycle knew about the host clock: its measured skew, and whether income ingestion was skipped."""
    if record is None:
        return {"skew_ms": None, "beyond_tolerance": False, "income_skipped_ms": None}
    clock = record.get("clock") or {}
    flows = record.get("external_flows") or {}
    return {
        "skew_ms": clock.get("skew_ms"),
        "beyond_tolerance": bool(clock.get("beyond_tolerance")),
        # set when the income watermark was ahead of the clock, so that cycle attributed nothing (not a fault)
        "income_skipped_ms": flows.get("clock_skew_ms"),
    }


async def verify_live_targets(
    model: SignalModel,
    market: MarketData,
    symbols: Sequence[str],
    interval: str,
    history_bars: int,
    state: LiveState,
    tolerance: float = 1e-9,
    recorded_as_of_ms: int | None = None,
) -> dict[str, Any]:
    inputs = await model_inputs(market, model, symbols, interval, history_bars)
    targets = model.targets(
        inputs.bars, inputs.funding, previous=state.last_contributions, funding_history=inputs.funding_history
    )
    return {"inputs": inputs.to_dict(), **compare_targets(targets, state, tolerance, recorded_as_of_ms)}


__all__ = ["compare_targets", "cycle_clock", "last_cycle", "last_recorded_as_of_ms", "verify_live_targets"]
