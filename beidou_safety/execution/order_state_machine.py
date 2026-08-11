"""BD-CV41: 订单状态机 — 幂等 + UNKNOWN 处理。

UNKNOWN 首先 query-by-client-id，不得直接重发。
OrderAggregate terminal transition 必须满足状态机。
client_order_id 全局确定性且唯一。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum


class OrderState(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    EXCHANGE_ACKED = "EXCHANGE_ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


# BD-CV41 AC-41-04: 合法状态转换
VALID_ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.QUEUED, OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.QUEUED: {OrderState.EXCHANGE_ACKED, OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.EXCHANGE_ACKED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.UNKNOWN},
    OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN},
    OrderState.FILLED: set(),  # 终态
    OrderState.CANCELED: set(),  # 终态
    OrderState.REJECTED: set(),  # 终态
    OrderState.EXPIRED: set(),  # 终态
    OrderState.UNKNOWN: {OrderState.EXCHANGE_ACKED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED},
}


@dataclass
class OrderStateMachine:
    """BD-CV41: 订单状态机。

    FILLED 后不能回 NEW（非法逆转）。
    """

    order_id: str
    client_order_id: str = ""
    symbol: str = ""
    state: OrderState = OrderState.CREATED
    state_history: list[tuple[OrderState, float]] = field(default_factory=list)
    idempotency_key: str = ""

    def transition(self, new_state: OrderState) -> tuple[bool, str]:
        """BD-CV41: 状态转换验证。"""
        if new_state == self.state:
            return True, "NO_CHANGE"

        allowed = VALID_ORDER_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            return False, f"INVALID_TRANSITION:{self.state.value}→{new_state.value}"

        # FILLED 后不能回退
        if self.state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED):
            return False, f"TERMINAL_STATE:{self.state.value}"

        old_state = self.state
        self.state = new_state
        self.state_history.append((new_state, time.time()))
        return True, f"{old_state.value}→{new_state.value}"

    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED)

    def handle_unknown(self) -> str:
        """BD-CV41: UNKNOWN 处理策略。

        UNKNOWN → 首先 query-by-client-id，不得直接重发。
        """
        self.transition(OrderState.UNKNOWN)
        return f"QUERY_BY_CLIENT_ID:{self.client_order_id}"

    def compute_idempotency_key(self) -> str:
        key = f"{self.client_order_id}:{self.order_id}:{self.symbol}"
        self.idempotency_key = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.idempotency_key
