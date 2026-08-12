"""Behavioral coverage for legacy order state recovery semantics."""

from __future__ import annotations

import pytest

from beidou_safety.execution.order_state import OrderEvent, OrderStateTracker, UnknownRecoveryHandler
from beidou_shared.types import OrderId, OrderStatus


@pytest.mark.parametrize(
    ("exchange_status", "expected_event"),
    [
        (OrderStatus.FILLED, OrderEvent.FILLED),
        (OrderStatus.CANCELED, OrderEvent.CANCELED),
        (OrderStatus.NEW, OrderEvent.RECOVERED),
        (OrderStatus.PARTIALLY_FILLED, OrderEvent.RECOVERED),
        (OrderStatus.REJECTED, OrderEvent.UNKNOWN),
        (OrderStatus.EXPIRED, OrderEvent.UNKNOWN),
        (OrderStatus.UNKNOWN, OrderEvent.UNKNOWN),
    ],
)
def test_exchange_recovery_mapping_never_guesses_terminal_state(exchange_status, expected_event) -> None:
    assert UnknownRecoveryHandler.recover_from_exchange(exchange_status) is expected_event


@pytest.mark.parametrize(
    "terminal_status", list({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
)
def test_terminal_orders_reject_every_later_event(terminal_status) -> None:
    tracker = OrderStateTracker(OrderId("terminal"), status=terminal_status)
    assert not tracker.apply(OrderEvent.UNKNOWN)
    assert not tracker.apply(OrderEvent.RECOVERED)
    assert tracker.status is terminal_status
    assert tracker.events == []


def test_nonterminal_invalid_event_is_rejected_without_history() -> None:
    tracker = OrderStateTracker(OrderId("new"))
    assert not tracker.apply(OrderEvent.RECOVERED)
    assert tracker.status is OrderStatus.NEW
    assert tracker.events == []


def test_unknown_event_moves_nonterminal_order_to_unknown() -> None:
    tracker = OrderStateTracker(OrderId("partial"), status=OrderStatus.PARTIALLY_FILLED)
    assert tracker.apply(OrderEvent.UNKNOWN)
    assert tracker.status is OrderStatus.UNKNOWN
    assert tracker.events[-1][0] is OrderEvent.UNKNOWN
