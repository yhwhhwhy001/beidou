"""Deterministic property checks for Alpha V3 market state."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.state.market_state import MarketStateEstimator


def test_probability_vector_is_finite_normalized_and_replay_stable() -> None:
    timestamp = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    for sign in (-1.0, 0.0, 1.0):
        features = {
            "benchmark_returns": {5: sign * 0.01, 20: sign * 0.03, 50: sign * 0.06},
            "trend_slopes": {5: sign * 0.001, 20: sign * 0.003, 50: sign * 0.006},
            "breadth": 0.5 + sign * 0.2,
            "breakout_breadth": 0.5 + sign * 0.15,
            "volume_expansion": 1.0,
            "realized_volatility": 0.2,
            "dispersion": 0.1,
            "cross_sectional_correlation": 0.3,
            "liquidity_score": 0.8,
            "data_quality": "PASS",
        }
        estimator = MarketStateEstimator()
        first = estimator.estimate(VenueId("BINANCE"), InstrumentId("ETHUSDT"), features, timestamp=timestamp)
        second = estimator.estimate(VenueId("BINANCE"), InstrumentId("ETHUSDT"), features, timestamp=timestamp)
        probabilities = first.direction.regime_probabilities
        assert math.isclose(sum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-12)
        assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in probabilities.values())
        assert first.state_hash == second.state_hash


def test_nan_inf_quality_inputs_never_become_tradable() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        state = MarketStateEstimator().estimate(
            VenueId("BINANCE"),
            InstrumentId("ETHUSDT"),
            {
                "benchmark_returns": {5: value},
                "realized_volatility": 0.2,
                "data_quality": "PASS",
            },
        )
        assert not state.is_tradable()
        assert state.quality.tier == "UNKNOWN"
