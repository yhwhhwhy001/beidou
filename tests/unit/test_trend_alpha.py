"""Alpha V3 A2 forecast contract and formal trend-entry tests."""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_shared.types import InstrumentId, OrderSide, StrategyId, VenueId
from beidou_strategy.alpha.contracts import AlphaForecast, EnsembleComponent, EnsembleForecast
from beidou_strategy.alpha.mean_reversion import MeanReversionAlpha
from beidou_strategy.alpha.trend import TrendAlpha

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _trend_context(*, with_costs: bool = True, future_return: float | None = None) -> dict[str, object]:
    context: dict[str, object] = {
        "instrument_id": InstrumentId("ETHUSDT"),
        "venue_id": VenueId("BINANCE"),
        "strategy_id": StrategyId("trend-v3"),
        "timestamp": TIMESTAMP,
        "features": {
            "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
            "trend_slopes": {5: 0.003, 20: 0.010, 50: 0.025},
            "breadth": 0.85,
            "breakout_breadth": 0.80,
            "volume_expansion": 1.30,
            "realized_volatility": 0.20,
            "liquidity_score": 0.90,
            "market_beta": 0.20,
            "data_quality": "PASS",
            "feature_hash": "feature-v3-bull",
        },
    }
    if with_costs:
        context["costs"] = {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v3-1",
        }
    if future_return is not None:
        context["future_return"] = future_return
    return context


def test_alpha_forecast_contract_exposes_costs_horizon_uncertainty_and_hashes() -> None:
    forecast = TrendAlpha().generate_forecast(_trend_context())

    assert isinstance(forecast, AlphaForecast)
    assert forecast.expected_return is not None
    assert forecast.expected_return_after_cost is not None
    assert forecast.horizon_seconds > 0
    assert 0.0 <= forecast.uncertainty <= 1.0
    assert forecast.cost_breakdown["expected_fee_bps"] == 2.0
    assert forecast.cost_source_hash == "cost-v3-1"
    assert forecast.cost_verifiable
    assert forecast.forecast_hash
    assert forecast.market_beta is not None
    assert forecast.policy_version
    assert forecast.feature_hash == "feature-v3-bull"


def test_unknown_cost_is_not_converted_to_zero() -> None:
    forecast = TrendAlpha().generate_forecast(_trend_context(with_costs=False))

    assert forecast.expected_return is not None
    assert forecast.expected_return_after_cost is None
    assert not forecast.cost_verifiable


def test_trend_is_a_directional_entry_without_mean_reversion_dependency() -> None:
    forecast = TrendAlpha().generate_forecast(_trend_context())

    assert forecast.side is OrderSide.BUY
    assert forecast.expected_return is not None and forecast.expected_return > 0
    assert forecast.raw_score > 0
    assert forecast.alpha_id == "trend_v3"


def test_future_return_does_not_change_trend_forecast() -> None:
    first = TrendAlpha().generate_forecast(_trend_context(future_return=-0.90))
    second = TrendAlpha().generate_forecast(_trend_context(future_return=0.90))

    assert first.forecast_hash == second.forecast_hash
    assert first.raw_score == second.raw_score


def test_mean_reversion_can_emit_the_same_forecast_contract() -> None:
    prices = [100.0 + (index % 4) * 0.1 for index in range(60)]
    prices[-1] = 102.0
    context = {
        "instrument_id": InstrumentId("ETHUSDT"),
        "venue_id": VenueId("BINANCE"),
        "strategy_id": StrategyId("mr-v3"),
        "timestamp": TIMESTAMP,
        "features": {
            "prices": prices,
            "close": prices[-1],
            "realized_volatility": 0.20,
            "data_quality": "PASS",
        },
        "costs": {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v3-1",
        },
    }

    forecast = MeanReversionAlpha().generate_forecast(context)

    assert isinstance(forecast, AlphaForecast)
    assert forecast.alpha_id == "mean_reversion_v3"


def test_ensemble_contract_retains_component_contributions() -> None:
    component = EnsembleComponent(
        alpha_id="trend_v3",
        weight=1.0,
        expected_return=0.02,
        contribution=0.02,
        regime_fit=0.9,
        reliability=0.8,
    )
    ensemble = EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=0.02,
        expected_return_after_cost=0.015,
        expected_volatility=0.20,
        uncertainty=0.10,
        conflict_score=0.0,
        components=(component,),
        model_version="ensemble-v3",
        policy_version="ensemble-policy-v1",
        timestamp=TIMESTAMP,
    )

    assert ensemble.components[0].contribution == 0.02
    assert ensemble.forecast_hash
