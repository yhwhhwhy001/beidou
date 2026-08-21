"""Semantic coverage for the engine's pure safety and alpha boundaries."""

from __future__ import annotations

import math
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from beidou_core.engine import (
    BreakoutEntry,
    MeanReversionEntry,
    MomentumFilter,
    TimeExit,
    TrailingExit,
    TrendFollowingEntry,
    VolatilityFilter,
    VolumeFilter,
    _blocked_signal,
    _child_state_for_venue_fact,
    _derive_liquidation_price,
    _is_retryable_venue_rejection,
    _local_equity_estimate,
    _local_owned_symbols,
    _rotating_symbol_batch,
    _signal_identity,
    _validate_duplicate_order_response,
    _validate_params_positive,
    _validated_features,
    _validated_prices,
    _validated_ws_quote,
    adaptive_leverage,
    adaptive_position_pct,
)
from beidou_strategy.alpha import AlphaComponentType, SignalDirection


def _features() -> dict[str, object]:
    prices = [100.0 + ((index % 7) - 3) * 0.8 for index in range(80)]
    return {
        "prices": prices,
        "close": prices[-1],
        "sma_5": 100.0,
        "sma_20": 100.0,
        "highest_20": 103.0,
        "lowest_20": 97.0,
        "ann_volatility": 0.3,
        "atr_pct": 1.0,
        "rsi_14": 55.0,
        "trend_20_pct": 1.2,
        "vol_ratio": 1.0,
        "spread_bps": 1.0,
    }


def test_engine_pure_validation_and_rotation_boundaries() -> None:
    assert _validate_params_positive("2", name="x", max_value=3) is True
    assert _validate_params_positive(0, name="x") is False
    assert _validate_params_positive(float("nan"), name="x") is False
    assert _validate_params_positive("bad", name="x") is False
    assert _validate_params_positive(4, name="x", max_value=3) is False

    assert _validated_features({"x": 1}, ("x",)) == {"x": 1.0}
    assert _validated_features({"x": True}, ("x",)) is None
    assert _validated_features({"x": float("inf")}, ("x",)) is None
    assert _validated_features({}, ("x",)) is None
    assert _validated_features([], ("x",)) is None
    assert _validated_prices({"prices": [1, 2.5]}) == [1.0, 2.5]
    assert _validated_prices({"prices": []}) is None
    assert _validated_prices({"prices": [1, 0]}) is None
    assert _validated_prices({"prices": ["bad"]}) is None
    assert _validated_prices(None) is None
    assert _validated_ws_quote(None) is None
    assert _validated_ws_quote({"lastPrice": "bad", "bid": "1", "ask": "2"}) is None
    assert _validated_ws_quote({"lastPrice": "1", "bid": "0", "ask": "2"}) is None

    assert _rotating_symbol_batch([], 0) == ([], 0)
    assert _rotating_symbol_batch(["A", "B", "C"], -1, 2) == (["C", "A"], 1)
    assert _rotating_symbol_batch(["A", "B"], 0, 0) == ([], 0)
    assert _rotating_symbol_batch(["A", "B", "C"], 1, 10) == (["B", "C", "A"], 1)


def test_engine_rejection_and_child_state_semantics_are_fail_closed() -> None:
    markers = ("-1003", "-1021", "RATE_LIMIT", "timeout", "retryable_rejection", "venue_health_unsafe")
    assert all(_is_retryable_venue_rejection(marker) for marker in markers)
    assert _is_retryable_venue_rejection("-2013 order absent") is False
    assert _child_state_for_venue_fact("CANCELED", Decimal("0.1")).value == "UNKNOWN"
    assert _child_state_for_venue_fact("PENDING_CANCEL", Decimal("0")).value == "ACKED"
    assert _child_state_for_venue_fact("PENDING_CANCEL", Decimal("0.1")).value == "PARTIALLY_FILLED"
    assert _child_state_for_venue_fact("FILLED", Decimal("1")).value == "FILLED"
    assert _child_state_for_venue_fact("REJECTED", Decimal("0")).value == "REJECTED"
    assert _child_state_for_venue_fact("NEW", Decimal("0")).value == "ACKED"
    assert _child_state_for_venue_fact("NEW_STATUS", Decimal("0")).value == "UNKNOWN"

    assert _derive_liquidation_price(1, 100, 10) == 90
    assert _derive_liquidation_price(-1, 100, 10) == 110
    assert _derive_liquidation_price(0, 100, 10) is None
    assert _derive_liquidation_price(1, 0, 10) is None
    assert _derive_liquidation_price(math.inf, 100, 10) is None

    engine = SimpleNamespace(
        _env_mode=SimpleNamespace(value="testnet"),
        _protection=SimpleNamespace(all_positions=lambda: {"p": SimpleNamespace(instrument_id="BTCUSDT")}),
        _position_generation={"ETHUSDT": 1},
        _position_projection={"SOLUSDT": object()},
        _last_account={
            "positions": [
                {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100", "unrealizedProfit": "2"},
                {"symbol": "OTHER", "positionAmt": "1", "entryPrice": "1000", "unrealizedProfit": "9"},
                {"symbol": "BTCUSDT", "positionAmt": "bad", "entryPrice": "bad"},
            ]
        },
    )
    assert _local_owned_symbols(engine) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert _local_equity_estimate(engine, 1000.0) == pytest.approx(902.0)
    assert _local_equity_estimate(SimpleNamespace(_env_mode=SimpleNamespace(value="paper")), 10.0) == 10.0

    assert str(_signal_identity({})[0]) == "UNKNOWN"
    blocked = _blocked_signal({}, strategy_id="x", component_type=AlphaComponentType.FILTER, reason="bad")
    assert blocked.direction is SignalDirection.NO_ACTION
    assert blocked.confidence == 0.95 and blocked.metadata["filter_decision"] == "VETO"


def test_duplicate_order_validation_covers_identity_and_quantity_failures() -> None:
    base = {
        "orderId": "1",
        "clientOrderId": "cid",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "status": "NEW",
        "origQty": "1",
        "reduceOnly": True,
    }
    cases = (
        (None, "RESPONSE_NOT_OBJECT"),
        ({"orderId": "1"}, "IDENTITY_FIELD_MISSING"),
        ({**base, "clientOrderId": "other"}, "CLIENT_ORDER_ID_MISMATCH"),
        ({**base, "symbol": "ETHUSDT"}, "SYMBOL_MISMATCH"),
        ({**base, "side": "SELL"}, "SIDE_MISMATCH"),
        ({**base, "type": "LIMIT"}, "ORDER_TYPE_MISMATCH"),
        ({**base, "status": "MYSTERY"}, "ORDER_STATUS_UNKNOWN"),
        ({**base, "origQty": "bad"}, "QUANTITY_INVALID"),
        ({**base, "origQty": "2"}, "QUANTITY_MISMATCH"),
    )
    for response, expected in cases:
        assert _validate_duplicate_order_response(
            response,
            client_order_id="cid",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
            reduce_only=True,
        ) == (False, expected)


@pytest.mark.asyncio
async def test_alpha_components_cover_accept_veto_exit_and_time_paths() -> None:
    context = {"features": _features(), "instrument_id": "BTCUSDT", "venue_id": "BINANCE"}
    assert (await MomentumFilter().generate(context)).direction is SignalDirection.NO_ACTION
    assert (
        await VolatilityFilter().generate({**context, "features": {**context["features"], "ann_volatility": 0.8}})
    ).metadata["reason"] == "extreme_volatility"
    assert (
        await VolatilityFilter().generate({**context, "features": {**context["features"], "ann_volatility": 0.5}})
    ).metadata["reason"] == "elevated_volatility"
    assert (
        await VolatilityFilter().generate({**context, "features": {**context["features"], "ann_volatility": 0.2}})
    ).metadata["reason"] == "normal"
    for ratio, reason in ((0.1, "dead_volume"), (0.4, "low_volume"), (2.0, "volume_surge"), (1.0, "normal")):
        features = {**context["features"], "vol_ratio": ratio}
        assert (await VolumeFilter().generate({**context, "features": features})).metadata["reason"] == reason

    up = {**context["features"], "close": 105.0, "highest_20": 100.0, "ann_volatility": 0.3, "vol_ratio": 2.0}
    down = {**context["features"], "close": 95.0, "lowest_20": 100.0, "ann_volatility": 0.3, "vol_ratio": 2.0}
    assert (await BreakoutEntry().generate({**context, "features": up})).metadata["breakout_up"] is True
    assert (await BreakoutEntry().generate({**context, "features": down})).metadata["breakout_down"] is True
    assert (await BreakoutEntry().generate(context)).metadata["breakout_up"] is False
    assert str((await TrendFollowingEntry().generate(context)).instrument_id) == "BTCUSDT"

    no_position = await TrailingExit().generate({"features": _features(), "_position_info": {}})
    assert no_position.direction is SignalDirection.FLAT
    stop = await TrailingExit().generate(
        {
            "features": {**_features(), "close": 90.0},
            "_position_info": {"has_position": True, "entry_price": 100, "side": "LONG"},
        }
    )
    assert stop.direction is SignalDirection.FLAT and stop.strength == 0.9
    take = await TrailingExit().generate(
        {
            "features": {**_features(), "close": 110.0, "rsi_14": 80.0},
            "_position_info": {"has_position": True, "entry_price": 100, "side": "LONG"},
        }
    )
    assert take.strength == 0.7
    assert (await TimeExit().generate({"_position_info": {}})).direction is SignalDirection.FLAT
    old = await TimeExit().generate(
        {"_position_info": {"has_position": True, "entry_time": time.time() - 49 * 3600, "pnl_pct": -1.0}}
    )
    assert old.strength == 0.95


@pytest.mark.asyncio
async def test_mean_reversion_valid_snapshot_and_status_constants() -> None:
    features = _features()
    signal = await MeanReversionEntry().generate(
        {"features": features, "instrument_id": "BTCUSDT", "venue_id": "BINANCE"}
    )
    assert str(signal.instrument_id) == "BTCUSDT"
    assert all(
        component.validate()
        for component in (
            MeanReversionEntry(),
            MomentumFilter(),
            TrendFollowingEntry(),
            BreakoutEntry(),
            VolatilityFilter(),
            VolumeFilter(),
            TrailingExit(),
            TimeExit(),
        )
    )


@pytest.mark.asyncio
async def test_engine_alpha_components_cover_veto_degrade_and_exit_boundaries(monkeypatch) -> None:
    import beidou_core.engine as engine_module

    context = {"features": _features(), "instrument_id": "BTCUSDT", "venue_id": "BINANCE"}
    assert _validated_features({"x": object()}, ("x",)) is None
    assert _validated_ws_quote({"lastPrice": "nan", "bid": "1", "ask": "2"}) is None
    assert adaptive_leverage(0.1) == 3.0
    assert adaptive_leverage(0.3) == 2.0
    assert adaptive_leverage(0.5) == 1.0
    assert adaptive_leverage(0.8) == 0.5
    assert adaptive_position_pct(0.5, 0.5, 5.0, base_pct=0.1) > 0.0
    assert adaptive_position_pct(0.5, 0.5, 5.0, base_pct=0.1, vol_penalty_floor=0.8) > 0.0

    monkeypatch.setattr(
        engine_module,
        "estimate_half_life",
        lambda _prices: SimpleNamespace(valid=False, half_life_bars=None),
    )
    mean_reversion = MeanReversionEntry()
    monkeypatch.setattr(
        mean_reversion._engine,
        "evaluate",
        lambda **_kwargs: SimpleNamespace(
            signal_direction="LONG",
            strength=0.8,
            confidence=0.9,
            z_score=2.0,
            half_life_hours=12.0,
            regime_allowed=True,
            cost_viable=True,
        ),
    )
    signal = await mean_reversion.generate(context)
    assert signal.strength == pytest.approx(0.4)
    assert signal.confidence == pytest.approx(0.72)

    momentum = MomentumFilter()
    result = SimpleNamespace(
        trend_direction="UP",
        trend_strength=0.6,
        persistence=0.8,
        volatility_override=False,
        filter_decision="VETO",
    )
    for decision, expected_strength in (("VETO", 0.0), ("DEGRADE", 0.3), ("ACCEPT", 0.6)):
        result.filter_decision = decision
        result.trend_direction = "DOWN" if decision == "DEGRADE" else "UP"
        monkeypatch.setattr(momentum._momentum, "evaluate", lambda _prices, _vol, result=result: result)
        generated = await momentum.generate(context)
        assert generated.strength == pytest.approx(expected_strength)
        assert generated.metadata["reason"] in {"momentum_veto", "momentum_degrade", "momentum_accept"}

    trend = TrendFollowingEntry()
    up = {**context, "features": {**_features(), "sma_5": 105.0, "sma_20": 100.0, "trend_20_pct": 1.0, "rsi_14": 50}}
    down = {**context, "features": {**_features(), "sma_5": 95.0, "sma_20": 100.0, "trend_20_pct": -1.0, "rsi_14": 50}}
    weak = {**context, "features": {**_features(), "sma_5": 100.2, "sma_20": 100.0, "trend_20_pct": 0.1, "rsi_14": 50}}
    assert (await trend.generate(up)).direction is SignalDirection.LONG
    assert (await trend.generate(down)).direction is SignalDirection.SHORT
    assert (await trend.generate(weak)).direction is SignalDirection.LONG
    blocked_trend = await trend.generate({"features": {"sma_5": -1, "sma_20": 100, "trend_20_pct": 1, "rsi_14": 50}})
    assert blocked_trend.metadata["data_quality"] == "BLOCK"

    breakout = BreakoutEntry()
    invalid = {**context, "features": {**_features(), "close": 0}}
    assert (await breakout.generate(invalid)).metadata["data_quality"] == "BLOCK"
    no_expansion = {**context, "features": {**_features(), "close": 105, "highest_20": 100, "ann_volatility": 0.1}}
    assert (await breakout.generate(no_expansion)).direction is SignalDirection.NO_ACTION

    volatility = VolatilityFilter()
    assert (await volatility.generate({"features": {"ann_volatility": -1, "atr_pct": 1, "rsi_14": 50}})).metadata[
        "data_quality"
    ] == "BLOCK"
    volume = VolumeFilter()
    assert (await volume.generate({"features": {"vol_ratio": -1, "trend_20_pct": 0, "ann_volatility": 0.2}})).metadata[
        "data_quality"
    ] == "BLOCK"

    trailing = TrailingExit()
    features = {**_features(), "close": 102.0, "sma_20": 105.0, "atr_pct": 1.0, "rsi_14": 50.0}
    assert (
        await trailing.generate(
            {"features": features, "_position_info": {"has_position": True, "entry_price": 100, "side": "LONG"}}
        )
    ).metadata["reason"] == "trend_breakdown_sma20"
    short_features = {**features, "close": 102.0, "sma_20": 100.0}
    assert (
        await trailing.generate(
            {"features": short_features, "_position_info": {"has_position": True, "entry_price": 100, "side": "SHORT"}}
        )
    ).metadata["reason"] == "trend_reversal_sma20"
    hold = await trailing.generate(
        {
            "features": {**_features(), "close": 101.0, "sma_20": 100.0, "atr_pct": 2.0, "rsi_14": 50},
            "_position_info": {"has_position": True, "entry_price": 100, "side": "LONG"},
        }
    )
    assert hold.metadata["reason"].startswith("hold:")

    now = time.time()
    time_exit = TimeExit()
    assert (
        await time_exit.generate(
            {"_position_info": {"has_position": True, "entry_time": now - 30 * 3600, "pnl_pct": -1}}
        )
    ).strength == 0.7
    assert (
        await time_exit.generate(
            {"_position_info": {"has_position": True, "entry_time": now - 18 * 3600, "pnl_pct": 6}}
        )
    ).strength == 0.4
    assert (
        await time_exit.generate({"_position_info": {"has_position": True, "entry_time": now - 2 * 3600, "pnl_pct": 1}})
    ).direction is SignalDirection.NO_ACTION
