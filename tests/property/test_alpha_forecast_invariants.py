from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import AlphaForecast


@given(raw_score=st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False))
def test_alpha_forecast_hash_is_deterministic_for_finite_inputs(raw_score: float) -> None:
    kwargs = {
        "alpha_id": "property-alpha",
        "strategy_id": "property-strategy",
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("BINANCE"),
        "side": "BUY",
        "raw_score": raw_score,
        "expected_return": raw_score * 0.01,
        "expected_return_after_cost": raw_score * 0.01 - 0.0001,
        "expected_volatility": 0.2,
        "horizon_seconds": 3600,
        "probability_positive": 0.5,
        "confidence": 0.8,
        "uncertainty": 0.2,
        "expected_fee_bps": 0.5,
        "expected_slippage_bps": 0.5,
        "expected_funding_bps": 0.0,
        "market_beta": 1.0,
        "regime_fit": 0.8,
        "capacity_score": 0.9,
        "model_version": "property-model-v1",
        "policy_version": "property-policy-v1",
        "feature_hash": "features-v1",
        "timestamp": datetime(2026, 8, 21, tzinfo=UTC),
        "cost_source_hash": "cost-v1",
    }
    first = AlphaForecast(**kwargs)
    second = AlphaForecast(**kwargs)

    assert first.forecast_hash == second.forecast_hash
    assert first.forecast_hash
