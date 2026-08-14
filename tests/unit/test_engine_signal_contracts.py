"""Fail-closed contracts for the legacy components behind TypedAlphaGraph."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from beidou_core.engine import (
    AutonomousEngine,
    MeanReversionEntry,
    MomentumFilter,
    RealMarketStateEstimator,
    TrendFollowingEntry,
    VolatilityFilter,
    VolumeFilter,
    _validate_duplicate_order_response,
    _validated_ws_quote,
    adaptive_leverage,
    adaptive_position_pct,
)
from beidou_safety.execution.order_state import OrderEvent, OrderStateTracker
from beidou_shared.types import OrderId, OrderStatus
from beidou_strategy.alpha import SignalDirection


def _features() -> dict[str, object]:
    prices = [100.0 + index * 0.1 for index in range(60)]
    return {
        "prices": prices,
        "close": prices[-1],
        "sma_5": 105.7,
        "sma_20": 104.8,
        "highest_20": 106.0,
        "lowest_20": 103.0,
        "ann_volatility": 0.30,
        "atr_pct": 1.0,
        "rsi_14": 55.0,
        "trend_20_pct": 1.2,
        "vol_ratio": 1.1,
        "spread_bps": 1.5,
    }


def test_missing_entry_features_cannot_create_a_symbol_or_direction() -> None:
    signal = asyncio.run(TrendFollowingEntry().generate({"features": {}}))
    assert signal.direction is SignalDirection.NO_ACTION
    assert signal.strength == 0.0
    assert str(signal.instrument_id) == "UNKNOWN"
    assert str(signal.venue_id) == "UNKNOWN"
    assert signal.metadata["data_quality"] == "BLOCK"


def test_missing_filter_features_are_vetoes_without_direction_semantics() -> None:
    for component in (MomentumFilter(), VolatilityFilter(), VolumeFilter()):
        signal = asyncio.run(component.generate({"features": {}}))
        assert signal.direction is SignalDirection.NO_ACTION
        assert signal.strength == 0.0
        assert signal.metadata["filter_decision"] == "VETO"


def test_filters_never_emit_entry_directions_on_valid_features() -> None:
    features = _features()
    for component in (MomentumFilter(), VolatilityFilter(), VolumeFilter()):
        signal = asyncio.run(component.generate({"features": features}))
        assert signal.direction is SignalDirection.NO_ACTION


def test_mean_reversion_requires_price_series_and_market_features() -> None:
    signal = asyncio.run(MeanReversionEntry().generate({"features": {"close": 100.0}}))
    assert signal.direction is SignalDirection.NO_ACTION
    assert signal.metadata["reason"] == "MARKET_FEATURES_UNKNOWN"


def test_market_state_and_sizing_fail_closed_on_unknown_inputs() -> None:
    assert RealMarketStateEstimator.estimate({}) == {
        "direction": "UNKNOWN",
        "stress": "UNKNOWN",
        "quality": "UNKNOWN",
    }
    assert adaptive_leverage(0.0) == 0.0
    assert adaptive_position_pct(0.5, 0.0, 1.0) == 0.0
    assert adaptive_position_pct(0.5, 0.3, -1.0) == 0.0


def test_websocket_quote_requires_explicit_two_sided_finite_prices() -> None:
    assert _validated_ws_quote({"lastPrice": "100", "bid": "99", "ask": "101"}) == (100.0, 99.0, 101.0)
    assert _validated_ws_quote({"lastPrice": "100", "bid": "", "ask": "101"}) is None
    assert _validated_ws_quote({"lastPrice": "100", "bid": "101", "ask": "99"}) is None


def test_duplicate_order_recovery_requires_exact_identity_and_semantics() -> None:
    response = {
        "orderId": "42",
        "clientOrderId": "beidou-btc-close-1",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "status": "FILLED",
        "origQty": "0.010",
        "reduceOnly": True,
    }
    assert _validate_duplicate_order_response(
        response,
        client_order_id="beidou-btc-close-1",
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity="0.01",
        reduce_only=True,
    ) == (True, "OK")

    mismatched = {**response, "clientOrderId": "other-client-id"}
    assert _validate_duplicate_order_response(
        mismatched,
        client_order_id="beidou-btc-close-1",
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity="0.01",
        reduce_only=True,
    ) == (False, "CLIENT_ORDER_ID_MISMATCH")

    unproven_reduce_only = {key: value for key, value in response.items() if key != "reduceOnly"}
    assert _validate_duplicate_order_response(
        unproven_reduce_only,
        client_order_id="beidou-btc-close-1",
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity="0.01",
        reduce_only=True,
    ) == (False, "REDUCE_ONLY_UNPROVEN")


def test_ambiguous_order_status_is_durable_unknown_and_closes_risk() -> None:
    class Store:
        def __init__(self) -> None:
            self.rows = []

        def save_order_state(self, *args, **kwargs) -> None:
            self.rows.append((args, kwargs))

    engine = AutonomousEngine.__new__(AutonomousEngine)
    tracker = OrderStateTracker(order_id=OrderId("order-unknown"))
    tracker.apply(OrderEvent.ACKED)
    tracker.apply(OrderEvent.SENT)
    engine._order_trackers = {"order-unknown": tracker}
    engine._order_symbols = {"order-unknown": "BTCUSDT"}
    engine._active_order_ids = {"order-unknown"}
    engine._store = Store()
    failures: list[str] = []
    engine._record_execution_fact_failure_env_guarded = failures.append

    AutonomousEngine._mark_order_unknown(engine, "order-unknown", "BTCUSDT", "ORDER_QUERY_TIMEOUT")

    assert tracker.status is OrderStatus.UNKNOWN
    assert not engine._active_order_ids
    assert engine._store.rows[0][0][0:3] == ("order-unknown", "BTCUSDT", "UNKNOWN")
    assert engine._store.rows[0][0][6] == "UNKNOWN"
    assert failures == ["ORDER_QUERY_TIMEOUT"]


def test_filled_without_positive_execution_facts_cannot_become_filled() -> None:
    class Store:
        def __init__(self) -> None:
            self.rows = []

        def save_order_state(self, *args, **kwargs) -> None:
            self.rows.append((args, kwargs))

    engine = AutonomousEngine.__new__(AutonomousEngine)
    tracker = OrderStateTracker(order_id=OrderId("order-filled-unknown"))
    tracker.apply(OrderEvent.ACKED)
    tracker.apply(OrderEvent.SENT)
    engine._order_trackers = {"order-filled-unknown": tracker}
    engine._order_symbols = {"order-filled-unknown": "BTCUSDT"}
    engine._active_order_ids = {"order-filled-unknown"}
    engine._store = Store()
    failures: list[str] = []
    engine._record_execution_fact_failure_env_guarded = failures.append

    asyncio.run(
        AutonomousEngine._process_fill(
            engine,
            "order-filled-unknown",
            "BTCUSDT",
            {"status": "FILLED", "executedQty": "0", "avgPrice": "0"},
        )
    )

    assert tracker.status is OrderStatus.UNKNOWN
    assert engine._store.rows[0][0][6] == "UNKNOWN"
    assert failures == ["FILLED_EXECUTION_FACTS_INCOMPLETE"]


def test_leverage_error_never_treats_requested_value_as_exchange_readback() -> None:
    engine = SimpleNamespace(_leverage_cache={})
    responses = [
        {"code": -2028, "msg": "insufficient margin"},
        [{"symbol": "BTCUSDT", "leverage": "0"}],
    ]

    async def api(*_args, **_kwargs):
        return responses.pop(0)

    # Bind the production method to a minimal object so this contract tests
    # the final readback boundary without constructing the networked engine.
    from beidou_core.engine import AutonomousEngine

    engine._api_async = api
    result = asyncio.run(AutonomousEngine._ensure_leverage(engine, "BTCUSDT", 3))
    assert result == -1
    assert engine._leverage_cache["BTCUSDT"] == -1


def test_leverage_error_accepts_only_positive_exchange_readback() -> None:
    engine = SimpleNamespace(_leverage_cache={})
    responses = [
        {"code": -2028, "msg": "insufficient margin"},
        [{"symbol": "BTCUSDT", "leverage": "7"}],
    ]

    async def api(*_args, **_kwargs):
        return responses.pop(0)

    from beidou_core.engine import AutonomousEngine

    engine._api_async = api
    result = asyncio.run(AutonomousEngine._ensure_leverage(engine, "BTCUSDT", 3))
    assert result == 7
    assert engine._leverage_cache["BTCUSDT"] == 7
