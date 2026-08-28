"""BD-FIX (V4 B1): market features must build from the real demo-fapi shape.

The Binance USD-M demo endpoint ``/fapi/v1/ticker/24hr`` omits
``bidPrice``/``askPrice`` (verified live on 2026-08-28).  Top-of-book from the
depth snapshot is the canonical spread source; the ticker bid/ask fields are
only an optional fallback.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.testnet_verify.runtime import VerificationRuntime

_features_from_observation = VerificationRuntime._features_from_observation


def _closed_bars(count: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "open_time": index * 60_000,
            "close_time": (index + 1) * 60_000,
            "close": str(100.0 + index * 0.1),
            "is_closed": True,
        }
        for index in range(count)
    ]


def _demo_ticker() -> dict[str, Any]:
    """Mirror the observed demo-fapi 24hr ticker: no bidPrice/askPrice."""

    return {
        "closeTime": 1787947756934,
        "count": 366422,
        "lastPrice": "77513.00",
        "lastQty": "0.0009",
        "quoteVolume": "123948120042.97",
        "symbol": "BTCUSDT",
        "volume": "1562609.8871",
        "weightedAvgPrice": "79321.22",
    }


def _depth() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "bids": [["77512.60", "0.0030"], ["77512.50", "0.0010"]],
        "asks": [["77513.30", "0.0065"], ["77513.40", "0.0020"]],
    }


def test_features_use_depth_top_of_book_when_ticker_lacks_bid_ask() -> None:
    features = _features_from_observation(
        "BTCUSDT",
        _demo_ticker(),
        _depth(),
        _closed_bars(),
        [{"fundingRate": "0.0001"}],
    )
    # spread from depth top-of-book: (77513.30 - 77512.60) / 77513.30 * 1e4
    assert features["spread_bps"] == pytest.approx(0.0903, abs=1e-3)
    assert features["quote_volume"] == pytest.approx(123948120042.97)
    assert features["signal_confidence"] >= 0.0


def test_features_fail_closed_when_depth_has_no_valid_levels() -> None:
    with pytest.raises(ValueError, match="INVALID_TICKER:BTCUSDT"):
        _features_from_observation(
            "BTCUSDT",
            _demo_ticker(),
            {"symbol": "BTCUSDT", "bids": [["not-a-price", "1"]], "asks": []},
            _closed_bars(),
            [{"fundingRate": "0.0001"}],
        )


def test_features_still_accept_ticker_bid_ask_as_fallback() -> None:
    ticker = {**_demo_ticker(), "bidPrice": "109.89", "askPrice": "109.91"}
    features = _features_from_observation(
        "BTCUSDT",
        ticker,
        {"symbol": "BTCUSDT", "bids": [], "asks": []},
        _closed_bars(),
        [{"fundingRate": "0.0001"}],
    )
    assert features["spread_bps"] == pytest.approx((109.91 - 109.89) / 109.91 * 10000, abs=1e-3)


def test_features_reject_crossed_book() -> None:
    with pytest.raises(ValueError, match="INVALID_TICKER:BTCUSDT"):
        _features_from_observation(
            "BTCUSDT",
            {"lastPrice": "100.0", "quoteVolume": "1"},
            {"symbol": "BTCUSDT", "bids": [["101.0", "1"]], "asks": [["100.0", "1"]]},
            _closed_bars(),
            [{"fundingRate": "0.0001"}],
        )
