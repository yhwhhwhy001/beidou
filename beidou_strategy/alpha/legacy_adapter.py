"""Legacy AlphaComponent adapters for the typed execution graph.

The existing component implementations are retained as model code, but their
outputs cross one explicit boundary before they can affect execution:

* ENTRY -> :class:`EntryProposal`
* FILTER -> :class:`FilterResult` (never a direction)
* EXIT -> a reduce-only legacy signal, kept outside entry fusion

This module is deliberately one-way.  It does not provide a reverse adapter
from typed results back into an independent trading decision path.
"""

from __future__ import annotations

from typing import Any

from beidou_shared.types import InstrumentId, OrderSide, StrategyId, VenueId

from . import AlphaComponent, AlphaSignal, SignalDirection
from .contracts import EntryProposal, FilterDecision, FilterResult
from .typed_graph import EntryNode, ExitNode, FilterNode, FusionNode, TypedAlphaGraph


def _identity(context: dict[str, Any], signal: AlphaSignal) -> tuple[StrategyId, InstrumentId, VenueId]:
    return (
        signal.strategy_id,
        signal.instrument_id or context.get("instrument_id", InstrumentId("UNKNOWN")),
        signal.venue_id or context.get("venue_id", VenueId("UNKNOWN")),
    )


def _as_signal(value: Any, component: AlphaComponent) -> AlphaSignal:
    if not isinstance(value, AlphaSignal):
        raise TypeError(f"{component.component_id} returned {type(value).__name__}, expected AlphaSignal")
    if value.component_type != component.component_type:
        raise TypeError(
            f"{component.component_id} returned component type {value.component_type!r}; "
            f"expected {component.component_type!r}"
        )
    return value


def make_entry_node(component: AlphaComponent) -> EntryNode:
    """Wrap one legacy ENTRY component without exposing direction downstream."""

    async def evaluate(context: dict[str, Any]) -> EntryProposal:
        signal = _as_signal(await component.generate(context), component)
        strategy_id, instrument_id, venue_id = _identity(context, signal)
        side = {
            SignalDirection.LONG: OrderSide.BUY,
            SignalDirection.SHORT: OrderSide.SELL,
        }.get(signal.direction)
        strength = max(0.0, min(1.0, float(signal.strength)))
        confidence = max(0.0, min(1.0, float(signal.confidence)))
        if side is None:
            strength = 0.0
            confidence = 0.0
        return EntryProposal(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            side=side,
            strength=strength,
            confidence=confidence,
            model_version=signal.model_version,
            metadata={
                "legacy_component_id": component.component_id,
                "legacy_direction": signal.direction.value,
                "legacy_metadata": dict(signal.metadata),
            },
        )

    return EntryNode(
        node_id=component.component_id,
        entry_fn=evaluate,
        factor_version=str(component.version),
        model_version=str(component.version),
    )


def _filter_decision(signal: AlphaSignal) -> FilterDecision:
    raw = str(signal.metadata.get("filter_decision", "")).upper()
    if raw in FilterDecision._value2member_map_:
        return FilterDecision(raw)
    reason = str(signal.metadata.get("reason", "")).lower()
    if signal.direction == SignalDirection.NO_ACTION and signal.confidence >= 0.8:
        return FilterDecision.VETO
    if any(token in reason for token in ("veto", "extreme", "dead")):
        return FilterDecision.VETO
    if any(token in reason for token in ("degrade", "elevated", "low_volume")):
        return FilterDecision.DEGRADE
    return FilterDecision.ACCEPT


def make_filter_node(component: AlphaComponent) -> FilterNode:
    """Wrap one legacy FILTER and erase its accidental LONG/SHORT output."""

    async def evaluate(context: dict[str, Any], _entry: EntryProposal | None) -> FilterResult:
        signal = _as_signal(await component.generate(context), component)
        decision = _filter_decision(signal)
        confidence = max(0.0, min(1.0, float(signal.confidence)))
        strength = max(0.0, min(1.0, float(signal.strength)))
        if decision is FilterDecision.VETO:
            confidence_multiplier = 0.0
            size_multiplier = 0.0
            reasons = [str(signal.metadata.get("reason", "filter_veto"))]
        elif decision is FilterDecision.DEGRADE:
            confidence_multiplier = confidence
            # Preserve the legacy degrade intent while keeping the value a
            # typed multiplier.  A missing/zero strength remains conservative.
            size_multiplier = max(0.0, min(1.0, strength / 0.2))
            reasons = [str(signal.metadata.get("reason", "filter_degrade"))]
        else:
            confidence_multiplier = 1.0
            size_multiplier = 1.0
            reasons = []
        return FilterResult(
            decision=decision,
            confidence_multiplier=confidence_multiplier,
            size_multiplier=size_multiplier,
            reason_codes=reasons,
            component_id=component.component_id,
            metadata={
                "legacy_component_id": component.component_id,
                "legacy_direction_discarded": signal.direction.value,
                "legacy_metadata": dict(signal.metadata),
            },
        )

    return FilterNode(node_id=component.component_id, filter_fn=evaluate, is_mandatory=True)


def make_exit_node(component: AlphaComponent) -> ExitNode:
    """Wrap one legacy EXIT and reject any accidental risk-increasing direction."""

    async def evaluate(context: dict[str, Any]) -> AlphaSignal | None:
        signal = _as_signal(await component.generate(context), component)
        if signal.direction not in {SignalDirection.FLAT, SignalDirection.NO_ACTION}:
            raise ValueError(f"{component.component_id} emitted risk-increasing exit direction")
        return signal

    return ExitNode(node_id=component.component_id, exit_fn=evaluate)


def build_typed_graph(
    strategy_id: StrategyId,
    components: dict[str, AlphaComponent],
    entry_ids: set[str],
    filter_ids: set[str],
    exit_ids: set[str],
) -> TypedAlphaGraph:
    """Build the single typed graph for the currently evidence-approved factors."""

    graph = TypedAlphaGraph(strategy_id=strategy_id)
    for component_id in sorted(components):
        component = components[component_id]
        if component_id in entry_ids:
            graph.add_node(make_entry_node(component))
        elif component_id in filter_ids:
            graph.add_node(make_filter_node(component))
        elif component_id in exit_ids:
            graph.add_node(make_exit_node(component))

    fusion_id = "typed_fusion_v1"
    graph.add_node(FusionNode(fusion_id))
    entries = sorted(set(components) & entry_ids)
    filters = sorted(set(components) & filter_ids)
    exits = sorted(set(components) & exit_ids)

    for entry_id in entries:
        graph.connect(entry_id, fusion_id)
        for filter_id in filters:
            graph.connect(entry_id, filter_id)
        if not filters:
            for exit_id in exits:
                graph.connect(entry_id, exit_id)
    for filter_id in filters:
        graph.connect(filter_id, fusion_id)
        for exit_id in exits:
            graph.connect(filter_id, exit_id)
    return graph
