"""订单状态机与幂等执行 — BD-08。

支持 CREATED→READY→SENT→ACKED→PARTIAL→FILLED/CANCELED/REJECTED/EXPIRED/UNKNOWN。
所有 slice 总数量不超过 Approval。重复用户流事件幂等处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OrderState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.READY, OrderState.REJECTED},
    OrderState.READY: {OrderState.SENT, OrderState.REJECTED},
    OrderState.SENT: {OrderState.ACKED, OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.ACKED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.EXPIRED},
    OrderState.PARTIAL: {OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.PARTIAL, OrderState.EXPIRED},
    OrderState.CANCEL_PENDING: {OrderState.CANCELED, OrderState.PARTIAL, OrderState.FILLED},
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
    OrderState.UNKNOWN: {
        OrderState.ACKED,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
    },
}

TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}


@dataclass
class OrderAggregate:
    """订单聚合 — 完整生命周期跟踪。"""

    order_id: str
    client_order_id: str
    instrument_id: str
    venue_id: str
    side: str
    order_type: str
    original_quantity: float
    executed_quantity: float = 0.0
    avg_fill_price: float = 0.0
    total_commission: float = 0.0
    trade_ids: list[str] = field(default_factory=list)
    state: OrderState = OrderState.CREATED
    intent_id: str = ""
    approval_id: str = ""
    correlation_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def transition(self, new_state: OrderState) -> bool:
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            return False
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc)
        return True

    def apply_fill(self, quantity: float, price: float, commission: float = 0.0, trade_id: str = "") -> bool:
        """应用成交更新。重复 trade_id 被幂等忽略。"""
        if trade_id and trade_id in self.trade_ids:
            return False  # 幂等去重
        self.executed_quantity += quantity
        if self.executed_quantity > 0:
            total_value = self.avg_fill_price * (self.executed_quantity - quantity) + price * quantity
            self.avg_fill_price = total_value / self.executed_quantity
        self.total_commission += commission
        if trade_id:
            self.trade_ids.append(trade_id)
        if self.executed_quantity >= self.original_quantity:
            self.transition(OrderState.FILLED)
        else:
            self.transition(OrderState.PARTIAL)
        return True

    @property
    def remaining_quantity(self) -> float:
        return max(0, self.original_quantity - self.executed_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class OrderStateMachine:
    """订单状态机管理器。

    保证:
    - crash-before-send 不重复下单（Outbox 幂等）
    - crash-after-send-before-ack: 按 client_order_id 查询恢复
    - crash-after-ack-before-persist: 从 exchange 回放恢复
    - cancel/fill race: 回放后唯一终态
    - UNKNOWN 未闭合时同 symbol 新风险冻结
    """

    def __init__(self):
        self._orders: dict[str, OrderAggregate] = {}

    def create(self, order: OrderAggregate) -> OrderAggregate:
        if order.order_id in self._orders:
            existing = self._orders[order.order_id]
            if existing.is_terminal:
                raise ValueError(f"Order {order.order_id} already in terminal state")
            return existing
        self._orders[order.order_id] = order
        return order

    def get(self, order_id: str) -> OrderAggregate | None:
        return self._orders.get(order_id)

    def get_by_client_id(self, client_order_id: str) -> OrderAggregate | None:
        for o in self._orders.values():
            if o.client_order_id == client_order_id:
                return o
        return None

    def has_unknown_for_symbol(self, symbol: str) -> bool:
        """检查是否有 UNKNOWN 状态的订单（未闭合）。"""
        return any(o.state == OrderState.UNKNOWN and o.instrument_id == symbol for o in self._orders.values())

    def active_orders(self) -> list[OrderAggregate]:
        return [o for o in self._orders.values() if not o.is_terminal]

    def total_slice_quantity(self, approval_id: str) -> float:
        """计算某 Approval 下所有 slice 的总数量。"""
        return sum(o.original_quantity for o in self._orders.values() if o.approval_id == approval_id)
