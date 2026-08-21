from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import EnsembleComponent, EnsembleForecast
from beidou_strategy.portfolio.exposure_governor import ExposureGovernor
from beidou_strategy.state.market_state import MarketStateEstimator


def test_after_cost_negative_edge_blocks_exposure() -> None:
    state = MarketStateEstimator().estimate(
        VenueId("BINANCE"),
        InstrumentId("ETHUSDT"),
        {
            "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
            "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
            "breadth": 0.85,
            "breakout_breadth": 0.8,
            "volume_expansion": 1.3,
            "realized_volatility": 0.2,
            "data_quality": "PASS",
        },
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    forecast = EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=0.0001,
        expected_return_after_cost=-0.001,
        expected_volatility=0.2,
        uncertainty=0.1,
        conflict_score=0.0,
        components=(EnsembleComponent("trend-v3", 1.0, 0.0001, -0.001, 0.9, 0.9),),
        model_version="ensemble-v3",
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    target = ExposureGovernor().compute(state, forecast, account_facts={"equity": 100000.0})

    assert target.target_gross == 0.0
    assert "NO_POSITIVE_EDGE_AFTER_COST" in target.reason_codes
