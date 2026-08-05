"""事务 Outbox — BD-03 幂等消息投递。

保证业务状态变更与事件发布在同一数据库事务中。
实现至少一次投递、幂等消费、死信队列和重试机制。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    QUARANTINED = "QUARANTINED"  # 人工隔离


@dataclass
class OutboxMessage:
    """Outbox 消息。"""

    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    client_order_id: str | None = None
    exchange_event_id: str | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    retry_count: int = 0
    max_retries: int = 10
    next_attempt_at: float = field(default_factory=time.monotonic)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    dead_letter_reason: str | None = None

    def __post_init__(self):
        if not self.idempotency_key:
            content = f"{self.aggregate_type}:{self.aggregate_id}:{self.event_type}:{json.dumps(self.payload, sort_keys=True, default=str)}"
            self.idempotency_key = hashlib.sha256(content.encode()).hexdigest()[:32]


class TransactionalOutbox:
    """事务 Outbox。

    用法:
        outbox = TransactionalOutbox()
        msg = OutboxMessage(...)
        outbox.enqueue(msg)  # 与业务写入在同一事务中

        # 后台 Worker 消费:
        for msg in outbox.poll_pending():
            try:
                dispatch(msg)
                outbox.mark_sent(msg)
                outbox.mark_acked(msg)
            except Exception:
                outbox.mark_failed(msg)
    """

    def __init__(self, max_retries: int = 10, dead_letter_threshold: int = 10):
        self._messages: dict[str, OutboxMessage] = {}
        self._idempotency_seen: set[str] = set()
        self._max_retries = max_retries
        self._dead_letter_threshold = dead_letter_threshold

    def enqueue(self, message: OutboxMessage) -> bool:
        """加入 Outbox。幂等键重复时拒绝。"""
        if message.idempotency_key and message.idempotency_key in self._idempotency_seen:
            return False  # 幂等拒绝

        self._messages[message.idempotency_key or str(time.monotonic())] = message
        if message.idempotency_key:
            self._idempotency_seen.add(message.idempotency_key)
        return True

    def poll_pending(self, batch_size: int = 100) -> list[OutboxMessage]:
        """拉取待发送消息（按 next_attempt_at 排序）。"""
        now = time.monotonic()
        pending = [m for m in self._messages.values() if m.status == OutboxStatus.PENDING and m.next_attempt_at <= now]
        pending.sort(key=lambda m: m.next_attempt_at)
        return pending[:batch_size]

    def mark_sent(self, message: OutboxMessage) -> None:
        """标记已发送。"""
        message.status = OutboxStatus.SENT
        message.sent_at = datetime.now(timezone.utc)

    def mark_acked(self, message: OutboxMessage) -> None:
        """标记已确认（终态）。"""
        message.status = OutboxStatus.ACKED

    def mark_failed(self, message: OutboxMessage) -> OutboxStatus:
        """标记失败，计算重试。"""
        message.retry_count += 1
        if message.retry_count >= self._dead_letter_threshold:
            message.status = OutboxStatus.DEAD_LETTER
            message.dead_letter_reason = f"Exceeded max retries ({message.retry_count})"
        else:
            message.status = OutboxStatus.PENDING
            backoff = min(2**message.retry_count, 300)
            message.next_attempt_at = time.monotonic() + backoff
        return message.status

    def quarantine(self, message: OutboxMessage, reason: str) -> None:
        """人工隔离消息。"""
        message.status = OutboxStatus.QUARANTINED
        message.dead_letter_reason = reason

    def get_dead_letters(self) -> list[OutboxMessage]:
        """获取所有死信消息。"""
        return [m for m in self._messages.values() if m.status == OutboxStatus.DEAD_LETTER]

    def get_quarantined(self) -> list[OutboxMessage]:
        """获取所有隔离消息。"""
        return [m for m in self._messages.values() if m.status == OutboxStatus.QUARANTINED]

    def size(self) -> int:
        return len([m for m in self._messages.values() if m.status == OutboxStatus.PENDING])
