from __future__ import annotations

from dataclasses import replace

import pytest

from beidou_shared.types import StrategyId
from beidou_strategy.alpha.breakout import BreakoutAlpha
from beidou_strategy.alpha.forecast import EnsembleFuser
from beidou_strategy.alpha.relative_strength import RelativeStrengthAlpha
from beidou_strategy.alpha.typed_graph import ForecastNode, FusionNode, TypedAlphaGraph


def _contexts() -> tuple[dict[str, object], dict[str, object]]:
    common = {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": {
            "realized_volatility": 0.03,
            "data_quality": "PASS",
            "liquidity_score": 0.9,
        },
        "costs": {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v1",
        },
    }
    rs = {
        **common,
        "features": {
            **common["features"],
            "asset_returns": {5: 0.10, 20: 0.20},
            "benchmark_returns": {5: 0.02, 20: 0.05},
            "universe_returns": {"BTCUSDT": {5: 0.02, 20: 0.05}, "ETHUSDT": {5: 0.10, 20: 0.20}},
            "universe_snapshot_timestamp": "2026-08-21T11:59:00+00:00",
            "universe_source_hash": "universe-v1",
        },
    }
    breakout = {
        **common,
        "features": {
            **common["features"],
            "close": 120.0,
            "high_history": [100.0, 102.0, 105.0, 108.0, 110.0],
            "low_history": [95.0, 96.0, 98.0, 99.0, 100.0],
            "atr": 5.0,
            "volume_ratio": 2.0,
            "breadth": 0.8,
            "breakout_breadth": 0.8,
        },
    }
    return rs, breakout


def test_multi_entry_fusion_is_order_invariant_and_traces_contributions() -> None:
    rs_context, breakout_context = _contexts()
    forecasts = [
        RelativeStrengthAlpha().generate_forecast(rs_context),
        BreakoutAlpha().generate_forecast(breakout_context),
    ]
    fuser = EnsembleFuser()

    first = fuser.fuse(forecasts)
    second = fuser.fuse(list(reversed(forecasts)))

    assert first.forecast_hash == second.forecast_hash
    assert [component.alpha_id for component in first.components] == sorted(
        component.alpha_id for component in first.components
    )
    assert len(first.components) == 2
    assert all(component.contribution is not None for component in first.components)


def test_opposite_forecasts_are_not_first_entry_wins() -> None:
    rs_context, breakout_context = _contexts()
    long_forecast = RelativeStrengthAlpha().generate_forecast(rs_context)
    short_features = dict(breakout_context["features"])
    short_features.update(
        {
            "close": 80.0,
            "high_history": [100.0, 102.0, 105.0, 108.0, 110.0],
            "low_history": [90.0, 92.0, 94.0, 95.0, 96.0],
            "volume_ratio": 2.0,
            "breadth": 0.8,
            "breakout_breadth": 0.8,
        }
    )
    breakout_context["features"] = short_features
    short_forecast = BreakoutAlpha().generate_forecast(breakout_context)

    ensemble = EnsembleFuser().fuse([long_forecast, short_forecast])

    assert ensemble.conflict_score > 0.0
    assert len(ensemble.components) == 2
    assert {component.alpha_id for component in ensemble.components} == {"relative-strength-v3", "breakout-v3"}


def test_cost_shock_is_not_resurrected_by_fusion() -> None:
    rs_context, _breakout_context = _contexts()
    forecast = RelativeStrengthAlpha().generate_forecast(rs_context)
    shocked = replace(forecast, expected_return_after_cost=-abs(forecast.expected_return or 0.001))

    ensemble = EnsembleFuser().fuse([shocked])

    assert ensemble.expected_return_after_cost is None
    assert all(component.weight == 0.0 for component in ensemble.components)


@pytest.mark.asyncio
async def test_typed_graph_uses_the_same_forecast_fuser() -> None:
    rs_context, breakout_context = _contexts()
    rs_forecast = RelativeStrengthAlpha().generate_forecast(rs_context)
    breakout_forecast = BreakoutAlpha().generate_forecast(breakout_context)

    async def rs_node(_context: dict[str, object]):
        return rs_forecast

    async def breakout_node(_context: dict[str, object]):
        return breakout_forecast

    graph = TypedAlphaGraph(StrategyId("alpha-v3"))
    graph.add_node(ForecastNode("rs", rs_node))
    graph.add_node(ForecastNode("breakout", breakout_node))
    graph.add_node(FusionNode("fusion"))
    graph.connect("rs", "fusion")
    graph.connect("breakout", "fusion")

    detailed = await graph._execute_detailed({})
    ensemble = detailed["ensemble_forecast"]

    assert ensemble is not None
    assert ensemble.forecast_hash == EnsembleFuser().fuse([rs_forecast, breakout_forecast]).forecast_hash
