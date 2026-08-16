"""订单状态机与幂等执行 — BD-08。

支持 CREATED→READY→SENT→ACKED→PARTIAL→FILLED/CANCELED/REJECTED/EXPIRED/UNKNOWN。
所有 slice 总数量不超过 Approval。重复用户流事件幂等处理。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any  # M21: mypy 清偿


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
    trade_facts: dict[str, tuple[float, float, float]] = field(default_factory=dict)
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
            previous = self.trade_facts.get(trade_id)
            current = (quantity, price, commission)
            if previous is not None and previous != current:
                raise ValueError(f"Conflicting economic facts for trade_id {trade_id}")
            return False  # 幂等去重

        numeric_values = (quantity, price, commission, self.original_quantity, self.executed_quantity)
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("Fill economic fields must be finite")
        if quantity <= 0 or price <= 0 or commission < 0 or self.original_quantity <= 0:
            raise ValueError("Fill quantity/price must be positive and commission non-negative")
        if self.state not in {
            OrderState.ACKED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN,
        }:
            return False

        new_executed_quantity = self.executed_quantity + quantity
        tolerance = max(1e-12, abs(self.original_quantity) * 1e-12)
        if new_executed_quantity > self.original_quantity + tolerance:
            raise ValueError("Fill quantity exceeds approved order quantity")
        next_state = (
            OrderState.FILLED if new_executed_quantity >= self.original_quantity - tolerance else OrderState.PARTIAL
        )
        if not self.transition(next_state):
            return False

        previous_executed_quantity = self.executed_quantity
        previous_value = self.avg_fill_price * previous_executed_quantity
        self.executed_quantity = min(new_executed_quantity, self.original_quantity)
        self.avg_fill_price = (previous_value + price * quantity) / self.executed_quantity
        self.total_commission += commission
        if trade_id:
            self.trade_ids.append(trade_id)
            self.trade_facts[trade_id] = (quantity, price, commission)
        return True

    @classmethod
    def from_tracker(
        cls,
        tracker: Any,
        *,
        client_order_id: str,
        instrument_id: str,
        venue_id: str,
        side: str,
        order_type: str,
        original_quantity: float,
    ) -> "OrderAggregate":
        """BD-T09: 从旧 OrderStateTracker 迁移到统一 OrderAggregate。

        旧 tracker 不包含完整经济身份，调用方必须显式提供，禁止用空字段
        构造一个看似可执行的订单。
        """
        if not all((client_order_id, instrument_id, venue_id, side, order_type)):
            raise ValueError("Legacy tracker migration requires complete economic identity")
        if not math.isfinite(original_quantity) or original_quantity <= 0:
            raise ValueError("Legacy tracker migration requires positive original_quantity")

        status_value = str(getattr(tracker.status, "value", tracker.status))
        state_mapping = {
            "NEW": OrderState.UNKNOWN,
            "PARTIALLY_FILLED": OrderState.PARTIAL,
            "PENDING_CANCEL": OrderState.CANCEL_PENDING,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "REJECTED": OrderState.REJECTED,
            "EXPIRED": OrderState.EXPIRED,
            "UNKNOWN": OrderState.UNKNOWN,
        }
        state = state_mapping.get(status_value, OrderState.UNKNOWN)
        filled_value = getattr(getattr(tracker, "filled_qty", 0.0), "amount", getattr(tracker, "filled_qty", 0.0))
        executed_quantity = float(filled_value)
        if not math.isfinite(executed_quantity) or executed_quantity < 0 or executed_quantity > original_quantity:
            raise ValueError("Legacy tracker contains invalid filled quantity")
        avg_price_value = getattr(getattr(tracker, "avg_price", None), "amount", 0.0) or 0.0
        commission_value = getattr(getattr(tracker, "commission", None), "amount", 0.0) or 0.0
        return cls(
            order_id=str(tracker.order_id),
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            side=side,
            order_type=order_type,
            original_quantity=original_quantity,
            executed_quantity=executed_quantity,
            avg_fill_price=float(avg_price_value),
            total_commission=float(commission_value),
            state=state,
        )

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

    def __init__(self) -> None:
        self._orders: dict[str, OrderAggregate] = {}

    def create(self, order: OrderAggregate) -> OrderAggregate:
        if order.order_id in self._orders:
            existing = self._orders[order.order_id]
            if self._economic_identity(existing) != self._economic_identity(order):
                raise ValueError(f"Order {order.order_id} conflicts with existing economic identity")
            if existing.is_terminal:
                raise ValueError(f"Order {order.order_id} already in terminal state")
            return existing
        if order.client_order_id:
            existing_by_client_id = self.get_by_client_id(order.client_order_id)
            if existing_by_client_id is not None:
                raise ValueError(f"Client order id {order.client_order_id} already belongs to another order")
        self._orders[order.order_id] = order
        return order

    @staticmethod
    def _economic_identity(order: OrderAggregate) -> tuple:
        return (
            order.client_order_id,
            order.instrument_id,
            order.venue_id,
            order.side,
            order.order_type,
            order.original_quantity,
            order.intent_id,
            order.approval_id,
        )

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
