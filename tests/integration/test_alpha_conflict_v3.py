from __future__ import annotations

from datetime import UTC, datetime

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.alpha.contracts import AlphaForecast
from beidou_strategy.alpha.forecast import EnsembleFuser


def _forecast(alpha_id: str, value: float) -> AlphaForecast:
    return AlphaForecast(
        alpha_id=alpha_id,
        strategy_id="alpha-v3",
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        side="BUY" if value > 0 else "SELL",
        raw_score=value,
        expected_return=value * 0.02,
        expected_return_after_cost=value * 0.02,
        expected_volatility=0.2,
        horizon_seconds=3600,
        probability_positive=0.7 if value > 0 else 0.3,
        confidence=0.8,
        uncertainty=0.2,
        expected_fee_bps=1.0,
        expected_slippage_bps=1.0,
        expected_funding_bps=0.0,
        market_beta=0.0,
        regime_fit=0.8,
        capacity_score=0.9,
        model_version="model-v3",
        policy_version="alpha-policy-v3",
        feature_hash="features-v1",
        timestamp=datetime(2026, 8, 21, 12, tzinfo=UTC),
        cost_source_hash="cost-v1",
    )


def test_conflicting_entries_are_all_traced_and_order_invariant() -> None:
    forecasts = (_forecast("trend-v3", 1.0), _forecast("mr-v3", -1.0), _forecast("rs-v3", 0.5))
    first = EnsembleFuser().fuse(forecasts)
    second = EnsembleFuser().fuse(tuple(reversed(forecasts)))

    assert first.forecast_hash == second.forecast_hash
    assert len(first.components) == 3
    assert first.conflict_score > 0.0
