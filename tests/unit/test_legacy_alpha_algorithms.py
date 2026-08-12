from __future__ import annotations

import math

import pytest

from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import (
    AlphaComponent,
    AlphaComponentType,
    AlphaSignal,
    SignalDirection,
)
from beidou_strategy.alpha.contracts import FilterDecision
from beidou_strategy.alpha.legacy_adapter import (
    build_typed_graph,
    make_entry_node,
    make_exit_node,
    make_filter_node,
)
from beidou_strategy.alpha.mean_reversion import MeanReversionEngine, MultiPeriodMomentum


class _StaticComponent(AlphaComponent):
    def __init__(self, component_id: str, component_type: AlphaComponentType, signal: object) -> None:
        super().__init__(component_type, component_id, SchemaVersion("1.0"))
        self.signal = signal

    async def generate(self, context):
        return self.signal

    def validate(self) -> bool:
        return True


def _signal(
    component_type: AlphaComponentType,
    direction: SignalDirection,
    *,
    strength: float = 0.5,
    confidence: float = 0.7,
    metadata: dict | None = None,
) -> AlphaSignal:
    return AlphaSignal(
        strategy_id=StrategyId("strategy"),
        component_type=component_type,
        direction=direction,
        strength=strength,
        confidence=confidence,
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        model_version=SchemaVersion("1.0"),
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_legacy_entry_adapter_clamps_values_and_erases_no_action_direction() -> None:
    long_component = _StaticComponent(
        "entry",
        AlphaComponentType.ENTRY,
        _signal(AlphaComponentType.ENTRY, SignalDirection.LONG, strength=2.0, confidence=-1.0),
    )
    proposal = (await make_entry_node(long_component).execute({}, {})).data
    assert proposal.side is OrderSide.BUY
    assert proposal.strength == 1.0
    assert proposal.confidence == 0.0

    idle_component = _StaticComponent(
        "idle",
        AlphaComponentType.ENTRY,
        _signal(AlphaComponentType.ENTRY, SignalDirection.NO_ACTION, strength=0.9, confidence=0.9),
    )
    idle = (await make_entry_node(idle_component).execute({}, {})).data
    assert idle.side is None
    assert idle.strength == idle.confidence == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "direction", "expected"),
    [
        ({"filter_decision": "VETO"}, SignalDirection.LONG, FilterDecision.VETO),
        ({"reason": "elevated_vol"}, SignalDirection.SHORT, FilterDecision.DEGRADE),
        ({}, SignalDirection.LONG, FilterDecision.ACCEPT),
    ],
)
async def test_legacy_filter_adapter_discards_direction(metadata, direction, expected) -> None:
    component = _StaticComponent(
        f"filter-{expected.value}",
        AlphaComponentType.FILTER,
        _signal(AlphaComponentType.FILTER, direction, strength=0.1, confidence=0.8, metadata=metadata),
    )
    result = (await make_filter_node(component).execute({}, {})).data
    assert result.decision is expected
    assert result.metadata["legacy_direction_discarded"] == direction.value
    if expected is FilterDecision.VETO:
        assert result.size_multiplier == 0.0
    elif expected is FilterDecision.DEGRADE:
        assert result.size_multiplier == 0.5


@pytest.mark.asyncio
async def test_legacy_exit_adapter_rejects_risk_increasing_signal_and_wrong_type() -> None:
    unsafe = _StaticComponent(
        "exit",
        AlphaComponentType.EXIT,
        _signal(AlphaComponentType.EXIT, SignalDirection.LONG),
    )
    blocked = await make_exit_node(unsafe).execute({}, {})
    assert blocked.dq_tier.value == "BLOCK"
    assert "risk-increasing" in blocked.metadata["error"]

    wrong = _StaticComponent("wrong", AlphaComponentType.ENTRY, {"direction": "LONG"})
    output = await make_entry_node(wrong).execute({}, {})
    assert output.dq_tier.value == "BLOCK"


def test_build_typed_graph_has_deterministic_entry_filter_exit_wiring() -> None:
    components = {
        "entry": _StaticComponent(
            "entry", AlphaComponentType.ENTRY, _signal(AlphaComponentType.ENTRY, SignalDirection.LONG)
        ),
        "filter": _StaticComponent(
            "filter", AlphaComponentType.FILTER, _signal(AlphaComponentType.FILTER, SignalDirection.NO_ACTION)
        ),
        "exit": _StaticComponent(
            "exit", AlphaComponentType.EXIT, _signal(AlphaComponentType.EXIT, SignalDirection.NO_ACTION)
        ),
    }
    graph = build_typed_graph(
        StrategyId("strategy"),
        components,
        {"entry"},
        {"filter"},
        {"exit"},
    )
    assert graph.topological_order() == ["entry", "filter", "typed_fusion_v1", "exit"]


def test_mean_reversion_half_life_uses_stationary_ar_coefficient() -> None:
    log_prices = [math.log(100.0) + 0.1]
    center = math.log(100.0)
    for _ in range(39):
        log_prices.append(center + 0.8 * (log_prices[-1] - center))
    prices = [math.exp(value) for value in log_prices]

    half_life = MeanReversionEngine().estimate_half_life(prices)

    assert half_life == pytest.approx(-math.log(2) / math.log(0.8), rel=1e-5)
    assert half_life > 0
    explosive_logs = [center + 0.01]
    for _ in range(29):
        explosive_logs.append(center + 1.1 * (explosive_logs[-1] - center))
    assert math.isinf(MeanReversionEngine().estimate_half_life([math.exp(value) for value in explosive_logs]))


def test_mean_reversion_and_momentum_fail_closed_on_invalid_inputs() -> None:
    engine = MeanReversionEngine(z_threshold=1.0)
    prices = [100.0 + (index % 3) for index in range(30)]
    invalid = engine.evaluate(100.0, prices, float("nan"), 1.0)
    assert invalid.signal_direction == "NO_ACTION"
    assert not invalid.regime_allowed and not invalid.cost_viable
    assert engine.compute_z_score(100.0, [0.0] * 30) == 0.0

    with pytest.raises(ValueError, match="periods"):
        MultiPeriodMomentum([0])
    assert MultiPeriodMomentum([2]).evaluate([100.0, -1.0, 101.0], 0.1).filter_decision == "VETO"


def test_momentum_filter_covers_accept_degrade_and_volatility_veto() -> None:
    momentum = MultiPeriodMomentum([2, 3, 4])
    trending = [100.0 * (1.02**index) for index in range(8)]
    flat = [100.0] * 8

    assert momentum.evaluate(trending, 0.02).filter_decision == "ACCEPT"
    assert momentum.evaluate(flat, 0.02).filter_decision == "DEGRADE"
    veto = momentum.evaluate(trending, 0.6)
    assert veto.filter_decision == "VETO"
    assert veto.volatility_override
