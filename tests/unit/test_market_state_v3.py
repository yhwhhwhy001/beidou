"""Alpha V3 A1 market-state scenarios and fail-closed contracts."""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_shared.types import InstrumentId, VenueId
from beidou_strategy.kernel.typed_kernel import KernelInput
from beidou_strategy.state.market_state import MarketStateEstimator

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def _features(
    returns: dict[int, float],
    *,
    breadth: float = 0.5,
    breakout_breadth: float = 0.5,
    volume_expansion: float = 1.0,
    volatility: float = 0.2,
    stress_level: str | None = None,
) -> dict[str, object]:
    features: dict[str, object] = {
        "benchmark_returns": returns,
        "trend_slopes": {horizon: value / 10.0 for horizon, value in returns.items()},
        "breadth": breadth,
        "breakout_breadth": breakout_breadth,
        "volume_expansion": volume_expansion,
        "realized_volatility": volatility,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "data_quality": "PASS",
    }
    if stress_level is not None:
        features["stress_level"] = stress_level
    return features


def _estimate(features: dict[str, object]):
    return MarketStateEstimator().estimate(
        VenueId("BINANCE"),
        InstrumentId("ETHUSDT"),
        features,
        timestamp=TIMESTAMP,
    )


def test_strong_bull_is_detectable_with_directional_probability() -> None:
    state = _estimate(
        _features(
            {5: 0.03, 20: 0.10, 50: 0.25},
            breadth=0.85,
            breakout_breadth=0.80,
            volume_expansion=1.30,
        )
    )

    assert state.direction.regime == "TRENDING_UP"
    assert state.direction.regime_probabilities["STRONG_UP"] > state.direction.regime_probabilities["STRONG_DOWN"]
    assert state.direction.trend_probability > 0.5
    assert state.quality.tier == "GOOD"
    assert state.is_tradable()


def test_range_is_not_misclassified_as_trend() -> None:
    state = _estimate(_features({5: 0.001, 20: -0.001, 50: 0.002}))

    assert state.direction.regime == "RANGING"
    assert state.direction.regime_probabilities["RANGE"] >= state.direction.regime_probabilities["STRONG_UP"]
    assert state.direction.trend_probability < 0.8


def test_crisis_override_blocks_direction_even_when_trend_is_up() -> None:
    state = _estimate(
        _features(
            {5: 0.03, 20: 0.10, 50: 0.25},
            breadth=0.85,
            breakout_breadth=0.80,
            stress_level="CRISIS",
            volatility=1.4,
        )
    )

    assert state.stress.level == "CRISIS"
    assert state.should_override_direction()
    assert not state.is_tradable()


def test_invalid_nan_is_fail_closed() -> None:
    state = _estimate(_features({5: float("nan"), 20: 0.1, 50: 0.2}))

    assert state.direction.regime == "UNKNOWN"
    assert state.quality.tier == "UNKNOWN"
    assert state.direction.model_fallback
    assert not state.is_tradable()


def test_market_state_hash_is_part_of_typed_kernel_input_identity() -> None:
    base = KernelInput(symbol="ETHUSDT", market_state_hash="state-a")
    changed = KernelInput(symbol="ETHUSDT", market_state_hash="state-b")

    assert base.compute_input_hash() != changed.compute_input_hash()
