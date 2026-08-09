"""Order Intent、Outbox/Inbox、幂等与事务边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueId,
)

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

    ``db_path`` 非空时使用 SQLite WAL 持久化 intent/outbox 状态。默认无
    路径仍保留轻量内存实现供单元测试；生产路径必须显式注入数据库路径。
    """

    _MAX_OUTBOX_SIZE = 1000  # 清理前最大在途 + 已处理条目数
    _MAX_PROCESSED_RETENTION = 500  # 已处理条目保留上限

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._db_lock = threading.RLock()
        self._memory_outbox: list[OrderIntent] = []
        self._inbox: dict[str, OrderIntent] = {}
        self._processed: set[str] = set()
        self._states: dict[str, OutboxState] = {}
        # P0 修复: 死信追踪 — _groom() 丢弃的 intent 计数
        self._dead_letter_count: int = 0
        self._dead_letter_ids: list[str] = []  # 最近 20 个丢弃的 intent_id
        self._total_committed: int = 0
        self._total_acked: int = 0
        if self._db_path:
            self._init_db()
            self._recover_after_restart()

    @property
    def _outbox(self) -> list[OrderIntent]:
        """兼容旧诊断代码；持久模式从数据库读取未终态 intent。"""

        if self._db_path:
            return self.unacked()
        return self._memory_outbox

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path:
            raise RuntimeError("SQLite connection requested for memory outbox")
        path = Path(self._db_path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._db_lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS intent_outbox (
                    idempotency_key TEXT PRIMARY KEY NOT NULL,
                    intent_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_intent_outbox_state
                    ON intent_outbox(state, created_at);
                """
            )

    @staticmethod
    def _serialize_intent(intent: OrderIntent, key: str) -> str:
        return json.dumps(
            {
                "intent_id": intent.intent_id,
                "account_venue_id": str(intent.account_ref.venue_id),
                "account_id": str(intent.account_ref.account_id),
                "instrument_id": str(intent.instrument_id),
                "side": intent.side.value,
                "order_type": intent.order_type.value,
                "quantity": intent.quantity.amount,
                "quantity_decimals": intent.quantity.decimals,
                "price": intent.price.amount if intent.price else None,
                "price_decimals": intent.price.decimals if intent.price else 8,
                "time_in_force": intent.time_in_force.value,
                "client_order_id": intent.client_order_id,
                "correlation_id": str(intent.correlation_id) if intent.correlation_id else None,
                "idempotency_key": key,
                "created_at": intent.created_at.isoformat(),
                "risk_approval_id": intent.risk_approval_id,
                "reduce_only": intent.reduce_only,
                "close_position": intent.close_position,
                "emergency_policy_signed": intent.emergency_policy_signed,
                "risk_approval_signature": intent.risk_approval_signature,
                "risk_proposal_hash": intent.risk_proposal_hash,
                "risk_account_snapshot_hash": intent.risk_account_snapshot_hash,
                "risk_snapshot_hash": intent.risk_snapshot_hash,
                "risk_policy_version": intent.risk_policy_version,
                "risk_nonce": intent.risk_nonce,
                "risk_expires_at": intent.risk_expires_at,
                "net_alpha_bps": intent.net_alpha_bps,
                "predicted_cost_bps": intent.predicted_cost_bps,
            },
            sort_keys=True,
        )

    @staticmethod
    def _deserialize_intent(payload: str) -> OrderIntent:
        # Local import avoids the execution package's intentional circularity.
        from beidou_safety.execution import OrderIntent

        data = json.loads(payload)
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        price = Price(amount=str(data["price"]), decimals=int(data.get("price_decimals", 8))) if data.get("price") is not None else None
        correlation_id = data.get("correlation_id")
        return OrderIntent(
            intent_id=str(data["intent_id"]),
            account_ref=AccountRef(
                venue_id=VenueId(str(data["account_venue_id"])),
                account_id=AccountId(str(data["account_id"])),
            ),
            instrument_id=InstrumentId(str(data["instrument_id"])),
            side=OrderSide(str(data["side"])),
            order_type=OrderType(str(data["order_type"])),
            quantity=Quantity(amount=str(data["quantity"]), decimals=int(data.get("quantity_decimals", 8))),
            price=price,
            time_in_force=TimeInForce(str(data.get("time_in_force", TimeInForce.GTC.value))),
            client_order_id=data.get("client_order_id"),
            correlation_id=CorrelationId(str(correlation_id)) if correlation_id else None,
            idempotency_key=str(data.get("idempotency_key", "")),
            created_at=created_at.astimezone(timezone.utc),
            risk_approval_id=data.get("risk_approval_id"),
            reduce_only=bool(data.get("reduce_only", False)),
            close_position=bool(data.get("close_position", False)),
            emergency_policy_signed=bool(data.get("emergency_policy_signed", False)),
            risk_approval_signature=data.get("risk_approval_signature"),
            risk_proposal_hash=str(data.get("risk_proposal_hash", "")),
            risk_account_snapshot_hash=str(data.get("risk_account_snapshot_hash", "")),
            risk_snapshot_hash=str(data.get("risk_snapshot_hash", "")),
            risk_policy_version=str(data.get("risk_policy_version", "")),
            risk_nonce=str(data.get("risk_nonce", "")),
            risk_expires_at=float(data["risk_expires_at"]) if data.get("risk_expires_at") is not None else None,
            net_alpha_bps=float(data.get("net_alpha_bps", 0.0)),
            predicted_cost_bps=float(data.get("predicted_cost_bps", 0.0)),
        )

    def _recover_after_restart(self) -> None:
        """中断中的发送不自动重发，先变为 UNKNOWN 等待查询裁决。"""

        if not self._db_path:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._db_lock, self._connect() as conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                "WHERE state IN (?, ?)",
                (OutboxState.UNKNOWN.value, now, OutboxState.SENDING.value, OutboxState.SENT.value),
            )

    def _db_intents(self, states: tuple[str, ...]) -> list[OrderIntent]:
        if not self._db_path:
            return []
        if len(states) != 3:
            raise ValueError("persistent outbox state query requires exactly three states")
        with self._db_lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM intent_outbox WHERE state IN (?, ?, ?) ORDER BY created_at",
                states,
            ).fetchall()
        return [self._deserialize_intent(str(row["payload"])) for row in rows]

    @property
    def dead_letter_count(self) -> int:
        return self._dead_letter_count

    @property
    def dead_letter_ids(self) -> list[str]:
        return list(self._dead_letter_ids)

    @property
    def stats(self) -> dict:
        if self._db_path:
            with self._db_lock, self._connect() as conn:
                counts = conn.execute(
                    "SELECT state, COUNT(*) AS count FROM intent_outbox GROUP BY state"
                ).fetchall()
            state_counts = {str(row["state"]): int(row["count"]) for row in counts}
            return {
                "outbox_size": sum(state_counts.values()),
                "inbox_size": 0,
                "processed_count": state_counts.get(OutboxState.ACKED.value, 0),
                "dead_letter_count": self._dead_letter_count,
                "total_committed": sum(state_counts.values()),
                "total_acked": state_counts.get(OutboxState.ACKED.value, 0),
                "pending_count": sum(
                    state_counts.get(state.value, 0)
                    for state in (OutboxState.PENDING, OutboxState.SENDING, OutboxState.UNKNOWN)
                ),
            }
        return {
            "outbox_size": len(self._memory_outbox),
            "inbox_size": len(self._inbox),
            "processed_count": len(self._processed),
            "dead_letter_count": self._dead_letter_count,
            "total_committed": self._total_committed,
            "total_acked": self._total_acked,
            "pending_count": self.pending_count(),
        }

    def commit(self, intent: OrderIntent) -> str:
        key = intent.idempotency_key or self._hash(intent)
        if self._db_path:
            now = datetime.now(timezone.utc).isoformat()
            payload = self._serialize_intent(intent, key)
            try:
                with self._db_lock, self._connect() as conn:
                    conn.execute(
                        "INSERT INTO intent_outbox "
                        "(idempotency_key,intent_id,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        (key, intent.intent_id, payload, OutboxState.PENDING.value, now, now),
                    )
                return key
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Duplicate intent: {key}") from exc
        if key in self._processed or any(i.idempotency_key == key or self._hash(i) == key for i in self._outbox):
            raise ValueError(f"Duplicate intent: {key}")
        self._memory_outbox.append(intent)
        self._states[key] = OutboxState.PENDING
        self._total_committed += 1
        self._groom()
        return key

    def _groom(self) -> None:
        """清理内存：移除已 ack 的过期条目，防止内存无限增长。

        P0 修复: 超过上限时记录死信告警而非静默丢弃。
        """
        if len(self._memory_outbox) > self._MAX_OUTBOX_SIZE:
            keep = self._MAX_OUTBOX_SIZE // 2
            removed = self._memory_outbox[:-keep]
            self._memory_outbox = self._memory_outbox[-keep:]
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
                import json
                import os
                import time
                from pathlib import Path
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
            except Exception as exc:
                logger.warning("Failed to persist outbox groom evidence: %s", exc)
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
        if self._db_path:
            now = datetime.now(timezone.utc).isoformat()
            with self._db_lock, self._connect() as conn:
                if idempotency_key:
                    conn.execute(
                        "UPDATE intent_outbox SET state=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                        "WHERE idempotency_key=? OR intent_id=?",
                        (OutboxState.ACKED.value, now, idempotency_key, intent_id),
                    )
                else:
                    conn.execute(
                        "UPDATE intent_outbox SET state=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE intent_id=?",
                        (OutboxState.ACKED.value, now, intent_id),
                    )
            self._total_acked += 1
            return
        self._processed.add(intent_id)
        if idempotency_key:
            self._processed.add(idempotency_key)
        self._inbox.pop(intent_id, None)
        self._memory_outbox = [i for i in self._memory_outbox if i.intent_id != intent_id]
        self._total_acked += 1
        stale_keys = [k for k, v in self._states.items() if v == OutboxState.PENDING]
        for k in stale_keys:
            if len(self._states) > self._MAX_PROCESSED_RETENTION:
                self._states.pop(k, None)

    def reject(self, intent_id: str, reason: str, idempotency_key: str = "") -> None:
        """在交易所写入前的确定性拒绝，保留幂等记录和失败原因。"""

        if self._db_path:
            now = datetime.now(timezone.utc).isoformat()
            with self._db_lock, self._connect() as conn:
                conn.execute(
                    "UPDATE intent_outbox SET state=?, last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                    "WHERE intent_id=? OR idempotency_key=?",
                    (OutboxState.FAILED.value, reason, now, intent_id, idempotency_key),
                )
            return
        self._processed.add(intent_id)
        if idempotency_key:
            self._processed.add(idempotency_key)
        self._memory_outbox = [i for i in self._memory_outbox if i.intent_id != intent_id]
        self._states[intent_id] = OutboxState.FAILED

    def unacked(self) -> list[OrderIntent]:
        if self._db_path:
            return self._db_intents(
                (OutboxState.PENDING.value, OutboxState.SENDING.value, OutboxState.UNKNOWN.value)
            )
        # Check both _inbox and _outbox for unprocessed intents
        inbox_unacked = [v for k, v in self._inbox.items() if k not in self._processed]
        outbox_unacked = [i for i in self._memory_outbox if i.intent_id not in self._processed]
        return inbox_unacked + outbox_unacked

    def pending_count(self) -> int:
        if self._db_path:
            with self._db_lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM intent_outbox WHERE state IN (?, ?, ?)",
                    (OutboxState.PENDING.value, OutboxState.SENDING.value, OutboxState.UNKNOWN.value),
                ).fetchone()
            return int(row["count"] if row else 0)
        return max(0, len(self._memory_outbox))

    def claim(self, owner: str, lease_seconds: float = 30.0) -> OrderIntent | None:
        """单写者 claim；UNKNOWN 不会被盲目重发。"""

        if not self._db_path:
            pending = self._memory_outbox[0] if self._memory_outbox else None
            return pending
        now = datetime.now(timezone.utc).isoformat()
        lease_until = datetime.now(timezone.utc).timestamp() + lease_seconds
        with self._db_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT idempotency_key,payload FROM intent_outbox WHERE state=? ORDER BY created_at LIMIT 1",
                (OutboxState.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                "UPDATE intent_outbox SET state=?, lease_owner=?, lease_until=?, updated_at=? "
                "WHERE idempotency_key=? AND state=?",
                (
                    OutboxState.SENDING.value,
                    owner,
                    lease_until,
                    now,
                    row["idempotency_key"],
                    OutboxState.PENDING.value,
                ),
            ).rowcount
            if updated != 1:
                return None
            return self._deserialize_intent(str(row["payload"]))

    def mark_unknown(self, intent_id: str, reason: str) -> None:
        if not self._db_path:
            self._states[intent_id] = OutboxState.UNKNOWN
            return
        with self._db_lock, self._connect() as conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE intent_id=?",
                (OutboxState.UNKNOWN.value, reason, datetime.now(timezone.utc).isoformat(), intent_id),
            )

    def resolve_unknown(self, intent_id: str, *, exchange_order_found: bool) -> None:
        """只有查询得出结论后才允许 ACK 或重新进入 PENDING。"""

        state = OutboxState.ACKED.value if exchange_order_found else OutboxState.PENDING.value
        if not self._db_path:
            self._states[intent_id] = OutboxState(state)
            return
        with self._db_lock, self._connect() as conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, last_error=NULL, updated_at=? WHERE intent_id=? AND state=?",
                (state, datetime.now(timezone.utc).isoformat(), intent_id, OutboxState.UNKNOWN.value),
            )
