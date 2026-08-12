"""BD-T09: 订单状态追踪器 — 当前引擎使用的实现。

迁移计划: OrderStateTracker → OrderAggregate (order_machine.py)
- OrderAggregate 提供完整事件溯源 + UNKNOWN 恢复
- 引擎逐步切换: engine.py 中的 OrderStateTracker 引用需替换为 OrderAggregate
- 切换条件: BD-T18 Testnet 认证完成后
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import (
    CorrelationId,
    MonetaryValue,
    OrderId,
    OrderStatus,
    Price,
    Quantity,
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


# PKG14 (BDS-P0-017, BDS-P0-018): 终端状态不可逆 + 恢复保留交易所状态
# 终态集合: 一旦进入不可被 UNKNOWN 覆盖
_TERMINAL_STATES: set[OrderStatus] = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
}

ORDER_STATE_TRANSITIONS: dict[OrderStatus, dict[OrderEvent, OrderStatus]] = {
    OrderStatus.NEW: {
        OrderEvent.SENT: OrderStatus.NEW,
        OrderEvent.ACKED: OrderStatus.NEW,
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
    # PKG14: 恢复时保留交易所真实状态 — 不完全丢失 filled_qty/avg_price/status
    OrderStatus.UNKNOWN: {
        OrderEvent.RECOVERED: OrderStatus.PARTIALLY_FILLED,  # 保留交易所事实
        OrderEvent.FILLED: OrderStatus.FILLED,
        OrderEvent.CANCELED: OrderStatus.CANCELED,
        OrderEvent.ACKED: OrderStatus.NEW,
    },
    # PKG14: 终态不可逆 — UNKNOWN 事件不能覆盖 FILLED/CANCELED/EXPIRED/REJECTED
    OrderStatus.FILLED: {},
    OrderStatus.CANCELED: {},
    OrderStatus.EXPIRED: {},
    OrderStatus.REJECTED: {
        OrderEvent.RECOVERED: OrderStatus.REJECTED,  # 恢复后保持终态
    },
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
        # PKG14 (BDS-P0-017): 终态不可逆 — UNKNOWN 不能覆盖 FILLED/CANCELED/EXPIRED/REJECTED
        if self.is_terminal():
            return False  # 终态订单不接受任何事件

        transitions = ORDER_STATE_TRANSITIONS.get(self.status, {})
        if event in transitions:
            self.status = transitions[event]
            self.events.append((event, datetime.now(timezone.utc)))
            return True
        # PKG14 (BDS-P0-017): UNKNOWN 作为对账标记，不篡改终态
        if event == OrderEvent.UNKNOWN:
            # 仅非终态可以进入 UNKNOWN
            self.status = OrderStatus.UNKNOWN
            self.events.append((event, datetime.now(timezone.utc)))
            return True
        return False

    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


class UnknownRecoveryHandler:
    """UNKNOWN 状态恢复。查询交易所实际状态，不猜测。"""

    @staticmethod
    def recover_from_exchange(exchange_status: OrderStatus) -> OrderEvent:
        if exchange_status == OrderStatus.FILLED:
            return OrderEvent.FILLED
        if exchange_status == OrderStatus.CANCELED:
            return OrderEvent.CANCELED
        if exchange_status == OrderStatus.NEW or exchange_status == OrderStatus.PARTIALLY_FILLED:
            return OrderEvent.RECOVERED
        return OrderEvent.UNKNOWN
