from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import EnsembleComponent, EnsembleForecast
from beidou_strategy.portfolio.exposure_governor import ExposureGovernor
from beidou_strategy.portfolio.optimizer import ActivePortfolioOptimizer, PortfolioOptimizationInput
from beidou_strategy.state.market_state import MarketStateEstimator

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _state(*, range_state: bool = False, stress_level: str | None = None):
    returns = {5: 0.001, 20: -0.001, 50: 0.002} if range_state else {5: 0.03, 20: 0.10, 50: 0.25}
    features: dict[str, object] = {
        "benchmark_returns": returns,
        "trend_slopes": {horizon: value / 10.0 for horizon, value in returns.items()},
        "breadth": 0.5 if range_state else 0.85,
        "breakout_breadth": 0.5 if range_state else 0.80,
        "volume_expansion": 1.0 if range_state else 1.3,
        "realized_volatility": 0.2,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "data_quality": "PASS",
    }
    if stress_level:
        features["stress_level"] = stress_level
        features["realized_volatility"] = 1.4
    return MarketStateEstimator().estimate(VenueId("BINANCE"), InstrumentId("ETHUSDT"), features, timestamp=TIMESTAMP)


def _ensemble(value: float) -> EnsembleForecast:
    return EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=value,
        expected_return_after_cost=value,
        expected_volatility=0.2,
        uncertainty=0.1,
        conflict_score=0.0,
        components=(EnsembleComponent("trend-v3", 1.0, value, value, 0.9, 0.9),),
        model_version="ensemble-v3",
        timestamp=TIMESTAMP,
    )


def _optimizer_input(target):
    return PortfolioOptimizationInput(
        forecasts={"ETHUSDT": _ensemble(0.01)},
        current_weights={"ETHUSDT": 0.0},
        covariance={"ETHUSDT": {"ETHUSDT": 0.04}},
        asset_betas={"ETHUSDT": 1.0},
        exposure_target=target,
        account_equity=100000.0,
        fee_costs={"ETHUSDT": 2.0},
        slippage_costs={"ETHUSDT": 1.0},
        funding_costs={"ETHUSDT": 0.5},
        liquidity_limits={"ETHUSDT": 0.8},
        capacity_limits={"ETHUSDT": 0.8},
        min_notional={"ETHUSDT": 10.0},
        step_sizes={"ETHUSDT": 0.001},
        tick_sizes={"ETHUSDT": 0.01},
        policy_version="portfolio-v3-policy-v1",
    )


def test_bull_state_flows_to_nonzero_portfolio_target() -> None:
    governor = ExposureGovernor()
    target = governor.compute(_state(), _ensemble(0.01), account_facts={"equity": 100000.0})
    result = ActivePortfolioOptimizer().optimize(_optimizer_input(target))

    assert target.target_beta > 0.0
    assert target.target_gross > 0.0
    assert result.weights
    assert result.gross_exposure >= abs(result.net_exposure)


def test_range_and_stress_states_reduce_portfolio_risk() -> None:
    governor = ExposureGovernor()
    bull = governor.compute(_state(), _ensemble(0.01), account_facts={"equity": 100000.0})
    range_target = governor.compute(_state(range_state=True), _ensemble(0.01), account_facts={"equity": 100000.0})
    stress_target = governor.compute(_state(stress_level="CRISIS"), _ensemble(0.01), account_facts={"equity": 100000.0})

    assert range_target.target_gross <= bull.target_gross
    assert range_target.target_beta <= bull.target_beta
    assert stress_target.target_gross == 0.0
