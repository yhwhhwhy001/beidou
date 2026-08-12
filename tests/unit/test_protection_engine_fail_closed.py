"""Fail-closed contracts for venue protection definitions and restoration."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from beidou_safety.protection.engine import (
    PositionProtection,
    ProtectionManager,
    ProtectionOrder,
    ProtectionStatus,
    StopLossCalculator,
    StopLossType,
    TakeProfitCalculator,
    TakeProfitType,
)
from beidou_shared.types import InstrumentId, OrderSide, Price, Quantity, VenueId


def _manager() -> ProtectionManager:
    return ProtectionManager(price_decimals=2, quantity_decimals=3)


def _create(manager: ProtectionManager, **overrides) -> PositionProtection:
    values = {
        "position_id": "position-1",
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("BINANCE"),
        "entry_price": 100.0,
        "quantity": 1.0,
        "side": OrderSide.BUY,
        "stop_loss_config": {"type": "FIXED_PERCENT", "stop_pct": 5.0},
        "owner_id": "owner-1",
        "position_generation": 1,
        "session_id": "session-1",
    }
    values.update(overrides)
    return manager.create_protection(**values)


def _active_order(*, protection_id: str = "sl-position-1", position_id: str = "position-1") -> ProtectionOrder:
    return ProtectionOrder(
        protection_id=protection_id,
        position_id=position_id,
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.SELL,
        trigger_price=Price(amount="95"),
        order_price=None,
        quantity=Quantity(amount="1"),
        order_type="STOP_MARKET",
        status=ProtectionStatus.ACTIVE,
        stop_type=StopLossType.FIXED_PERCENT,
        owner_id="owner-1",
        position_generation=1,
        session_id="session-1",
        exchange_order_id="algo-1",
    )


def _projection(**overrides) -> PositionProtection:
    values = {
        "position_id": "position-1",
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("BINANCE"),
        "entry_price": 100.0,
        "quantity": 1.0,
        "side": OrderSide.BUY,
        "stop_loss": _active_order(),
        "owner_id": "owner-1",
        "position_generation": 1,
        "session_id": "session-1",
    }
    values.update(overrides)
    return PositionProtection(**values)


def test_precision_must_be_explicit_and_valid() -> None:
    with pytest.raises(ValueError, match="precision"):
        _create(ProtectionManager())
    manager = ProtectionManager()
    with pytest.raises(ValueError, match="precision"):
        manager.set_precision_from_rule(None)
    with pytest.raises(ValueError, match="precision"):
        manager.set_precision_from_rule(SimpleNamespace(price_precision=-1, qty_precision=2))
    manager.set_precision_from_rule(SimpleNamespace(price_precision=2, qty_precision=3))
    assert _create(manager).stop_loss is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_id": ""},
        {"entry_price": "bad"},
        {"entry_price": float("nan")},
        {"quantity": 0},
        {"position_generation": -1},
        {"stop_loss_config": None},
        {"side": "BUY"},
    ],
)
def test_create_rejects_incomplete_or_invalid_position_facts(overrides) -> None:
    with pytest.raises(ValueError):
        _create(_manager(), **overrides)


def test_create_rejects_duplicate_position_identity() -> None:
    manager = _manager()
    _create(manager)
    with pytest.raises(ValueError, match="already exists"):
        _create(manager)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "bad"])
def test_price_extremes_reject_invalid_market_prices(value) -> None:
    projection = _projection(stop_loss=None)
    with pytest.raises(ValueError, match="current_price"):
        projection.update_price_extremes(value)


def test_trailing_stop_requires_valid_configuration_and_never_widens() -> None:
    manager = _manager()
    with pytest.raises(ValueError, match="trail_pct"):
        _create(manager, stop_loss_config={"type": "TRAILING", "stop_pct": 5.0})

    long = _create(
        manager,
        position_id="long",
        stop_loss_config={"type": "TRAILING", "stop_pct": 5.0},
        trailing_config={"trail_pct": 2.0},
    )
    assert long.stop_loss is not None
    long.stop_loss.status = ProtectionStatus.ACTIVE
    long.update_price_extremes(110)
    assert float(long.stop_loss.trigger_price.amount) == 107.8
    long.update_price_extremes(105)
    assert float(long.stop_loss.trigger_price.amount) == 107.8

    short = _create(
        manager,
        position_id="short",
        side=OrderSide.SELL,
        stop_loss_config={"type": "TRAILING", "stop_pct": 5.0},
        trailing_config={"trail_pct": 2.0},
    )
    assert short.stop_loss is not None
    short.stop_loss.status = ProtectionStatus.ACTIVE
    short.update_price_extremes(90)
    assert float(short.stop_loss.trigger_price.amount) == 91.8


def test_restored_trailing_stop_fails_closed_on_missing_or_unsafe_config() -> None:
    order = replace(_active_order(), stop_type=StopLossType.TRAILING)
    missing = _projection(stop_loss=order, trailing_config={})
    missing.update_price_extremes(110)
    assert order.status is ProtectionStatus.FAILED

    unsafe_order = replace(_active_order(), stop_type=StopLossType.TRAILING)
    unsafe = _projection(stop_loss=unsafe_order, trailing_config={"trail_pct": 100})
    unsafe.update_price_extremes(110)
    assert unsafe_order.status is ProtectionStatus.FAILED


def test_protection_order_classification_and_position_helpers() -> None:
    stop = _active_order()
    assert stop.is_stop_loss()
    assert not stop.is_take_profit()
    short = _projection(side=OrderSide.SELL)
    assert short.is_short()
    zero = _projection(entry_price=0)
    assert zero.unrealized_pnl_pct(100) == 0


@pytest.mark.parametrize(
    "targets",
    [
        [],
        [{"rr_ratio": 0, "close_pct": 100}],
        [{"rr_ratio": 1, "close_pct": 0}],
        [{"rr_ratio": 1, "close_pct": float("nan")}],
        [{"rr_ratio": 1, "close_pct": 101}],
    ],
)
def test_multi_target_rejects_empty_or_invalid_allocations(targets) -> None:
    with pytest.raises(ValueError):
        TakeProfitCalculator.multi_target(100, 95, OrderSide.BUY, targets)


def test_take_profit_calculator_rejects_zero_or_nonpositive_output() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        TakeProfitCalculator.fixed_rr(100, 100, OrderSide.BUY)
    with pytest.raises(ValueError, match="positive"):
        TakeProfitCalculator.fixed_rr(100, 200, OrderSide.SELL, rr_ratio=2)


def test_calculator_routes_cover_short_volatility_swing_and_trailing_take_profit() -> None:
    assert StopLossCalculator.volatility_based(100, OrderSide.SELL, 5, 2) == pytest.approx(110)
    assert StopLossCalculator.calculate(StopLossType.VOLATILITY_BASED, 100, OrderSide.BUY, volatility_pct=5) == 90
    assert StopLossCalculator.calculate(
        StopLossType.SWING_STRUCTURE, 100, OrderSide.BUY, swing_low=90
    ) == pytest.approx(89.91)
    with pytest.raises(ValueError, match="protective long"):
        StopLossCalculator.swing_structure(110, None, OrderSide.BUY, entry_price=100)
    with pytest.raises(ValueError, match="protective short"):
        StopLossCalculator.swing_structure(None, 90, OrderSide.SELL, entry_price=100)
    with pytest.raises(ValueError, match="unsupported"):
        StopLossCalculator.calculate("UNKNOWN", 100, OrderSide.BUY)  # type: ignore[arg-type]

    trailing = TakeProfitCalculator.calculate(TakeProfitType.TRAILING_TAKE_PROFIT, 100, 95, OrderSide.BUY)
    assert trailing[0]["price"] == 110
    multi = TakeProfitCalculator.calculate(TakeProfitType.MULTI_TARGET, 100, 95, OrderSide.BUY)
    assert multi[0]["close_pct"] == 100
    with pytest.raises(ValueError, match="unsupported"):
        TakeProfitCalculator.calculate("UNKNOWN", 100, 95, OrderSide.BUY)  # type: ignore[arg-type]


def test_multi_target_rejects_non_mapping_total_overflow_and_zero_distance() -> None:
    with pytest.raises(ValueError, match="mapping"):
        TakeProfitCalculator.multi_target(100, 95, OrderSide.BUY, [None])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="sum"):
        TakeProfitCalculator.multi_target(
            100,
            95,
            OrderSide.BUY,
            [{"rr_ratio": 1, "close_pct": 60}, {"rr_ratio": 2, "close_pct": 60}],
        )
    with pytest.raises(ValueError, match="non-zero"):
        TakeProfitCalculator.multi_target(100, 100, OrderSide.BUY, [{"rr_ratio": 1, "close_pct": 100}])


def test_manager_stop_limit_directions_and_trailing_bound() -> None:
    long = _create(
        _manager(),
        position_id="long-limit",
        stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 5, "use_limit": True},
    )
    assert long.stop_loss is not None
    assert float(long.stop_loss.order_price.amount) < float(long.stop_loss.trigger_price.amount)

    short = _create(
        _manager(),
        position_id="short-limit",
        side=OrderSide.SELL,
        stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 5, "use_limit": True},
    )
    assert short.stop_loss is not None
    assert float(short.stop_loss.order_price.amount) > float(short.stop_loss.trigger_price.amount)
    with pytest.raises(ValueError, match="below 100"):
        _create(
            _manager(),
            position_id="bad-trail",
            stop_loss_config={"type": "TRAILING", "stop_pct": 5},
            trailing_config={"trail_pct": 100},
        )


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"price": float("nan"), "quantity_pct": 1},
        {"price": 90, "quantity_pct": 1},
        {"price": 110, "quantity_pct": 2},
    ],
)
def test_manager_rejects_malformed_calculator_targets(monkeypatch, target) -> None:
    monkeypatch.setattr(TakeProfitCalculator, "calculate", lambda *_args, **_kwargs: [target])
    with pytest.raises(ValueError):
        _create(_manager(), take_profit_config={"type": "FIXED_RR"})


@pytest.mark.parametrize(
    ("stop_price", "side"),
    [
        ("bad", OrderSide.BUY),
        (0, OrderSide.BUY),
        (100, OrderSide.BUY),
        (100, OrderSide.SELL),
    ],
)
def test_manager_rejects_invalid_stop_calculator_output(monkeypatch, stop_price, side) -> None:
    monkeypatch.setattr(StopLossCalculator, "calculate", lambda *_args, **_kwargs: stop_price)
    with pytest.raises(ValueError):
        _create(_manager(), side=side)


def test_manager_rejects_short_and_rounded_direction_target_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        TakeProfitCalculator,
        "calculate",
        lambda *_args, **_kwargs: [{"price": 110, "quantity_pct": 1}],
    )
    with pytest.raises(ValueError, match="short take-profit"):
        _create(_manager(), side=OrderSide.SELL, take_profit_config={"type": "FIXED_RR"})

    monkeypatch.setattr(
        TakeProfitCalculator,
        "calculate",
        lambda *_args, **_kwargs: [{"price": 100.004, "quantity_pct": 1}],
    )
    with pytest.raises(ValueError, match="rounded long"):
        _create(_manager(), take_profit_config={"type": "FIXED_RR"})

    monkeypatch.setattr(
        TakeProfitCalculator,
        "calculate",
        lambda *_args, **_kwargs: [{"price": 99.996, "quantity_pct": 1}],
    )
    with pytest.raises(ValueError, match="rounded short"):
        _create(_manager(), side=OrderSide.SELL, take_profit_config={"type": "FIXED_RR"})


def test_manager_rejects_values_collapsing_at_venue_precision() -> None:
    manager = ProtectionManager(price_decimals=2, quantity_decimals=0)
    with pytest.raises(ValueError, match="precision"):
        _create(manager, quantity=0.1, take_profit_config={"type": "FIXED_RR"})


def test_restore_requires_complete_matching_ack_backed_projection() -> None:
    manager = _manager()
    with pytest.raises(TypeError):
        manager.restore_position_protection(object())
    with pytest.raises(ValueError, match="identity"):
        manager.restore_position_protection(_projection(position_id=""))
    with pytest.raises(ValueError, match="entry"):
        manager.restore_position_protection(_projection(entry_price=float("nan")))
    with pytest.raises(ValueError, match="quantity"):
        manager.restore_position_protection(_projection(quantity=0))
    with pytest.raises(ValueError, match="economics"):
        manager.restore_position_protection(_projection(entry_price="bad"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        manager.restore_position_protection(_projection(stop_loss=None))
    with pytest.raises(ValueError, match="position identity"):
        manager.restore_position_protection(_projection(stop_loss=_active_order(position_id="other")))
    with pytest.raises(ValueError, match="ACK-backed"):
        manager.restore_position_protection(_projection(stop_loss=replace(_active_order(), exchange_order_id=None)))
    with pytest.raises(ValueError, match="reduce-only"):
        manager.restore_position_protection(_projection(stop_loss=replace(_active_order(), reduce_only=False)))

    projection = _projection()
    assert manager.restore_position_protection(projection) is projection
    with pytest.raises(ValueError, match="already exists"):
        manager.restore_position_protection(_projection())


def test_cancel_unknown_position_is_empty_and_terminal_orders_are_untouched() -> None:
    manager = _manager()
    assert manager.cancel_protection("missing") == []
    projection = _create(manager)
    assert projection.stop_loss is not None
    projection.stop_loss.status = ProtectionStatus.EXECUTED
    assert manager.cancel_protection(projection.position_id) == []
