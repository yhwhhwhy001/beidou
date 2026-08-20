"""事务 Outbox — BD-03 幂等消息投递。

保证业务状态变更与事件发布在同一数据库事务中。
实现至少一次投递、幂等消费、死信队列和重试机制。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterator, cast


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

    def __post_init__(self) -> None:
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


# BD-T08: PostgreSQL Transactional Outbox Worker
#
# The legacy ``TransactionalOutbox`` above remains a deliberately isolated
# in-memory helper for Paper/unit tests.  The classes below are the production
# PostgreSQL contract.  They never silently fall back to the in-memory helper:
# a missing connection is an explicit unavailable state and all write methods
# fail closed.


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    """One fenced message claimed by exactly one worker generation."""

    message_id: str
    intent_id: str
    client_order_id: str | None
    payload: dict[str, Any]
    lease_owner: str
    fencing_token: int


def _row_value(row: Any, key: str, index: int) -> Any:
    """Read a psycopg mapping row or a DB-API tuple row."""

    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row[index]


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("outbox payload is not a JSON object")


def _recover_sending_execution_commands(
    cursor: Any,
    *,
    lease_owner: str,
    fencing_token: int,
    expired_margin_seconds: int = 0,
) -> None:
    """Project crash-ambiguous child sends to UNKNOWN with durable evidence."""

    from beidou_safety.execution.command_aggregate import (
        ChildCommandState,
        ExecutionChildCommand,
    )

    margin = max(0, int(expired_margin_seconds))
    cursor.execute(
        "SELECT c.parent_intent_id,c.sequence,c.state,c.payload::text "
        "FROM v3_execution_commands AS c "
        "JOIN v3_transactional_outbox AS o ON o.intent_id=c.parent_intent_id "
        "WHERE c.state='SENDING' AND ("
        "c.lease_owner IS DISTINCT FROM %s OR c.fencing_token<%s "
        "OR o.status='UNKNOWN' OR o.lease_until IS NULL "
        "OR o.lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')"
        ") FOR UPDATE OF c,o SKIP LOCKED",
        (lease_owner, fencing_token, margin),
    )
    rows = cursor.fetchall() or []
    for row in rows:
        parent_intent_id = str(_row_value(row, "parent_intent_id", 0))
        sequence = int(_row_value(row, "sequence", 1))
        previous = str(_row_value(row, "state", 2))
        child = ExecutionChildCommand.from_payload(_json_payload(_row_value(row, "payload", 3)))
        event_id = f"restart-recovery:{parent_intent_id}:{sequence}:{fencing_token}"
        child = child.transition(ChildCommandState.UNKNOWN, event_id=event_id)
        cursor.execute(
            "UPDATE v3_execution_commands SET state='UNKNOWN',payload=CAST(%s AS jsonb),"
            "lease_owner=%s,fencing_token=%s,updated_at=CURRENT_TIMESTAMP "
            "WHERE parent_intent_id=%s AND sequence=%s AND state='SENDING'",
            (
                json.dumps(child.to_payload(), sort_keys=True, default=str),
                lease_owner,
                fencing_token,
                parent_intent_id,
                sequence,
            ),
        )
        if getattr(cursor, "rowcount", 1) != 1:
            continue
        cursor.execute(
            "INSERT INTO v3_execution_command_events "
            "(event_id,parent_intent_id,sequence,from_state,to_state,payload,"
            "lease_owner,fencing_token) "
            "VALUES (%s,%s,%s,%s,'UNKNOWN',CAST(%s AS jsonb),%s,%s)",
            (
                event_id,
                parent_intent_id,
                sequence,
                previous,
                json.dumps({"reason": "FENCED_RECOVERY_UNKNOWN"}, sort_keys=True),
                lease_owner,
                fencing_token,
            ),
        )


class OutboxWorker:
    """BD-T08: PG 事务性 Outbox Worker。

    使用 SELECT ... FOR UPDATE SKIP LOCKED + lease_owner/lease_until +
    fencing token 实现崩溃安全的发送。
    """

    def __init__(
        self,
        db_conn: Any | None = None,
        *,
        connection_factory: Callable[[], Any] | None = None,
        lease_owner: str = "",
        fencing_token: int = 0,
        lease_seconds: int = 30,
        sender: Callable[[OutboxClaim], Any] | None = None,
    ) -> None:
        if db_conn is not None and connection_factory is not None:
            raise ValueError("provide db_conn or connection_factory, not both")
        self._conn = db_conn
        self._connection_factory = connection_factory
        self._lease_owner = lease_owner or f"worker-{uuid.uuid4().hex[:12]}"
        self._fencing_token = int(fencing_token)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = int(lease_seconds)
        self._sender = sender

    @contextmanager
    def _connection_scope(self) -> Iterator[Any]:
        if self._conn is not None:
            yield self._conn
            return
        if self._connection_factory is None:
            raise RuntimeError("POSTGRES_OUTBOX_UNAVAILABLE: connection is not configured")
        conn = self._connection_factory()
        try:
            yield conn
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()

    @staticmethod
    @contextmanager
    def _transaction(conn: Any) -> Iterator[None]:
        transaction = getattr(conn, "transaction", None)
        if callable(transaction):
            with transaction():
                yield
            return
        try:
            yield
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        else:
            commit = getattr(conn, "commit", None)
            if callable(commit):
                commit()

    @staticmethod
    @contextmanager
    def _cursor_scope(conn: Any) -> Iterator[Any]:
        cursor = conn.cursor()
        enter = getattr(cursor, "__enter__", None)
        if callable(enter):
            with cursor as managed:
                yield managed
            return
        try:
            yield cursor
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _append_transition(
        cursor: Any,
        *,
        message_id: str,
        intent_id: str,
        from_status: str | None,
        to_status: str,
        event_type: str,
        payload: dict[str, Any] | None,
        lease_owner: str,
        fencing_token: int,
    ) -> None:
        cursor.execute(
            "INSERT INTO v3_outbox_events "
            "(event_id,message_id,intent_id,from_status,to_status,event_type,payload,lease_owner,fencing_token) "
            "VALUES (%s,%s,%s,%s,%s,%s,CAST(%s AS jsonb),%s,%s)",
            (
                f"evt-{uuid.uuid4().hex}",
                message_id,
                intent_id,
                from_status,
                to_status,
                event_type,
                json.dumps(payload or {}, sort_keys=True, default=str),
                lease_owner,
                fencing_token,
            ),
        )

    @staticmethod
    def _sync_intent_state(cursor: Any, *, intent_id: str, state: str) -> None:
        """Keep the durable intent projection aligned with the outbox state."""

        cursor.execute(
            "UPDATE v3_order_intents SET state=%s,updated_at=CURRENT_TIMESTAMP WHERE intent_id=%s",
            (state, intent_id),
        )

    @staticmethod
    def _claim_from_row(row: Any, owner: str, fencing_token: int) -> OutboxClaim:
        return OutboxClaim(
            message_id=str(_row_value(row, "message_id", 0)),
            intent_id=str(_row_value(row, "intent_id", 1)),
            client_order_id=(
                str(_row_value(row, "client_order_id", 2))
                if _row_value(row, "client_order_id", 2) is not None
                else None
            ),
            payload=_json_payload(_row_value(row, "payload", 3)),
            lease_owner=owner,
            fencing_token=fencing_token,
        )

    async def claim(self, batch_size: int = 10) -> list[OutboxClaim]:
        """Atomically claim PENDING messages with ``SKIP LOCKED`` + fencing."""

        if batch_size <= 0:
            return []
        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        if self._conn is None and self._connection_factory is None:
            return []

        claims: list[OutboxClaim] = []
        with self._connection_scope() as conn, self._transaction(conn), self._cursor_scope(conn) as cursor:
            cursor.execute(
                "SELECT message_id,intent_id,client_order_id,payload "
                "FROM v3_transactional_outbox "
                "WHERE status='PENDING' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP) "
                "AND (lease_until IS NULL OR lease_until < CURRENT_TIMESTAMP) "
                "ORDER BY created_at,message_id "
                "FOR UPDATE SKIP LOCKED LIMIT %s",
                (batch_size,),
            )
            rows = cursor.fetchall() or []
            for row in rows:
                message_id = str(_row_value(row, "message_id", 0))
                intent_id = str(_row_value(row, "intent_id", 1))
                cursor.execute(
                    "UPDATE v3_transactional_outbox SET status='SENDING',lease_owner=%s, "
                    "lease_until=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),fencing_token=%s, "
                    "updated_at=CURRENT_TIMESTAMP WHERE message_id=%s AND status='PENDING' "
                    "RETURNING message_id,intent_id,client_order_id,payload",
                    (self._lease_owner, self._lease_seconds, self._fencing_token, message_id),
                )
                updated = cursor.fetchone()
                if updated is None:
                    continue
                self._sync_intent_state(cursor, intent_id=intent_id, state="SENDING")
                self._append_transition(
                    cursor,
                    message_id=message_id,
                    intent_id=intent_id,
                    from_status="PENDING",
                    to_status="SENDING",
                    event_type="CLAIMED",
                    payload={"batch_size": batch_size},
                    lease_owner=self._lease_owner,
                    fencing_token=self._fencing_token,
                )
                claims.append(self._claim_from_row(updated, self._lease_owner, self._fencing_token))
        return claims

    async def mark_sent(self, message_id: str) -> bool:
        """Record transport submission without treating it as an exchange ACK."""

        return await self._transition_owned(
            message_id,
            from_states=("SENDING",),
            to_status="SENT",
            event_type="SUBMITTED",
            fields="sent_at=CURRENT_TIMESTAMP",
        )

    async def ack(self, message_id: str, *, exchange_order_id: str | None = None) -> bool:
        """Record a typed venue ACK only for the current owner/generation."""

        return await self._transition_owned(
            message_id,
            from_states=("SENDING", "SENT"),
            to_status="ACKED",
            event_type="ACKED",
            fields="acked_at=CURRENT_TIMESTAMP",
            payload={"exchange_order_id": exchange_order_id} if exchange_order_id else {},
        )

    async def mark_unknown(self, message_id: str, reason: str) -> bool:
        """Persist ambiguity; an UNKNOWN message is never directly resent."""

        return await self._transition_owned(
            message_id,
            from_states=("SENDING", "SENT"),
            to_status="UNKNOWN",
            event_type="UNKNOWN",
            fields="last_error=%s,lease_owner=NULL,lease_until=NULL",
            extra_params=(reason,),
            payload={"reason": reason},
        )

    async def resolve_unknown(self, message_id: str, *, exchange_order_found: bool) -> str:
        """Resolve UNKNOWN only after an independent client-order query.

        ``exchange_order_found=True`` closes the intent as ACKED.  A confirmed
        absence returns it to PENDING for a new fenced claim.  No method here
        performs a blind resend.
        """

        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        if self._conn is None and self._connection_factory is None:
            return "UNKNOWN"
        target = "ACKED" if exchange_order_found else "PENDING"
        event_type = "UNKNOWN_RESOLVED_FOUND" if exchange_order_found else "UNKNOWN_RESOLVED_ABSENT"
        with self._connection_scope() as conn, self._transaction(conn), self._cursor_scope(conn) as cursor:
            cursor.execute(
                "UPDATE v3_transactional_outbox SET status=%s,lease_owner=NULL,lease_until=NULL, "
                "last_error=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE message_id=%s AND status='UNKNOWN' AND fencing_token<=%s "
                "RETURNING intent_id",
                (target, message_id, self._fencing_token),
            )
            row = cursor.fetchone()
            if row is None:
                return "UNKNOWN"
            intent_id = str(_row_value(row, "intent_id", 0))
            self._sync_intent_state(cursor, intent_id=intent_id, state=target)
            self._append_transition(
                cursor,
                message_id=message_id,
                intent_id=intent_id,
                from_status="UNKNOWN",
                to_status=target,
                event_type=event_type,
                payload={"exchange_order_found": exchange_order_found},
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
            )
        return target

    async def recover_inflight(self, expired_margin_seconds: int = 0) -> int:
        """Fence prior generations/expired leases and convert sends to UNKNOWN.

        A process crash can leave a row in ``SENDING`` or ``SENT`` even when
        the venue may have accepted the order.  Neither a higher fencing
        generation, a different lease owner, or an expired lease does not
        prove that the order was absent, so recovery records an ambiguous fact
        and requires an independent venue query before any resend.

        ``expired_margin_seconds`` (BD-FIX root): 运行时周期恢复只回收
        "过期超过宽限窗口"的租约 —— 同一 worker 正在执行的慢 venue 调用
        不应被自己围栏。启动恢复仍传 0(进程边界,无自围栏风险)。
        """

        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        if self._conn is None and self._connection_factory is None:
            return 0
        recovered = 0
        margin = max(0, int(expired_margin_seconds))
        with self._connection_scope() as conn, self._transaction(conn), self._cursor_scope(conn) as cursor:
            cursor.execute(
                "SELECT message_id,intent_id,status FROM v3_transactional_outbox "
                "WHERE status IN ('SENDING','SENT') AND ("
                "lease_owner IS DISTINCT FROM %s OR fencing_token<%s "
                "OR lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')"
                ") FOR UPDATE SKIP LOCKED",
                (self._lease_owner, self._fencing_token, margin),
            )
            rows = cursor.fetchall() or []
            for row in rows:
                message_id = str(_row_value(row, "message_id", 0))
                intent_id = str(_row_value(row, "intent_id", 1))
                previous = str(_row_value(row, "status", 2))
                cursor.execute(
                    "UPDATE v3_transactional_outbox SET status='UNKNOWN',lease_owner=NULL,lease_until=NULL, "
                    "fencing_token=%s,last_error=%s,updated_at=CURRENT_TIMESTAMP WHERE message_id=%s "
                    "AND status IN ('SENDING','SENT') AND ("
                    "lease_owner IS DISTINCT FROM %s OR fencing_token<%s "
                    "OR lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')"
                    ")",
                    (
                        self._fencing_token,
                        "FENCED_OR_LEASE_EXPIRED",
                        message_id,
                        self._lease_owner,
                        self._fencing_token,
                        margin,
                    ),
                )
                if getattr(cursor, "rowcount", 1) != 1:
                    continue
                self._sync_intent_state(cursor, intent_id=intent_id, state="UNKNOWN")
                self._append_transition(
                    cursor,
                    message_id=message_id,
                    intent_id=intent_id,
                    from_status=previous,
                    to_status="UNKNOWN",
                    # Keep the historical event prefix so existing operators
                    # and dashboards continue to classify fenced recovery;
                    # the payload carries the stricter lease-expiry reason.
                    event_type="FENCED_RECOVERY_UNKNOWN",
                    payload={"reason": "FENCED_OR_LEASE_EXPIRED"},
                    lease_owner=self._lease_owner,
                    fencing_token=self._fencing_token,
                )
                recovered += 1

            _recover_sending_execution_commands(
                cursor,
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
            )
        return recovered

    async def mark_failed(self, message_id: str, reason: str) -> str:
        """Retry with a bounded backoff or move to DEAD_LETTER."""

        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        if self._conn is None and self._connection_factory is None:
            return "UNKNOWN"
        with self._connection_scope() as conn, self._transaction(conn), self._cursor_scope(conn) as cursor:
            cursor.execute(
                "SELECT intent_id,status,retry_count,max_retries FROM v3_transactional_outbox "
                "WHERE message_id=%s AND status IN ('SENDING','SENT') AND lease_owner=%s AND fencing_token=%s "
                "FOR UPDATE",
                (message_id, self._lease_owner, self._fencing_token),
            )
            row = cursor.fetchone()
            if row is None:
                return "UNKNOWN"
            intent_id = str(_row_value(row, "intent_id", 0))
            previous = str(_row_value(row, "status", 1))
            retry_count = int(_row_value(row, "retry_count", 2) or 0) + 1
            max_retries = int(_row_value(row, "max_retries", 3) or 0)
            target = "DEAD_LETTER" if retry_count >= max_retries else "PENDING"
            delay = min(300, 2**retry_count)
            cursor.execute(
                "UPDATE v3_transactional_outbox SET status=%s,retry_count=%s,last_error=%s, "
                "dead_letter_reason=%s,lease_owner=NULL,lease_until=NULL, "
                "next_attempt_at=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),updated_at=CURRENT_TIMESTAMP "
                "WHERE message_id=%s AND status IN ('SENDING','SENT') AND lease_owner=%s AND fencing_token=%s",
                (
                    target,
                    retry_count,
                    reason,
                    reason if target == "DEAD_LETTER" else None,
                    delay,
                    message_id,
                    self._lease_owner,
                    self._fencing_token,
                ),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                return "UNKNOWN"
            self._sync_intent_state(cursor, intent_id=intent_id, state=target)
            self._append_transition(
                cursor,
                message_id=message_id,
                intent_id=intent_id,
                from_status=previous,
                to_status=target,
                event_type="DEAD_LETTER" if target == "DEAD_LETTER" else "RETRY",
                payload={"reason": reason, "retry_count": retry_count},
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
            )
        return target

    async def _transition_owned(
        self,
        message_id: str,
        *,
        from_states: tuple[str, ...],
        to_status: str,
        event_type: str,
        fields: str,
        extra_params: tuple[Any, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        if self._conn is None and self._connection_factory is None:
            return False
        placeholders = ",".join("%s" for _ in from_states)
        with self._connection_scope() as conn, self._transaction(conn), self._cursor_scope(conn) as cursor:
            cursor.execute(
                "SELECT intent_id,status FROM v3_transactional_outbox "  # noqa: S608 - placeholders are generated only from the fixed from_states tuple
                f"WHERE message_id=%s AND status IN ({placeholders}) AND lease_owner=%s AND fencing_token=%s "
                "FOR UPDATE",
                (message_id, *from_states, self._lease_owner, self._fencing_token),
            )
            current = cursor.fetchone()
            if current is None:
                return False
            intent_id = str(_row_value(current, "intent_id", 0))
            previous = str(_row_value(current, "status", 1))
            cursor.execute(
                f"UPDATE v3_transactional_outbox SET status=%s,{fields},updated_at=CURRENT_TIMESTAMP "  # noqa: S608
                f"WHERE message_id=%s AND status IN ({placeholders}) AND lease_owner=%s AND fencing_token=%s "
                "RETURNING intent_id",
                (to_status, *extra_params, message_id, *from_states, self._lease_owner, self._fencing_token),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            self._sync_intent_state(cursor, intent_id=intent_id, state=to_status)
            self._append_transition(
                cursor,
                message_id=message_id,
                intent_id=intent_id,
                from_status=previous,
                to_status=to_status,
                event_type=event_type,
                payload=payload,
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
            )
        return True

    async def send(self, message: OutboxClaim) -> bool:
        """Invoke an explicitly injected transport; no transport means fail closed."""

        if self._sender is None:
            return False
        try:
            result = self._sender(message)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    async def process_batch(self, batch_size: int = 10) -> int:
        """Claim and invoke the injected sender; state transitions stay explicit."""

        claims = await self.claim(batch_size=batch_size)
        sent = 0
        for claim in claims:
            if await self.send(claim):
                if await self.mark_sent(claim.message_id):
                    sent += 1
            else:
                await self.mark_unknown(claim.message_id, "TRANSPORT_RESULT_UNKNOWN")
        return sent

    async def stats(self) -> dict[str, int]:
        if self._conn is None and self._connection_factory is None:
            return {"UNKNOWN": 1}
        with self._connection_scope() as conn, self._cursor_scope(conn) as cursor:
            cursor.execute("SELECT status,COUNT(*) FROM v3_transactional_outbox GROUP BY status")
            rows = cursor.fetchall() or []
        return {str(_row_value(row, "status", 0)): int(_row_value(row, "count", 1)) for row in rows}


class PostgresIntentOutbox:
    """Atomic PostgreSQL Approval + Intent + Outbox writer.

    This class provides the production transaction contract and is
    fail-closed when no connection is configured. The engine selects it only
    when the configured PostgreSQL schema and fencing token are available.
    """

    def __init__(
        self,
        dsn: str = "",
        *,
        connection_factory: Callable[[], Any] | None = None,
        lease_owner: str = "",
        fencing_token: int = 0,
        lease_seconds: int = 30,
    ) -> None:
        if connection_factory is not None and dsn:
            raise ValueError("provide dsn or connection_factory, not both")
        if connection_factory is None:
            if not dsn:
                raise ValueError("PostgreSQL DSN is required")
            import psycopg

            def connection_factory() -> Any:
                return psycopg.connect(dsn)

        self._connection_factory = connection_factory
        self._db_path = dsn or "postgresql://injected"
        self._lease_owner = lease_owner or f"engine-{uuid.uuid4().hex[:12]}"
        self._fencing_token = int(fencing_token)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = int(lease_seconds)
        # Compatibility diagnostics used by the engine.  The collections are
        # intentionally empty: PostgreSQL is the authority, not process-local
        # inbox state.
        self._processed: set[str] = set()
        self._inbox: dict[str, Any] = {}

    @staticmethod
    def _intent_key(intent: Any) -> str:
        key = str(getattr(intent, "idempotency_key", "") or "")
        if key:
            return key
        material = ":".join(
            str(getattr(intent, name, "")) for name in ("intent_id", "instrument_id", "side", "quantity")
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]

    @staticmethod
    def _intent_payload(intent: Any, key: str) -> dict[str, Any]:
        from beidou_safety.execution.intent import IntentOutbox

        return cast(dict[str, Any], json.loads(IntentOutbox._serialize_intent(intent, key)))

    def commit(self, intent: Any) -> str:
        """Atomically persist one approved intent and its send command."""

        key = self._intent_key(intent)
        payload = self._intent_payload(intent, key)
        reducing = bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False))
        approval_id = str(getattr(intent, "risk_approval_id", "") or "")
        signature = str(getattr(intent, "risk_approval_signature", "") or "")
        expires_at = getattr(intent, "risk_expires_at", None)
        if bool(approval_id) != bool(signature):
            raise ValueError("RISK_APPROVAL_ENVELOPE_INCOMPLETE")
        if not reducing and (not approval_id or not signature or expires_at is None):
            raise ValueError("RISK_APPROVAL_REQUIRED_FOR_POSTGRES_INTENT")
        approval_expiry: datetime | None = None
        if approval_id and signature:
            required_approval_fields = {
                "risk_proposal_hash": getattr(intent, "risk_proposal_hash", ""),
                "risk_intent_hash": getattr(intent, "risk_intent_hash", ""),
                "risk_account_snapshot_hash": getattr(intent, "risk_account_snapshot_hash", ""),
                "risk_snapshot_hash": getattr(intent, "risk_snapshot_hash", ""),
                "risk_policy_version": getattr(intent, "risk_policy_version", ""),
                "risk_nonce": getattr(intent, "risk_nonce", ""),
            }
            missing = [name for name, value in required_approval_fields.items() if not str(value or "")]
            if missing:
                raise ValueError(f"RISK_APPROVAL_FIELDS_REQUIRED: {','.join(missing)}")
            if expires_at is None:
                raise ValueError("RISK_APPROVAL_EXPIRY_REQUIRED")
            if isinstance(expires_at, datetime):
                approval_expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
            else:
                approval_expiry = datetime.fromtimestamp(float(expires_at), tz=timezone.utc)
            if approval_expiry <= datetime.now(timezone.utc):
                raise ValueError("RISK_APPROVAL_EXPIRED")
        created_at = getattr(intent, "created_at", datetime.now(timezone.utc))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        message_id = f"msg-{uuid.uuid4().hex}"

        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT intent_id,payload::text FROM v3_order_intents WHERE idempotency_key=%s",
                (key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                existing_payload = _json_payload(_row_value(existing, "payload", 1))
                if existing_payload != payload:
                    raise ValueError("IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
                return key
            cursor.execute(
                "INSERT INTO v3_order_intents "
                "(intent_id,idempotency_key,client_order_id,account_venue_id,account_id,instrument_id,side,"
                "order_type,quantity,price,time_in_force,payload,state,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CAST(%s AS jsonb),'PENDING',%s,%s)",
                (
                    str(intent.intent_id),
                    key,
                    getattr(intent, "client_order_id", None),
                    str(intent.account_ref.venue_id),
                    str(intent.account_ref.account_id),
                    str(intent.instrument_id),
                    str(getattr(intent.side, "value", intent.side)),
                    str(getattr(intent.order_type, "value", intent.order_type)),
                    str(intent.quantity.amount),
                    str(intent.price.amount) if getattr(intent, "price", None) else None,
                    str(getattr(intent.time_in_force, "value", intent.time_in_force)),
                    json.dumps(payload, sort_keys=True, default=str),
                    created_at,
                    created_at,
                ),
            )
            if approval_id and signature:
                cursor.execute(
                    "INSERT INTO v3_risk_approvals "
                    "(approval_id,intent_id,decision,signature,proposal_hash,account_snapshot_hash,"
                    "risk_snapshot_hash,intent_hash,policy_version,nonce,expires_at) "
                    "VALUES (%s,%s,'APPROVED',%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        approval_id,
                        str(intent.intent_id),
                        signature,
                        str(getattr(intent, "risk_proposal_hash", "")),
                        str(getattr(intent, "risk_account_snapshot_hash", "")),
                        str(getattr(intent, "risk_snapshot_hash", "")),
                        str(getattr(intent, "risk_intent_hash", "")),
                        str(getattr(intent, "risk_policy_version", "")),
                        str(getattr(intent, "risk_nonce", "")),
                        approval_expiry,
                    ),
                )
            cursor.execute(
                "INSERT INTO v3_transactional_outbox "
                "(message_id,intent_id,idempotency_key,client_order_id,payload,status) "
                "VALUES (%s,%s,%s,%s,CAST(%s AS jsonb),'PENDING')",
                (
                    message_id,
                    str(intent.intent_id),
                    key,
                    getattr(intent, "client_order_id", None),
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            OutboxWorker._append_transition(
                cursor,
                message_id=message_id,
                intent_id=str(intent.intent_id),
                from_status=None,
                to_status="PENDING",
                event_type="CREATED",
                payload={"idempotency_key": key},
                lease_owner="",
                fencing_token=0,
            )
        return key

    @staticmethod
    def _execution_aggregate(intent_id: str, rows: list[Any]) -> Any:
        from beidou_safety.execution.command_aggregate import ExecutionChildCommand, ParentExecutionAggregate

        children = [ExecutionChildCommand.from_payload(_json_payload(_row_value(row, "payload", 0))) for row in rows]
        return ParentExecutionAggregate.create(str(intent_id), children)

    def persist_execution_plan(self, intent_id: str, children: list[Any]) -> Any:
        """Atomically persist all child commands before the first venue write."""

        from beidou_safety.execution.command_aggregate import ParentExecutionAggregate

        if self._connection_factory is None or self._fencing_token <= 0:
            raise RuntimeError("EXECUTION_COMMAND_FENCING_TOKEN_UNKNOWN")
        aggregate = ParentExecutionAggregate.create(str(intent_id), children)
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT i.intent_id FROM v3_order_intents AS i "
                "JOIN v3_transactional_outbox AS o ON o.intent_id=i.intent_id "
                "WHERE i.intent_id=%s AND o.status='SENDING' AND o.lease_owner=%s "
                "AND o.fencing_token=%s AND o.lease_until>=CURRENT_TIMESTAMP "
                "FOR UPDATE OF i,o",
                (str(intent_id), self._lease_owner, self._fencing_token),
            )
            if cursor.fetchone() is None:
                raise ValueError("EXECUTION_PLAN_PARENT_NOT_CLAIMED")
            cursor.execute(
                "SELECT payload::text FROM v3_execution_commands WHERE parent_intent_id=%s ORDER BY sequence",
                (str(intent_id),),
            )
            existing_rows = cursor.fetchall() or []
            if existing_rows:
                existing = self._execution_aggregate(str(intent_id), existing_rows)
                if [child.command_hash for child in existing.children] != [
                    child.command_hash for child in aggregate.children
                ]:
                    raise ValueError("EXECUTION_PLAN_CONFLICT")
                return existing
            for child in aggregate.children:
                payload = json.dumps(child.to_payload(), sort_keys=True, default=str)
                cursor.execute(
                    "INSERT INTO v3_execution_commands "
                    "(parent_intent_id,sequence,client_order_id,command_hash,symbol,side,quantity,order_type,"
                    "time_in_force,limit_price,reduce_only,rule_snapshot_hash,payload,state,exchange_order_id,"
                    "filled_quantity,lease_owner,fencing_token) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CAST(%s AS jsonb),'PLANNED',NULL,%s,%s,%s)",
                    (
                        str(intent_id),
                        child.sequence,
                        child.client_order_id,
                        child.command_hash,
                        child.symbol,
                        child.side,
                        str(child.quantity),
                        child.order_type,
                        child.time_in_force,
                        str(child.limit_price) if child.limit_price is not None else None,
                        child.reduce_only,
                        child.rule_snapshot_hash,
                        payload,
                        str(child.filled_quantity),
                        self._lease_owner,
                        self._fencing_token,
                    ),
                )
                cursor.execute(
                    "INSERT INTO v3_execution_command_events "
                    "(event_id,parent_intent_id,sequence,from_state,to_state,payload,lease_owner,fencing_token) "
                    "VALUES (%s,%s,%s,NULL,'PLANNED',CAST(%s AS jsonb),%s,%s)",
                    (
                        f"plan:{intent_id}:{child.sequence}",
                        str(intent_id),
                        child.sequence,
                        json.dumps({"command_hash": child.command_hash}, sort_keys=True),
                        self._lease_owner,
                        self._fencing_token,
                    ),
                )
        return aggregate

    def restore_execution_plan(self, intent_id: str) -> Any | None:
        if self._connection_factory is None:
            return None
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT payload::text FROM v3_execution_commands WHERE parent_intent_id=%s ORDER BY sequence",
                (str(intent_id),),
            )
            rows = cursor.fetchall() or []
        return self._execution_aggregate(str(intent_id), rows) if rows else None

    def transition_execution_child(
        self,
        intent_id: str,
        sequence: int,
        state: Any,
        *,
        event_id: str,
        exchange_order_id: str = "",
        cumulative_filled_quantity: str | None = None,
    ) -> Any:
        if self._connection_factory is None or self._fencing_token <= 0:
            raise RuntimeError("EXECUTION_COMMAND_FENCING_TOKEN_UNKNOWN")
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT payload::text FROM v3_execution_commands "
                "WHERE parent_intent_id=%s ORDER BY sequence FOR UPDATE",
                (str(intent_id),),
            )
            rows = cursor.fetchall() or []
            if not rows:
                raise ValueError("EXECUTION_PLAN_NOT_FOUND")
            # Check idempotency only after locking the aggregate. Two workers
            # racing on the same venue event then serialize before the unique
            # event insert instead of turning a duplicate into a transaction
            # error.
            cursor.execute(
                "SELECT event_id FROM v3_execution_command_events WHERE event_id=%s",
                (str(event_id),),
            )
            duplicate = cursor.fetchone()
            aggregate = self._execution_aggregate(str(intent_id), rows)
            if duplicate is not None:
                return aggregate
            if sequence < 0 or sequence >= len(aggregate.children):
                raise ValueError("UNKNOWN_CHILD_SEQUENCE")
            previous = aggregate.children[sequence].state.value
            updated = aggregate.transition_child(
                sequence,
                state,
                event_id=event_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_quantity=cumulative_filled_quantity,
            )
            child = updated.children[sequence]
            cursor.execute(
                "UPDATE v3_execution_commands SET payload=CAST(%s AS jsonb),state=%s,exchange_order_id=%s,"
                "filled_quantity=%s,lease_owner=%s,fencing_token=%s,updated_at=CURRENT_TIMESTAMP "
                "WHERE parent_intent_id=%s AND sequence=%s AND command_hash=%s AND ("
                "(state IN ('PLANNED','SENDING') AND lease_owner=%s AND fencing_token=%s AND EXISTS ("
                "SELECT 1 FROM v3_transactional_outbox AS o WHERE o.intent_id=%s "
                "AND o.status='SENDING' AND o.lease_owner=%s AND o.fencing_token=%s "
                "AND o.lease_until>=CURRENT_TIMESTAMP)) OR "
                "(state IN ('ACKED','PARTIALLY_FILLED','UNKNOWN') AND fencing_token<=%s))",
                (
                    json.dumps(child.to_payload(), sort_keys=True, default=str),
                    child.state.value,
                    child.exchange_order_id or None,
                    str(child.filled_quantity),
                    self._lease_owner,
                    self._fencing_token,
                    str(intent_id),
                    sequence,
                    child.command_hash,
                    self._lease_owner,
                    self._fencing_token,
                    str(intent_id),
                    self._lease_owner,
                    self._fencing_token,
                    self._fencing_token,
                ),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise ValueError("EXECUTION_COMMAND_FENCED_OR_CONCURRENT")
            cursor.execute(
                "INSERT INTO v3_execution_command_events "
                "(event_id,parent_intent_id,sequence,from_state,to_state,payload,lease_owner,fencing_token) "
                "VALUES (%s,%s,%s,%s,%s,CAST(%s AS jsonb),%s,%s)",
                (
                    str(event_id),
                    str(intent_id),
                    sequence,
                    previous,
                    child.state.value,
                    json.dumps(
                        {
                            "exchange_order_id": child.exchange_order_id,
                            "cumulative_filled_quantity": str(child.filled_quantity),
                        },
                        sort_keys=True,
                    ),
                    self._lease_owner,
                    self._fencing_token,
                ),
            )
        return updated

    def project_user_order_update(self, update: Any) -> Any:
        from beidou_safety.execution.command_aggregate import ChildCommandState

        if self._connection_factory is None or self._fencing_token <= 0:
            raise RuntimeError("EXECUTION_COMMAND_FENCING_TOKEN_UNKNOWN")
        client_order_id = str(getattr(update, "client_order_id", "") or "")
        if not client_order_id:
            raise ValueError("USER_ORDER_UPDATE_CLIENT_ID_MISSING")
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT parent_intent_id,sequence FROM v3_execution_commands WHERE client_order_id=%s",
                (client_order_id,),
            )
            row = cursor.fetchone()
        if row is None:
            # Account streams include non-aggregate protection/external
            # orders.  Absence from this command store means no aggregate
            # projection, not a corrupt venue event.
            return None
        raw_status = str(getattr(getattr(update, "order_status", None), "value", "UNKNOWN"))
        cumulative = str(getattr(getattr(update, "cumulative_quantity", None), "amount", "0"))
        terminal_partial = raw_status in {"CANCELED", "EXPIRED"} and Decimal(cumulative) > 0
        state_map = {
            "NEW": ChildCommandState.ACKED,
            "PENDING_CANCEL": (
                ChildCommandState.PARTIALLY_FILLED if Decimal(cumulative) > 0 else ChildCommandState.ACKED
            ),
            "PARTIALLY_FILLED": ChildCommandState.PARTIALLY_FILLED,
            "FILLED": ChildCommandState.FILLED,
            "CANCELED": ChildCommandState.UNKNOWN if terminal_partial else ChildCommandState.CANCELED,
            "EXPIRED": ChildCommandState.UNKNOWN if terminal_partial else ChildCommandState.CANCELED,
            "REJECTED": ChildCommandState.REJECTED,
            "UNKNOWN": ChildCommandState.UNKNOWN,
        }
        target = state_map.get(raw_status, ChildCommandState.UNKNOWN)
        try:
            return self.transition_execution_child(
                str(_row_value(row, "parent_intent_id", 0)),
                int(_row_value(row, "sequence", 1)),
                target,
                event_id=f"user:{getattr(getattr(update, 'event', None), 'event_id', '')}",
                exchange_order_id=str(getattr(update, "order_id", "") or ""),
                cumulative_filled_quantity=(
                    cumulative
                    if terminal_partial
                    or target
                    in {
                        ChildCommandState.PARTIALLY_FILLED,
                        ChildCommandState.FILLED,
                        ChildCommandState.CANCELED,
                    }
                    else None
                ),
            )
        except ValueError as exc:
            # BD-FIX (stream ordering): 用户流重放/乱序会把初始 NEW 确认
            # 送到已 PARTIALLY_FILLED/FILLED 的子命令之后(状态回归)。
            # 状态回归不是经济事实冲突 —— 保持现有聚合(订单监控路径会
            # 重新建立权威状态),不得把它升级为流故障。
            if "INVALID_CHILD_TRANSITION" in str(exc) or "TERMINAL_CHILD_STATE" in str(exc):
                return self.restore_execution_plan(str(_row_value(row, "parent_intent_id", 0)))
            raise

    def inflight_signed_quantity(self, symbol: str) -> Decimal:
        if self._connection_factory is None:
            raise RuntimeError("EXECUTION_COMMAND_STORE_UNKNOWN")
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT payload::text FROM v3_execution_commands WHERE symbol=%s "
                "AND state IN ('PLANNED','SENDING','ACKED','PARTIALLY_FILLED','UNKNOWN') "
                "ORDER BY parent_intent_id,sequence",
                (str(symbol).upper(),),
            )
            rows = cursor.fetchall() or []
        from beidou_safety.execution.command_aggregate import ExecutionChildCommand

        return sum(
            (
                ExecutionChildCommand.from_payload(
                    _json_payload(_row_value(row, "payload", 0))
                ).signed_remaining_quantity
                for row in rows
            ),
            Decimal("0"),
        )

    def stale_child_commands(self, min_age_seconds: float = 1800.0) -> list[dict[str, Any]]:
        """BD-FIX (final83j): 非终态且超龄的执行子命令(幽灵在途)。

        这些行会持续计入 inflight_signed_quantity,把新订单的目标增量挤到
        交易所最小下单量(实测 XRP 提案 64 币、实际只下 5.8)。引擎按 venue
        事实裁决终态后调用 recover_stale_child 迁移。
        """
        if self._connection_factory is None:
            return []
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT parent_intent_id,sequence,state,symbol,client_order_id,exchange_order_id,"
                "filled_quantity,updated_at FROM v3_execution_commands "
                "WHERE state IN ('PLANNED','SENDING','ACKED','PARTIALLY_FILLED','UNKNOWN') "
                "AND updated_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second') "
                "ORDER BY updated_at LIMIT 150",
                (float(min_age_seconds),),
            )
            rows = cursor.fetchall() or []
        return [
            {
                "parent_intent_id": str(_row_value(row, "parent_intent_id", 0)),
                "sequence": int(_row_value(row, "sequence", 1)),
                "state": str(_row_value(row, "state", 2)),
                "symbol": str(_row_value(row, "symbol", 3) or ""),
                "client_order_id": str(_row_value(row, "client_order_id", 4) or ""),
                "exchange_order_id": str(_row_value(row, "exchange_order_id", 5) or ""),
                "filled_quantity": str(_row_value(row, "filled_quantity", 6) or "0"),
            }
            for row in rows
        ]

    def recover_stale_child(
        self,
        intent_id: str,
        sequence: int,
        state: Any,
        *,
        event_id: str,
        exchange_order_id: str = "",
        cumulative_filled_quantity: str | None = None,
    ) -> None:
        """BD-FIX (final83j): 幽灵子命令按 venue 事实裁决终态(受治理恢复)。

        与 transition_execution_child 的区别:子命令行不要求活跃租约(历史
        遗留行的租约早已过期),只要求父意图当前未被活跃租约持有 —— 避免与
        正在执行中的意图竞争。事件幂等/终态守卫/状态机校验与主路径一致;
        PLANNED 只允许迁移到 SENDING/REJECTED,其余映射由调用方按 venue
        事实构造。
        """
        if self._connection_factory is None or self._fencing_token <= 0:
            raise RuntimeError("EXECUTION_COMMAND_FENCING_TOKEN_UNKNOWN")
        _all_terminal = False
        _any_rejected = False
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT 1 FROM v3_transactional_outbox WHERE intent_id=%s "
                "AND lease_until IS NOT NULL AND lease_until>=CURRENT_TIMESTAMP FOR UPDATE",
                (str(intent_id),),
            )
            if cursor.fetchone() is not None:
                raise ValueError("STALE_CHILD_PARENT_ACTIVELY_LEASED")
            cursor.execute(
                "SELECT payload::text FROM v3_execution_commands "
                "WHERE parent_intent_id=%s ORDER BY sequence FOR UPDATE",
                (str(intent_id),),
            )
            rows = cursor.fetchall() or []
            if not rows:
                raise ValueError("EXECUTION_PLAN_NOT_FOUND")
            if sequence < 0 or sequence >= len(rows):
                raise ValueError("UNKNOWN_CHILD_SEQUENCE")
            cursor.execute(
                "SELECT event_id FROM v3_execution_command_events WHERE event_id=%s",
                (str(event_id),),
            )
            if cursor.fetchone() is not None:
                return
            aggregate = self._execution_aggregate(str(intent_id), rows)
            previous = aggregate.children[sequence].state.value
            updated = aggregate.transition_child(
                sequence,
                state,
                event_id=event_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_quantity=cumulative_filled_quantity,
            )
            child = updated.children[sequence]
            cursor.execute(
                "UPDATE v3_execution_commands SET payload=CAST(%s AS jsonb),state=%s,exchange_order_id=%s,"
                "filled_quantity=%s,updated_at=CURRENT_TIMESTAMP "
                "WHERE parent_intent_id=%s AND sequence=%s AND state=%s",
                (
                    json.dumps(child.to_payload(), sort_keys=True, default=str),
                    child.state.value,
                    child.exchange_order_id or None,
                    str(child.filled_quantity),
                    str(intent_id),
                    sequence,
                    previous,
                ),
            )
            if getattr(cursor, "rowcount", 1) != 1:
                raise ValueError("STALE_CHILD_CONCURRENT")
            cursor.execute(
                "INSERT INTO v3_execution_command_events "
                "(event_id,parent_intent_id,sequence,from_state,to_state,payload,lease_owner,fencing_token) "
                "VALUES (%s,%s,%s,%s,%s,CAST(%s AS jsonb),%s,%s)",
                (
                    str(event_id),
                    str(intent_id),
                    sequence,
                    previous,
                    child.state.value,
                    json.dumps({"reason": "STALE_CHILD_VENUE_RESOLVED"}, sort_keys=True),
                    self._lease_owner,
                    self._fencing_token,
                ),
            )
            from beidou_safety.execution.command_aggregate import ChildCommandState

            _terminal = {ChildCommandState.FILLED, ChildCommandState.CANCELED, ChildCommandState.REJECTED}
            _all_terminal = all(c.state in _terminal for c in updated.children)
            _any_rejected = any(c.state is ChildCommandState.REJECTED for c in updated.children)

        # BD-FIX: 全部子命令到达终态后关闭父意图,避免父行永久停留在
        # SENDING/SENT(realtime 视图 pending 永不归零、inflight 泄漏)。
        # 父行仍为 UNKNOWN 时 ack/reject 均不适用,由 UNKNOWN 恢复路径
        # (venue 明确缺席 → PENDING → worker 复核)另行裁决。
        if _all_terminal:
            if _any_rejected:
                self.reject(intent_id, "EXECUTION_PLAN_TERMINAL_REJECTED", idempotency_key="")
            else:
                self.ack(intent_id, idempotency_key="")

    def project_order_terminal(
        self,
        exchange_order_id: str,
        status: str,
        cumulative_filled_quantity: str | None = None,
    ) -> Any:
        """BD-FIX (final83j): 订单监控路径按 orderId 投影终态。

        用户流投影在 pre-ACK 成交竞态下会错过(子命令行尚不存在),订单监控
        观察到 FILLED 后调用本方法兜底迁移 ACKED/PARTIALLY_FILLED/UNKNOWN
        到终态,堵住 inflight 泄漏。
        """
        if self._connection_factory is None or self._fencing_token <= 0:
            raise RuntimeError("EXECUTION_COMMAND_FENCING_TOKEN_UNKNOWN")
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT parent_intent_id,sequence,state FROM v3_execution_commands WHERE exchange_order_id=%s",
                (str(exchange_order_id),),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        parent_intent_id = str(_row_value(row, "parent_intent_id", 0))
        sequence = int(_row_value(row, "sequence", 1))
        current_state = str(_row_value(row, "state", 2))
        from beidou_safety.execution.command_aggregate import ChildCommandState

        _status = str(status).upper()
        state_map = {
            "FILLED": ChildCommandState.FILLED,
            "CANCELED": ChildCommandState.CANCELED,
            "EXPIRED": ChildCommandState.CANCELED,
            "REJECTED": ChildCommandState.REJECTED,
        }
        target = state_map.get(_status)
        if target is None or current_state in ("FILLED", "CANCELED", "REJECTED"):
            return None
        # PLANNED/SENDING 需要活跃租约,监控路径不处理(交恢复路径)
        if current_state in ("PLANNED", "SENDING"):
            return None
        try:
            return self.transition_execution_child(
                parent_intent_id,
                sequence,
                target,
                event_id="monitor:" + str(exchange_order_id) + ":" + _status,
                exchange_order_id=str(exchange_order_id),
                cumulative_filled_quantity=cumulative_filled_quantity,
            )
        except ValueError as exc:
            # TERMINAL_CHILD_STATE:幂等重复,忽略
            if "TERMINAL_CHILD_STATE" in str(exc):
                return None
            raise

    @property
    def _outbox(self) -> list[Any]:
        """Compatibility view for diagnostics; never a writable local queue."""

        return self.unacked()

    @staticmethod
    def _deserialize_intent_payload(payload: Any) -> Any:
        from beidou_safety.execution.intent import IntentOutbox

        return IntentOutbox._deserialize_intent(str(payload))

    def restore_pending_approvals(self) -> list[dict[str, Any]]:
        if self._connection_factory is None:
            return []
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT r.approval_id,r.intent_id,r.decision,r.signature,r.proposal_hash,"
                "r.account_snapshot_hash,r.risk_snapshot_hash,r.intent_hash,r.policy_version,r.nonce,"
                "EXTRACT(EPOCH FROM r.expires_at) AS expires_at "
                "FROM v3_risk_approvals r JOIN v3_transactional_outbox o ON o.intent_id=r.intent_id "
                "WHERE o.status IN ('PENDING','SENDING','UNKNOWN') AND r.decision='APPROVED' "
                "ORDER BY r.created_at"
            )
            rows = cursor.fetchall() or []
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "approval_id": _row_value(row, "approval_id", 0),
                    "intent_id": _row_value(row, "intent_id", 1),
                    "decision": _row_value(row, "decision", 2),
                    "signature": _row_value(row, "signature", 3),
                    "proposal_hash": _row_value(row, "proposal_hash", 4),
                    "account_snapshot_hash": _row_value(row, "account_snapshot_hash", 5),
                    "risk_snapshot_hash": _row_value(row, "risk_snapshot_hash", 6),
                    "intent_hash": _row_value(row, "intent_hash", 7),
                    "policy_version": _row_value(row, "policy_version", 8),
                    "nonce": _row_value(row, "nonce", 9),
                    "expires_at": _row_value(row, "expires_at", 10),
                }
            )
        return result

    def _query_intents(self, states: tuple[str, ...]) -> list[Any]:
        if self._connection_factory is None:
            return []
        placeholders = ",".join("%s" for _ in states)
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                f"SELECT payload::text FROM v3_transactional_outbox WHERE status IN ({placeholders}) "  # noqa: S608
                "ORDER BY created_at,message_id",
                states,
            )
            rows = cursor.fetchall() or []
        return [self._deserialize_intent_payload(_row_value(row, "payload", 0)) for row in rows]

    def unacked(self) -> list[Any]:
        return self._query_intents(("PENDING", "SENDING", "UNKNOWN"))

    def get_unknown_intents(self) -> list[dict[str, str]]:
        """Return identity-bound UNKNOWN rows for read-after-write recovery.

        Includes the approval envelope so the resolver can re-arm the one-shot
        approval after a definitive venue-absence fact (P1: nonce consumed
        before the write would otherwise make the governed resend a dead end).
        """

        intents = self._query_intents(("UNKNOWN",))
        return [
            {
                "intent_id": str(intent.intent_id),
                "symbol": str(intent.instrument_id),
                "client_order_id": str(intent.client_order_id or ""),
                "risk_approval_id": str(getattr(intent, "risk_approval_id", "") or ""),
                "risk_nonce": str(getattr(intent, "risk_nonce", "") or ""),
            }
            for intent in intents
        ]

    def pending_count(self) -> int:
        if self._connection_factory is None:
            return 0
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT COUNT(*) FROM v3_transactional_outbox WHERE status IN ('PENDING','SENDING','UNKNOWN')"
            )
            row = cursor.fetchone()
        return int(_row_value(row, "count", 0) or 0) if row is not None else 0

    def recover_inflight(self, expired_margin_seconds: int = 0) -> int:
        """Convert prior-generation/stale in-flight intents to ``UNKNOWN``.

        The engine uses a synchronous claim boundary, so startup recovery is
        exposed on the same adapter instead of relying on an uncalled async
        helper.  A different lease owner fences an old process generation;
        an expired or missing lease is ambiguous even when the token is
        unchanged.  Both cases are recorded as UNKNOWN and require an
        independent client-order query.

        ``expired_margin_seconds`` (BD-FIX root): 运行时周期恢复只回收
        "过期超过宽限窗口"的租约 —— 同一 worker 正在执行的慢 venue 调用
        不应被自己围栏。启动恢复仍传 0。
        """

        if self._connection_factory is None:
            return 0
        if self._fencing_token <= 0:
            raise RuntimeError("OUTBOX_FENCING_TOKEN_UNKNOWN")
        margin = max(0, int(expired_margin_seconds))
        recovered = 0
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT message_id,intent_id,status FROM v3_transactional_outbox "
                "WHERE status IN ('SENDING','SENT') AND ("
                "lease_owner IS DISTINCT FROM %s OR fencing_token<%s "
                "OR lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')"
                ") FOR UPDATE SKIP LOCKED",
                (self._lease_owner, self._fencing_token, margin),
            )
            rows = cursor.fetchall() or []
            for row in rows:
                message_id = str(_row_value(row, "message_id", 0))
                intent_id = str(_row_value(row, "intent_id", 1))
                previous = str(_row_value(row, "status", 2))
                cursor.execute(
                    "UPDATE v3_transactional_outbox SET status='UNKNOWN',lease_owner=NULL,lease_until=NULL,"
                    "fencing_token=%s,last_error=%s,updated_at=CURRENT_TIMESTAMP WHERE message_id=%s "
                    "AND status IN ('SENDING','SENT') AND ("
                    "lease_owner IS DISTINCT FROM %s OR fencing_token<%s "
                    "OR lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')"
                    ")",
                    (
                        self._fencing_token,
                        "FENCED_OR_LEASE_EXPIRED",
                        message_id,
                        self._lease_owner,
                        self._fencing_token,
                        margin,
                    ),
                )
                if getattr(cursor, "rowcount", 1) != 1:
                    continue
                OutboxWorker._sync_intent_state(cursor, intent_id=intent_id, state="UNKNOWN")
                OutboxWorker._append_transition(
                    cursor,
                    message_id=message_id,
                    intent_id=intent_id,
                    from_status=previous,
                    to_status="UNKNOWN",
                    event_type="FENCED_RECOVERY_UNKNOWN",
                    payload={"reason": "FENCED_OR_LEASE_EXPIRED"},
                    lease_owner=self._lease_owner,
                    fencing_token=self._fencing_token,
                )
                recovered += 1

            _recover_sending_execution_commands(
                cursor,
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
                expired_margin_seconds=margin,
            )
        return recovered

    def duplicate_order_count_24h(self) -> int | None:
        """Read duplicate identity facts from the durable intent table.

        A missing client ID, schema/query failure, or absent fencing token is
        not proof of zero duplicates and therefore returns ``None`` so the
        caller's R10 rule remains UNKNOWN/fail-closed.
        """

        if self._connection_factory is None or self._fencing_token <= 0:
            return None
        try:
            with (
                OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
                OutboxWorker._cursor_scope(conn) as cursor,
            ):
                cursor.execute(
                    "SELECT COUNT(*) AS total, COUNT(client_order_id) AS identified, "
                    "COUNT(DISTINCT client_order_id) AS distinct_client_ids, "
                    "COUNT(DISTINCT idempotency_key) AS distinct_idempotency_keys "
                    "FROM v3_order_intents "
                    "WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'"
                )
                row = cursor.fetchone()
            if row is None:
                return None
            total = int(_row_value(row, "total", 0) or 0)
            identified = int(_row_value(row, "identified", 1) or 0)
            distinct_client_ids = int(_row_value(row, "distinct_client_ids", 2) or 0)
            distinct_idempotency_keys = int(_row_value(row, "distinct_idempotency_keys", 3) or 0)
            if total != identified:
                return None
            return max(total - distinct_client_ids, total - distinct_idempotency_keys)
        except Exception:
            return None

    @property
    def stats(self) -> dict[str, Any]:
        if self._connection_factory is None:
            return {
                "state_counts": {},
                "pending_count": 0,
                "outbox_size": 0,
                "unknown_count": 0,
                "dead_letter_count": 0,
            }
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute("SELECT status,COUNT(*) FROM v3_transactional_outbox GROUP BY status")
            rows = cursor.fetchall() or []
        state_counts = {str(_row_value(row, "status", 0)): int(_row_value(row, "count", 1)) for row in rows}
        return {
            "state_counts": state_counts,
            "pending_count": sum(state_counts.get(status, 0) for status in ("PENDING", "SENDING", "UNKNOWN")),
            "outbox_size": sum(state_counts.values()),
            "unknown_count": state_counts.get("UNKNOWN", 0),
            "dead_letter_count": state_counts.get("DEAD_LETTER", 0),
        }

    def claim(self, owner: str, lease_seconds: float = 30.0) -> Any | None:
        """Synchronously claim one PENDING intent for the engine loop."""

        if self._connection_factory is None or self._fencing_token <= 0:
            return None
        if owner and owner != self._lease_owner:
            # The owner is part of the durable fencing identity. Allowing an
            # arbitrary per-call owner would make later ACK/FAIL transitions
            # unverifiable and strand the intent in SENDING.
            raise ValueError("OUTBOX_LEASE_OWNER_MISMATCH")
        claim_owner = self._lease_owner
        lease = max(1, int(lease_seconds or self._lease_seconds))
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT message_id,intent_id,payload::text FROM v3_transactional_outbox "
                "WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=CURRENT_TIMESTAMP) "
                "ORDER BY created_at,message_id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            message_id = str(_row_value(row, "message_id", 0))
            intent_id = str(_row_value(row, "intent_id", 1))
            cursor.execute(
                "UPDATE v3_transactional_outbox SET status='SENDING',lease_owner=%s,"
                "lease_until=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),fencing_token=%s,"
                "updated_at=CURRENT_TIMESTAMP WHERE message_id=%s AND status='PENDING' RETURNING payload::text",
                (claim_owner, lease, self._fencing_token, message_id),
            )
            updated = cursor.fetchone()
            if updated is None:
                return None
            OutboxWorker._sync_intent_state(cursor, intent_id=intent_id, state="SENDING")
            OutboxWorker._append_transition(
                cursor,
                message_id=message_id,
                intent_id=intent_id,
                from_status="PENDING",
                to_status="SENDING",
                event_type="CLAIMED",
                payload={"owner": claim_owner},
                lease_owner=claim_owner,
                fencing_token=self._fencing_token,
            )
            return self._deserialize_intent_payload(_row_value(updated, "payload", 0))

    def renew_lease(self, intent_id: str, lease_seconds: float = 60.0) -> bool:
        """Extend the current owner's lease on an in-flight parent intent.

        BD-FIX (multi-slice lease expiry): 切片间 pacing(TWAP 可达分钟级)
        远长于 claim 时的 30s 租约。租约过期后,同一 owner 的子命令转换
        被 fencing 谓词拒绝(EXECUTION_COMMAND_FENCED_OR_CONCURRENT),
        剩余切片永不发送。本方法只允许当前 owner + 当前 fencing
        generation 续租;不改变状态、不产生事件(续租不是状态转换)。
        """

        if self._connection_factory is None or self._fencing_token <= 0:
            return False
        lease = max(1, int(lease_seconds or self._lease_seconds))
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "UPDATE v3_transactional_outbox SET "
                "lease_until=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),updated_at=CURRENT_TIMESTAMP "
                "WHERE intent_id=%s AND status='SENDING' AND lease_owner=%s AND fencing_token=%s",
                (lease, str(intent_id), self._lease_owner, self._fencing_token),
            )
            return getattr(cursor, "rowcount", 1) == 1

    def _transition_intent(
        self,
        intent_id: str,
        *,
        target: str,
        reason: str = "",
        event_type: str,
        from_states: tuple[str, ...],
        owner_required: bool | None,
    ) -> bool:
        if self._connection_factory is None or self._fencing_token <= 0:
            return False
        if not from_states:
            raise ValueError("from_states must not be empty")
        with (
            OutboxWorker(connection_factory=self._connection_factory)._connection_scope() as conn,
            OutboxWorker._transaction(conn),
            OutboxWorker._cursor_scope(conn) as cursor,
        ):
            cursor.execute(
                "SELECT message_id,status,intent_id FROM v3_transactional_outbox WHERE intent_id=%s FOR UPDATE",
                (str(intent_id),),
            )
            current = cursor.fetchone()
            if current is None:
                return False
            message_id = str(_row_value(current, "message_id", 0))
            previous = str(_row_value(current, "status", 1))
            if previous in {"ACKED", "FAILED", "DEAD_LETTER"}:
                return previous == target

            placeholders = ",".join("%s" for _ in from_states)
            if owner_required is True:
                ownership_clause = "lease_owner=%s AND fencing_token=%s"
                ownership_params: tuple[Any, ...] = (self._lease_owner, self._fencing_token)
            elif owner_required is False:
                # UNKNOWN can only be resolved by a new fenced generation
                # after an independent venue query.  It must not be claimed by
                # an arbitrary owner or by an older fencing token.
                ownership_clause = "lease_owner IS NULL AND fencing_token<=%s"
                ownership_params = (self._fencing_token,)
            else:
                # Deterministic local rejection is allowed while still PENDING;
                # once a worker has claimed the intent, only that owner may
                # transition it.  This prevents a stale worker from ACKing or
                # failing another worker's in-flight command.
                ownership_clause = (
                    "((status='PENDING' AND lease_owner IS NULL AND fencing_token=0) "
                    "OR (status<>'PENDING' AND lease_owner=%s AND fencing_token=%s))"
                )
                ownership_params = (self._lease_owner, self._fencing_token)
            cursor.execute(
                "UPDATE v3_transactional_outbox SET status=%s,last_error=%s,"  # noqa: S608
                "lease_owner=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP "
                f"WHERE message_id=%s AND status IN ({placeholders}) AND {ownership_clause} "
                "RETURNING intent_id",
                (target, reason or None, message_id, *from_states, *ownership_params),
            )
            updated = cursor.fetchone()
            if updated is None:
                return False
            OutboxWorker._sync_intent_state(cursor, intent_id=str(intent_id), state=target)
            OutboxWorker._append_transition(
                cursor,
                message_id=message_id,
                intent_id=str(intent_id),
                from_status=previous,
                to_status=target,
                event_type=event_type,
                payload={"reason": reason} if reason else {},
                lease_owner=self._lease_owner,
                fencing_token=self._fencing_token,
            )
            return True

    def ack(self, intent_id: str, idempotency_key: str = "") -> None:
        self._transition_intent(
            str(intent_id),
            target="ACKED",
            event_type="ENGINE_ACK",
            from_states=("SENDING", "SENT"),
            owner_required=True,
        )

    def reject(self, intent_id: str, reason: str, idempotency_key: str = "") -> None:
        self._transition_intent(
            str(intent_id),
            target="FAILED",
            reason=reason,
            event_type="REJECTED",
            from_states=("PENDING", "SENDING", "SENT"),
            owner_required=None,
        )

    def dead_letter(self, intent_id: str, reason: str, idempotency_key: str = "") -> None:
        self._transition_intent(
            str(intent_id),
            target="DEAD_LETTER",
            reason=reason,
            event_type="DEAD_LETTER",
            from_states=("SENDING", "SENT"),
            owner_required=True,
        )

    def mark_unknown(self, intent_id: str, reason: str) -> None:
        self._transition_intent(
            str(intent_id),
            target="UNKNOWN",
            reason=reason,
            event_type="UNKNOWN",
            from_states=("SENDING", "SENT"),
            owner_required=True,
        )

    def resolve_unknown(self, intent_id: str, *, exchange_order_found: bool) -> None:
        target = "ACKED" if exchange_order_found else "PENDING"
        self._transition_intent(
            str(intent_id),
            target=target,
            event_type="UNKNOWN_RESOLVED_FOUND" if exchange_order_found else "UNKNOWN_RESOLVED_ABSENT",
            from_states=("UNKNOWN",),
            owner_required=False,
        )
