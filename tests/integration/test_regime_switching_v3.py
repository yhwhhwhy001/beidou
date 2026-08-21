from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import EnsembleComponent, EnsembleForecast
from beidou_strategy.portfolio.exposure_governor import ExposureGovernor
from beidou_strategy.state.market_state import MarketStateEstimator


def _state(returns: dict[int, float], *, quality: str = "PASS", stress_level: str | None = None):
    features: dict[str, object] = {
        "benchmark_returns": returns,
        "trend_slopes": {horizon: value / 10.0 for horizon, value in returns.items()},
        "breadth": 0.85 if returns[20] > 0 else 0.5,
        "breakout_breadth": 0.8 if returns[20] > 0 else 0.5,
        "volume_expansion": 1.3 if returns[20] > 0 else 1.0,
        "realized_volatility": 0.2,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "data_quality": quality,
    }
    if stress_level:
        features["stress_level"] = stress_level
        features["realized_volatility"] = 1.4
    return MarketStateEstimator().estimate(
        VenueId("BINANCE"),
        InstrumentId("ETHUSDT"),
        features,
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )


def _ensemble() -> EnsembleForecast:
    return EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=0.01,
        expected_return_after_cost=0.01,
        expected_volatility=0.2,
        uncertainty=0.1,
        conflict_score=0.0,
        components=(EnsembleComponent("trend-v3", 1.0, 0.01, 0.01, 0.9, 0.9),),
        model_version="ensemble-v3",
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )


def test_regime_switching_reduces_risk_without_second_state_semantics() -> None:
    governor = ExposureGovernor()
    normal = governor.compute(_state({5: 0.03, 20: 0.10, 50: 0.25}), _ensemble(), account_facts={"equity": 100000.0})
    crisis = governor.compute(
        _state({5: -0.08, 20: -0.20, 50: -0.35}, stress_level="CRISIS"),
        _ensemble(),
        account_facts={"equity": 100000.0},
    )

    assert crisis.target_gross <= normal.target_gross
    assert crisis.target_beta <= normal.target_beta
    assert "STATE_NOT_TRADABLE" in crisis.reason_codes or crisis.target_gross == 0.0
