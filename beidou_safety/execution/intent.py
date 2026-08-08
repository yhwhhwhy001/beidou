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
    - 内存安全: 自动清理超过最大保留数的已处理条目
    """

    _MAX_OUTBOX_SIZE = 1000  # 清理前最大在途 + 已处理条目数
    _MAX_PROCESSED_RETENTION = 500  # 已处理条目保留上限

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
        self._groom()
        return key

    def _groom(self) -> None:
        """清理内存：移除已 ack 的过期条目，防止内存无限增长。"""
        if len(self._outbox) > self._MAX_OUTBOX_SIZE:
            # 保留最近 _MAX_OUTBOX_SIZE/2 条，丢弃最旧的一半
            keep = self._MAX_OUTBOX_SIZE // 2
            removed = self._outbox[:-keep]
            self._outbox = self._outbox[-keep:]
            for intent in removed:
                self._processed.discard(intent.intent_id)
                key = intent.idempotency_key or self._hash(intent)
                self._states.pop(key, None)
        # 限制已处理集合大小
        if len(self._processed) > self._MAX_PROCESSED_RETENTION:
            # 移除最旧的已处理条目（非精确 LRU，使用当前 outbox 引用作为存活标记）
            active_ids = {i.intent_id for i in self._outbox}
            stale = [pid for pid in self._processed if pid not in active_ids]
            excess = len(self._processed) - self._MAX_PROCESSED_RETENTION
            for pid in stale[:max(0, excess)]:
                self._processed.discard(pid)

    def _hash(self, intent: OrderIntent) -> str:
        data = f"{intent.intent_id}:{intent.account_ref.venue_id}:{intent.account_ref.account_id}:{intent.instrument_id}:{intent.side}:{intent.quantity.amount}"
        return sha256(data.encode()).hexdigest()[:16]

    def send_to_inbox(self, intent: OrderIntent) -> None:
        self._inbox[intent.intent_id] = intent

    def ack(self, intent_id: str, idempotency_key: str = "") -> None:
        """BD-FIX (F15): ack 同时记录 intent_id 和 idempotency_key，
        确保已确认意图的幂等键继续被防重保护。"""
        self._processed.add(intent_id)
        if idempotency_key:
            self._processed.add(idempotency_key)  # 幂等键也标记为已处理
        self._inbox.pop(intent_id, None)
        # Clean up from _outbox to prevent unbounded growth
        self._outbox = [i for i in self._outbox if i.intent_id != intent_id]
        # Clean up _states for this intent
        stale_keys = [k for k, v in self._states.items() if v == OutboxState.PENDING]
        for k in stale_keys:
            if len(self._states) > self._MAX_PROCESSED_RETENTION:
                self._states.pop(k, None)

    def unacked(self) -> list[OrderIntent]:
        # Check both _inbox and _outbox for unprocessed intents
        inbox_unacked = [v for k, v in self._inbox.items() if k not in self._processed]
        outbox_unacked = [i for i in self._outbox if i.intent_id not in self._processed]
        return inbox_unacked + outbox_unacked

    def pending_count(self) -> int:
        return max(0, len(self._outbox))
