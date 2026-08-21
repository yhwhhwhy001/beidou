from __future__ import annotations

from beidou_strategy.alpha.breakout import BreakoutAlpha


def _context(volume_ratio: float = 2.0, breadth: float = 0.8) -> dict[str, object]:
    return {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": {
            "close": 120.0,
            "high_history": [100.0, 102.0, 105.0, 108.0, 110.0],
            "low_history": [95.0, 96.0, 98.0, 99.0, 100.0],
            "atr": 5.0,
            "volume_ratio": volume_ratio,
            "breadth": breadth,
            "breakout_breadth": breadth,
            "realized_volatility": 0.03,
            "data_quality": "PASS",
        },
        "costs": {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-v1",
        },
    }


def test_breakout_is_atr_normalized_and_confirmed() -> None:
    forecast = BreakoutAlpha().generate_forecast(_context())

    assert forecast.side == "BUY"
    assert forecast.expected_return is not None and forecast.expected_return > 0
    assert forecast.expected_return_after_cost is not None
    assert forecast.cost_verifiable


def test_false_breakout_penalty_reduces_forecast() -> None:
    confirmed = BreakoutAlpha().generate_forecast(_context())
    weak = BreakoutAlpha().generate_forecast(_context(volume_ratio=0.7, breadth=0.2))

    assert abs(weak.raw_score) < abs(confirmed.raw_score)
    assert weak.confidence < confirmed.confidence


def test_breakout_does_not_use_future_fields() -> None:
    first = BreakoutAlpha().generate_forecast(_context())
    context = _context()
    features = dict(context["features"])
    features["future_high"] = 10000.0
    features["label"] = "SHORT"
    context["features"] = features

    second = BreakoutAlpha().generate_forecast(context)

    assert first.forecast_hash == second.forecast_hash
