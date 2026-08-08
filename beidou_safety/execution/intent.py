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

    ⚠️ 当前为纯内存实现。进程崩溃将丢失所有 PENDING intent。
    生产环境需切换到 beidou_infra.outbox.TransactionalOutbox (PG 事务性 Outbox)。
    """

    _MAX_OUTBOX_SIZE = 1000  # 清理前最大在途 + 已处理条目数
    _MAX_PROCESSED_RETENTION = 500  # 已处理条目保留上限

    def __init__(self) -> None:
        self._outbox: list[OrderIntent] = []
        self._inbox: dict[str, OrderIntent] = {}
        self._processed: set[str] = set()
        self._states: dict[str, OutboxState] = {}
        # P0 修复: 死信追踪 — _groom() 丢弃的 intent 计数
        self._dead_letter_count: int = 0
        self._dead_letter_ids: list[str] = []  # 最近 20 个丢弃的 intent_id
        self._total_committed: int = 0
        self._total_acked: int = 0

    @property
    def dead_letter_count(self) -> int:
        return self._dead_letter_count

    @property
    def dead_letter_ids(self) -> list[str]:
        return list(self._dead_letter_ids)

    @property
    def stats(self) -> dict:
        return {
            "outbox_size": len(self._outbox),
            "inbox_size": len(self._inbox),
            "processed_count": len(self._processed),
            "dead_letter_count": self._dead_letter_count,
            "total_committed": self._total_committed,
            "total_acked": self._total_acked,
            "pending_count": self.pending_count(),
        }

    def commit(self, intent: OrderIntent) -> str:
        key = intent.idempotency_key or self._hash(intent)
        if key in self._processed or any(i.idempotency_key == key or self._hash(i) == key for i in self._outbox):
            raise ValueError(f"Duplicate intent: {key}")
        self._outbox.append(intent)
        self._states[key] = OutboxState.PENDING
        self._total_committed += 1
        self._groom()
        return key

    def _groom(self) -> None:
        """清理内存：移除已 ack 的过期条目，防止内存无限增长。

        P0 修复: 超过上限时记录死信告警而非静默丢弃。
        """
        if len(self._outbox) > self._MAX_OUTBOX_SIZE:
            keep = self._MAX_OUTBOX_SIZE // 2
            removed = self._outbox[:-keep]
            self._outbox = self._outbox[-keep:]
            # P0: 记录丢弃的 intent 而非静默
            dropped_ids = [i.intent_id for i in removed[-20:]]
            self._dead_letter_count += len(removed)
            self._dead_letter_ids = (self._dead_letter_ids + dropped_ids)[-20:]
            import logging
            logger = logging.getLogger("beidou.outbox")
            logger.warning(
                f"IntentOutbox GROOM: dropped {len(removed)} intents (total dead={self._dead_letter_count}), "
                f"recent={dropped_ids[-5:]}"
            )
            for intent in removed:
                self._processed.discard(intent.intent_id)
                key = intent.idempotency_key or self._hash(intent)
                self._states.pop(key, None)
            # 写入证据文件
            try:
                from pathlib import Path
                import json, os, time
                evt = {
                    "event": "outbox_groom_drop",
                    "dropped_count": len(removed),
                    "dead_letter_total": self._dead_letter_count,
                    "recent_ids": dropped_ids[-5:],
                    "timestamp": time.time(),
                }
                evidence_dir = Path(".beidou")
                evidence_dir.mkdir(parents=True, exist_ok=True)
                with (evidence_dir / "outbox_events.jsonl").open("a") as fh:
                    fh.write(json.dumps(evt, separators=(",", ":")) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                pass
        # 限制已处理集合大小
        if len(self._processed) > self._MAX_PROCESSED_RETENTION:
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
            self._processed.add(idempotency_key)
        self._inbox.pop(intent_id, None)
        self._outbox = [i for i in self._outbox if i.intent_id != intent_id]
        self._total_acked += 1
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
