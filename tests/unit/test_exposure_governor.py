from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import EnsembleComponent, EnsembleForecast
from beidou_strategy.portfolio.exposure_governor import ExposureGovernor
from beidou_strategy.state.market_state import MarketStateEstimator

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _state(*, volatility: float = 0.2, quality: str = "PASS", stress_level: str | None = None):
    features: dict[str, object] = {
        "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
        "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
        "breadth": 0.85,
        "breakout_breadth": 0.80,
        "volume_expansion": 1.3,
        "realized_volatility": volatility,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "data_quality": quality,
    }
    if stress_level:
        features["stress_level"] = stress_level
    return MarketStateEstimator().estimate(VenueId("BINANCE"), InstrumentId("ETHUSDT"), features, timestamp=TIMESTAMP)


def _ensemble(expected_return_after_cost: float | None, *, gross_return: float | None = None) -> EnsembleForecast:
    expected_return = expected_return_after_cost if gross_return is None else gross_return
    return EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=expected_return,
        expected_return_after_cost=expected_return_after_cost,
        expected_volatility=0.2,
        uncertainty=0.1,
        conflict_score=0.0,
        components=(
            EnsembleComponent(
                alpha_id="trend-v3",
                weight=1.0,
                expected_return=expected_return,
                contribution=expected_return_after_cost,
                regime_fit=0.9,
                reliability=0.9,
            ),
        ),
        model_version="ensemble-v3",
        timestamp=TIMESTAMP,
    )


def test_strong_bull_gets_nonzero_beta_and_gross_target() -> None:
    target = ExposureGovernor().compute(_state(), _ensemble(0.01), account_facts={"equity": 100000.0})

    assert target.target_gross > 0.0
    assert target.target_beta > 0.0
    assert target.gross_min <= target.target_gross <= target.gross_max
    assert target.beta_min <= target.target_beta <= target.beta_max
    assert target.target_gross >= abs(target.target_net)


def test_stress_and_quality_downgrade_cannot_increase_risk() -> None:
    normal = ExposureGovernor().compute(_state(), _ensemble(0.01), account_facts={"equity": 100000.0})
    stressed = ExposureGovernor().compute(
        _state(volatility=1.4, quality="PASS", stress_level="CRISIS"),
        _ensemble(0.01),
        account_facts={"equity": 100000.0},
    )
    degraded = ExposureGovernor().compute(
        _state(quality="DEGRADED"), _ensemble(0.01), account_facts={"equity": 100000.0}
    )

    assert stressed.target_gross <= normal.target_gross
    assert stressed.target_beta <= normal.target_beta
    assert degraded.target_gross <= normal.target_gross


def test_unknown_forecast_is_not_verifiable_and_has_zero_risk_target() -> None:
    target = ExposureGovernor().compute(_state(), _ensemble(None), account_facts={"equity": 100000.0})

    assert target.target_gross == 0.0
    assert target.target_net == 0.0
    assert target.target_beta == 0.0
    assert "FORECAST_NOT_VERIFIABLE" in target.reason_codes


def test_cost_shock_does_not_create_new_risk() -> None:
    target = ExposureGovernor().compute(
        _state(), _ensemble(-0.001, gross_return=0.0001), account_facts={"equity": 100000.0}
    )

    assert target.target_gross == 0.0
    assert "NO_POSITIVE_EDGE_AFTER_COST" in target.reason_codes
