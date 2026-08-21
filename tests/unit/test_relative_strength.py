from __future__ import annotations

from beidou_strategy.alpha.relative_strength import RelativeStrengthAlpha


def _context() -> dict[str, object]:
    return {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": {
            "asset_returns": {5: 0.10, 20: 0.20},
            "benchmark_returns": {5: 0.02, 20: 0.05},
            "universe_returns": {
                "BTCUSDT": {5: 0.02, 20: 0.05},
                "ETHUSDT": {5: 0.10, 20: 0.20},
                "SOLUSDT": {5: 0.04, 20: 0.08},
            },
            "universe_snapshot_timestamp": "2026-08-21T11:59:00+00:00",
            "universe_source_hash": "universe-v1",
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


def test_relative_strength_is_benchmark_relative_and_point_in_time() -> None:
    forecast = RelativeStrengthAlpha().generate_forecast(_context())

    assert forecast.side == "BUY"
    assert forecast.expected_return is not None and forecast.expected_return > 0
    assert forecast.expected_return_after_cost is not None
    assert forecast.cost_verifiable
    assert forecast.feature_hash


def test_relative_strength_ranking_is_input_order_invariant() -> None:
    first = RelativeStrengthAlpha().generate_forecast(_context())
    context = _context()
    features = dict(context["features"])
    features["universe_returns"] = {
        "SOLUSDT": {5: 0.04, 20: 0.08},
        "ETHUSDT": {5: 0.10, 20: 0.20},
        "BTCUSDT": {5: 0.02, 20: 0.05},
    }
    context["features"] = features
    second = RelativeStrengthAlpha().generate_forecast(context)

    assert first.forecast_hash == second.forecast_hash
    assert first.raw_score == second.raw_score


def test_relative_strength_stale_universe_fails_closed() -> None:
    context = _context()
    features = dict(context["features"])
    features["universe_snapshot_timestamp"] = "2026-08-21T12:01:00+00:00"
    context["features"] = features

    forecast = RelativeStrengthAlpha().generate_forecast(context)

    assert forecast.side == "NO_ACTION"
    assert forecast.expected_return is None
    assert forecast.confidence == 0.0


def test_relative_strength_ignores_future_label_fields() -> None:
    first = RelativeStrengthAlpha().generate_forecast(_context())
    context = _context()
    features = dict(context["features"])
    features["future_return"] = 999.0
    features["label"] = "BUY"
    context["features"] = features

    second = RelativeStrengthAlpha().generate_forecast(context)

    assert first.forecast_hash == second.forecast_hash
