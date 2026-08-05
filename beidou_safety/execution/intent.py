
"""Order Intent、Outbox/Inbox、幂等与事务边界。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from beidou_shared.types import CorrelationId, OrderId
from beidou_shared.types import AccountRef, CorrelationId, InstrumentId, OrderSide, OrderType, Quantity, Price, TimeInForce, VenueId, AccountId, OrderId, ExecutionId, MonetaryValue, OrderStatus

class IntentOutbox:
    """事务性 Outbox — Intent 先提交到 Outbox，再异步发送。"""
    def __init__(self):
        self._outbox: list[OrderIntent] = []
        self._inbox: dict[str, OrderIntent] = {}
        self._processed: set[str] = set()

    def commit(self, intent: OrderIntent) -> str:
        key = intent.idempotency_key or self._hash(intent)
        if key in self._processed or any(i.idempotency_key == key or self._hash(i) == key for i in self._outbox):
            raise ValueError(f"Duplicate intent: {key}")
        self._outbox.append(intent)
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
        return [v for k, v in self._inbox.items() if k not in self._processed]

    def pending_count(self) -> int:
        return len(self._outbox) - len(self._processed)
