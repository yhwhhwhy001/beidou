"""Fail-closed economic identity and fill tests for the unified order aggregate."""

from __future__ import annotations

import math

import pytest

from beidou_safety.execution.order_machine import OrderAggregate, OrderState, OrderStateMachine
from beidou_safety.execution.order_state import OrderStateTracker
from beidou_shared.types import MonetaryValue, OrderId, OrderStatus, Price, Quantity


def _order(**overrides) -> OrderAggregate:
    values = {
        "order_id": "order-1",
        "client_order_id": "client-1",
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
        "side": "BUY",
        "order_type": "LIMIT",
        "original_quantity": 2.0,
        "approval_id": "approval-1",
    }
    values.update(overrides)
    return OrderAggregate(**values)


def _acked_order(**overrides) -> OrderAggregate:
    order = _order(**overrides)
    assert order.transition(OrderState.READY)
    assert order.transition(OrderState.SENT)
    assert order.transition(OrderState.ACKED)
    return order


@pytest.mark.parametrize(
    ("quantity", "price", "commission"),
    [
        (0.0, 100.0, 0.0),
        (-1.0, 100.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, -100.0, 0.0),
        (1.0, 100.0, -0.1),
        (math.nan, 100.0, 0.0),
        (1.0, math.inf, 0.0),
    ],
)
def test_invalid_fill_economics_are_rejected_without_mutation(quantity, price, commission) -> None:
    order = _acked_order()
    with pytest.raises(ValueError):
        order.apply_fill(quantity, price, commission, "trade-invalid")
    assert order.executed_quantity == 0
    assert order.trade_ids == []
    assert order.state is OrderState.ACKED


def test_fill_before_ack_and_overfill_are_rejected_without_mutation() -> None:
    created = _order()
    assert not created.apply_fill(1.0, 100.0, trade_id="too-early")
    assert created.executed_quantity == 0

    acknowledged = _acked_order()
    with pytest.raises(ValueError, match="exceeds approved"):
        acknowledged.apply_fill(2.1, 100.0, trade_id="overfill")
    assert acknowledged.executed_quantity == 0


def test_rejected_fill_transition_does_not_mutate_economic_facts(monkeypatch) -> None:
    order = _acked_order()
    monkeypatch.setattr(order, "transition", lambda _state: False)
    assert not order.apply_fill(1.0, 100.0, trade_id="rejected")
    assert order.executed_quantity == 0
    assert order.trade_ids == []


def test_duplicate_trade_id_requires_identical_economic_facts() -> None:
    order = _acked_order()
    assert order.apply_fill(1.0, 100.0, 0.1, "trade-1")
    assert not order.apply_fill(1.0, 100.0, 0.1, "trade-1")
    with pytest.raises(ValueError, match="Conflicting economic facts"):
        order.apply_fill(1.0, 101.0, 0.1, "trade-1")
    assert order.executed_quantity == 1.0
    assert order.total_commission == 0.1


def test_order_registry_rejects_conflicting_order_and_client_identities() -> None:
    machine = OrderStateMachine()
    existing = machine.create(_order())
    assert machine.create(_order()) is existing
    with pytest.raises(ValueError, match="economic identity"):
        machine.create(_order(side="SELL"))
    with pytest.raises(ValueError, match="Client order id"):
        machine.create(_order(order_id="order-2"))
    assert machine.get("order-1") is existing
    assert machine.get("missing") is None


def test_legacy_tracker_migration_requires_identity_and_preserves_facts() -> None:
    tracker = OrderStateTracker(
        order_id=OrderId("legacy-1"),
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=Quantity(amount="0.5"),
        avg_price=Price(amount="100"),
        commission=MonetaryValue(amount="0.1"),
    )
    migrated = OrderAggregate.from_tracker(
        tracker,
        client_order_id="legacy-client",
        instrument_id="BTCUSDT",
        venue_id="BINANCE",
        side="BUY",
        order_type="LIMIT",
        original_quantity=1.0,
    )
    assert migrated.state is OrderState.PARTIAL
    assert migrated.executed_quantity == 0.5
    assert migrated.avg_fill_price == 100.0
    assert migrated.total_commission == 0.1

    with pytest.raises(ValueError, match="complete economic identity"):
        OrderAggregate.from_tracker(
            tracker,
            client_order_id="",
            instrument_id="BTCUSDT",
            venue_id="BINANCE",
            side="BUY",
            order_type="LIMIT",
            original_quantity=1.0,
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (OrderStatus.NEW, OrderState.UNKNOWN),
        (OrderStatus.PENDING_CANCEL, OrderState.CANCEL_PENDING),
        (OrderStatus.FILLED, OrderState.FILLED),
        (OrderStatus.CANCELED, OrderState.CANCELED),
        (OrderStatus.REJECTED, OrderState.REJECTED),
        (OrderStatus.EXPIRED, OrderState.EXPIRED),
        (OrderStatus.UNKNOWN, OrderState.UNKNOWN),
    ],
)
def test_legacy_tracker_state_mapping_is_explicit(status, expected) -> None:
    tracker = OrderStateTracker(order_id=OrderId("legacy"), status=status)
    migrated = OrderAggregate.from_tracker(
        tracker,
        client_order_id="client",
        instrument_id="BTCUSDT",
        venue_id="BINANCE",
        side="BUY",
        order_type="LIMIT",
        original_quantity=1.0,
    )
    assert migrated.state is expected


def test_legacy_tracker_invalid_quantities_fail_closed() -> None:
    tracker = OrderStateTracker(order_id=OrderId("legacy"), filled_qty=Quantity(amount="2"))
    with pytest.raises(ValueError, match="invalid filled quantity"):
        OrderAggregate.from_tracker(
            tracker,
            client_order_id="client",
            instrument_id="BTCUSDT",
            venue_id="BINANCE",
            side="BUY",
            order_type="LIMIT",
            original_quantity=1.0,
        )

    with pytest.raises(ValueError, match="positive original_quantity"):
        OrderAggregate.from_tracker(
            tracker,
            client_order_id="client",
            instrument_id="BTCUSDT",
            venue_id="BINANCE",
            side="BUY",
            order_type="LIMIT",
            original_quantity=0.0,
        )
