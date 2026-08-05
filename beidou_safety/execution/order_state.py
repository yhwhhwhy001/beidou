
"""订单状态机、用户数据流、UNKNOWN 恢复与交易所语义归一。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import (
    OrderId, OrderStatus, OrderSide, OrderType, InstrumentId, VenueId,
    Price, Quantity, MonetaryValue, CorrelationId, ExecutionId,
)

class OrderEvent(str, Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECOVERED = "RECOVERED"

ORDER_STATE_TRANSITIONS: dict[OrderStatus, dict[OrderEvent, OrderStatus]] = {
    OrderStatus.NEW: {
        OrderEvent.SENT: OrderStatus.NEW, OrderEvent.ACKED: OrderStatus.NEW,
        OrderEvent.REJECTED: OrderStatus.REJECTED,
        OrderEvent.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
        OrderEvent.FILLED: OrderStatus.FILLED,
        OrderEvent.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
        OrderEvent.CANCELED: OrderStatus.CANCELED,
        OrderEvent.EXPIRED: OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderEvent.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
        OrderEvent.FILLED: OrderStatus.FILLED,
        OrderEvent.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
    },
    OrderStatus.PENDING_CANCEL: {
        OrderEvent.CANCELED: OrderStatus.CANCELED,
        OrderEvent.PARTIALLY_FILLED: OrderStatus.PENDING_CANCEL,
        OrderEvent.FILLED: OrderStatus.FILLED,
    },
    OrderStatus.UNKNOWN: {OrderEvent.RECOVERED: OrderStatus.NEW},
}

@dataclass
class OrderStateTracker:
    order_id: OrderId
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Quantity = field(default_factory=lambda: Quantity(amount="0"))
    avg_price: Price | None = None
    commission: MonetaryValue | None = None
    events: list[tuple[OrderEvent, datetime]] = field(default_factory=list)
    correlation_id: CorrelationId | None = None

    def apply(self, event: OrderEvent) -> bool:
        transitions = ORDER_STATE_TRANSITIONS.get(self.status, {})
        if event in transitions:
            self.status = transitions[event]
            self.events.append((event, datetime.now(timezone.utc)))
            return True
        if event == OrderEvent.UNKNOWN:
            self.status = OrderStatus.UNKNOWN
            self.events.append((event, datetime.now(timezone.utc)))
            return True
        if event == OrderEvent.RECOVERED:
            if self.status == OrderStatus.UNKNOWN:
                self.status = OrderStatus.NEW
                self.events.append((event, datetime.now(timezone.utc)))
                return True
        return False

    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)

class UnknownRecoveryHandler:
    """UNKNOWN 状态恢复。查询交易所实际状态，不猜测。"""
    @staticmethod
    def recover_from_exchange(exchange_status: OrderStatus) -> OrderEvent:
        if exchange_status == OrderStatus.FILLED: return OrderEvent.FILLED
        if exchange_status == OrderStatus.CANCELED: return OrderEvent.CANCELED
        if exchange_status == OrderStatus.NEW or exchange_status == OrderStatus.PARTIALLY_FILLED:
            return OrderEvent.RECOVERED
        return OrderEvent.UNKNOWN
