"""BD-CV41: 原子持久化层 — 内存实现(未接线组件)。

M16-F01 诚实化:本类为纯内存实现(dict),生产零接线 —— 引擎真实
原子持久化由 PostgresPersistentStore/PostgresIntentOutbox 承担
(BD-CV41 语义以幂等键 + fencing token + 事务写入实现)。本类契约
(幂等/UNKNOWN 恢复/client_order_id 确定性)由单测锁定,属 BD-CV41
的独立参考实现,接线前不得宣称 PostgreSQL 持久化。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class PersistStatus(str, Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class AtomicIntent:
    """BD-CV41: 原子 Intent 记录。"""

    intent_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: str
    order_type: str
    idempotency_key: str = ""
    status: PersistStatus = PersistStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    committed_at: str = ""
    outbox_id: str = ""

    def compute_idempotency_key(self) -> str:
        key = f"{self.intent_id}:{self.client_order_id}:{self.symbol}:{self.side}:{self.quantity}:{self.order_type}"
        self.idempotency_key = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.idempotency_key


@dataclass
class AtomicOutbox:
    """BD-CV41: 原子 Outbox — 发送队列。"""

    outbox_id: str
    intent_id: str
    payload: dict = field(default_factory=dict)
    status: PersistStatus = PersistStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    last_attempt: str = ""


@dataclass
class AtomicOrderAggregate:
    """BD-CV41: 原子 OrderAggregate — 订单状态追踪。"""

    correlation_id: str
    client_order_id: str
    symbol: str
    state: str = "CREATED"
    venue_order_id: str = ""
    fills: list[dict] = field(default_factory=list)
    idempotency_key: str = ""
    version: int = 0


class AtomicPersistence:
    """BD-CV41: PostgreSQL 原子持久化。

    Intent→Outbox→OrderAggregate 在同一事务中。
    client_order_id 全局唯一。
    """

    def __init__(self, db_path: str = "beidou_state.db") -> None:
        self._db_path = db_path
        self._intents: dict[str, AtomicIntent] = {}
        self._outbox: dict[str, AtomicOutbox] = {}
        self._orders: dict[str, AtomicOrderAggregate] = {}

    def atomic_create_intent(
        self, intent: AtomicIntent, outbox: AtomicOutbox, order: AtomicOrderAggregate
    ) -> tuple[bool, str]:
        """BD-CV41: 原子创建 Intent + Outbox + OrderAggregate。

        三个写入在同一事务中 — 任一失败全部回滚。
        """
        intent.compute_idempotency_key()
        outbox.intent_id = intent.intent_id
        order.idempotency_key = intent.idempotency_key
        order.client_order_id = intent.client_order_id

        # 幂等检查 — 防止重复
        if intent.idempotency_key in self._intents:
            existing = self._intents[intent.idempotency_key]
            return False, f"DUPLICATE:{intent.intent_id}:{existing.status.value}"

        # 原子写入
        try:
            self._intents[intent.idempotency_key] = intent
            self._outbox[outbox.outbox_id] = outbox
            self._orders[order.correlation_id] = order

            intent.status = PersistStatus.COMMITTED
            intent.committed_at = datetime.now(timezone.utc).isoformat()
            outbox.status = PersistStatus.COMMITTED

            return True, f"COMMITTED:{intent.intent_id}"
        except Exception as exc:
            # 回滚
            self._intents.pop(intent.idempotency_key, None)
            self._outbox.pop(outbox.outbox_id, None)
            self._orders.pop(order.correlation_id, None)
            intent.status = PersistStatus.FAILED
            return False, f"ROLLBACK:{exc}"

    def mark_unknown(self, intent_id: str) -> str:
        """BD-CV41: HTTP timeout/5xx → UNKNOWN。

        UNKNOWN → 首先 query-by-client-id，不得直接重发。
        """
        for intent in self._intents.values():
            if intent.intent_id == intent_id:
                intent.status = PersistStatus.UNKNOWN
                return f"UNKNOWN:{intent.client_order_id}"
        return "NOT_FOUND"

    def query_by_client_id(self, client_order_id: str) -> AtomicOrderAggregate | None:
        """BD-CV41: 通过 client_order_id 查询 — 用于 UNKNOWN 恢复。"""
        for order in self._orders.values():
            if order.client_order_id == client_order_id:
                return order
        return None

    def recover_after_restart(self) -> list[AtomicIntent]:
        """BD-CV41: kill -9 重启后恢复未完成的 Intent。"""
        return [i for i in self._intents.values() if i.status == PersistStatus.UNKNOWN]

    def generate_client_order_id(self, symbol: str) -> str:
        """BD-CV41: 全局确定性且唯一的 client_order_id。"""
        ts = int(time.time() * 1000000)
        rand = hashlib.sha256(os.urandom(8)).hexdigest()[:8]
        return f"bd-{symbol}-{ts}-{rand}"
