"""Order Intent、Outbox/Inbox、幂等与事务边界。"""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beidou_safety.execution import OrderIntent


class OutboxState(str, Enum):
    """BD-P0-06: Outbox 消息状态。"""

    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class IntentOutbox:
    """事务性 Outbox — Intent 先提交到 Outbox，再异步发送。

    BD-P0-06 约束:
    - 幂等键唯一性：相同 idempotency_key 不得重复提交
    - 无覆盖写入：已处理的消息不可修改
    - 状态机: PENDING → SENDING → SENT → ACKED
    """

    def __init__(self) -> None:
        self._outbox: list[OrderIntent] = []
        self._inbox: dict[str, OrderIntent] = {}
        self._processed: set[str] = set()
        self._states: dict[str, OutboxState] = {}

    def commit(self, intent: OrderIntent) -> str:
        key = intent.idempotency_key or self._hash(intent)
        if key in self._processed or any(i.idempotency_key == key or self._hash(i) == key for i in self._outbox):
            raise ValueError(f"Duplicate intent: {key}")
        self._outbox.append(intent)
        self._states[key] = OutboxState.PENDING
        return key

    def _hash(self, intent: OrderIntent) -> str:
        data = f"{intent.intent_id}:{intent.account_ref.venue_id}:{intent.account_ref.account_id}:{intent.instrument_id}:{intent.side}:{intent.quantity.amount}"
        return sha256(data.encode()).hexdigest()[:16]

    def send_to_inbox(self, intent: OrderIntent) -> None:
        self._inbox[intent.intent_id] = intent

    def ack(self, intent_id: str) -> None:
        self._processed.add(intent_id)
        self._inbox.pop(intent_id, None)

    def unacked(self) -> list[OrderIntent]:
        # Check both _inbox and _outbox for unprocessed intents
        inbox_unacked = [v for k, v in self._inbox.items() if k not in self._processed]
        outbox_unacked = [i for i in self._outbox if i.intent_id not in self._processed]
        return inbox_unacked + outbox_unacked

    def pending_count(self) -> int:
        return len(self._outbox) - len(self._processed)
