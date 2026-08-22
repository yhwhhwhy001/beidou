"""Order Intent、Outbox/Inbox、幂等与事务边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
        self._memory_execution_plans: dict[str, Any] = {}
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
        with self._db_lock, closing(self._connect()) as conn, conn:
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
                CREATE TABLE IF NOT EXISTS risk_approvals (
                    approval_id TEXT PRIMARY KEY NOT NULL,
                    intent_id TEXT UNIQUE NOT NULL,
                    decision TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    account_snapshot_hash TEXT NOT NULL,
                    risk_snapshot_hash TEXT NOT NULL,
                    intent_hash TEXT NOT NULL DEFAULT '',
                    policy_version TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_approvals_intent
                    ON risk_approvals(intent_id);
                CREATE TABLE IF NOT EXISTS execution_commands (
                    parent_intent_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    command_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    exchange_order_id TEXT,
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (parent_intent_id, sequence),
                    FOREIGN KEY (parent_intent_id) REFERENCES intent_outbox(intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_commands_parent_state
                    ON execution_commands(parent_intent_id, state, sequence);
                CREATE TABLE IF NOT EXISTS execution_command_events (
                    event_id TEXT PRIMARY KEY NOT NULL,
                    parent_intent_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    FOREIGN KEY (parent_intent_id, sequence)
                        REFERENCES execution_commands(parent_intent_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_command_events_parent
                    ON execution_command_events(parent_intent_id, sequence, occurred_at, event_id);
                """
            )
            approval_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(risk_approvals)").fetchall()}
            if "intent_hash" not in approval_columns:
                conn.execute("ALTER TABLE risk_approvals ADD COLUMN intent_hash TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _serialize_intent(intent: OrderIntent, key: str) -> str:
        risk_expires_at = intent.risk_expires_at
        if isinstance(risk_expires_at, datetime):
            risk_expires_at = risk_expires_at.timestamp()
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
                "risk_intent_hash": intent.risk_intent_hash,
                "risk_account_snapshot_hash": intent.risk_account_snapshot_hash,
                "risk_snapshot_hash": intent.risk_snapshot_hash,
                "risk_policy_version": intent.risk_policy_version,
                "risk_nonce": intent.risk_nonce,
                "risk_expires_at": risk_expires_at,
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
        price = (
            Price(amount=str(data["price"]), decimals=int(data.get("price_decimals", 8)))
            if data.get("price") is not None
            else None
        )
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
            risk_intent_hash=str(data.get("risk_intent_hash", "")),
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
        with self._db_lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                "WHERE state IN (?, ?)",
                (OutboxState.UNKNOWN.value, now, OutboxState.SENDING.value, OutboxState.SENT.value),
            )
            rows = conn.execute(
                "SELECT parent_intent_id,sequence,payload FROM execution_commands WHERE state=?",
                ("SENDING",),
            ).fetchall()
            for row in rows:
                payload = json.loads(str(row["payload"]))
                payload["state"] = "UNKNOWN"
                event_id = f"restart-unknown:{row['parent_intent_id']}:{row['sequence']}"
                event_ids = [str(value) for value in payload.get("event_ids", [])]
                if event_id not in event_ids:
                    event_ids.append(event_id)
                payload["event_ids"] = event_ids
                conn.execute(
                    "UPDATE execution_commands SET state=?,payload=?,updated_at=? "
                    "WHERE parent_intent_id=? AND sequence=? AND state=?",
                    (
                        "UNKNOWN",
                        json.dumps(payload, sort_keys=True),
                        now,
                        str(row["parent_intent_id"]),
                        int(row["sequence"]),
                        "SENDING",
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO execution_command_events "
                    "(event_id,parent_intent_id,sequence,from_state,to_state,payload,occurred_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        event_id,
                        str(row["parent_intent_id"]),
                        int(row["sequence"]),
                        "SENDING",
                        "UNKNOWN",
                        json.dumps({"reason": "PROCESS_RESTART_DURING_SEND"}, sort_keys=True),
                        now,
                    ),
                )

    @staticmethod
    def _execution_aggregate_from_payloads(intent_id: str, payloads: list[str]) -> Any:
        from beidou_safety.execution.command_aggregate import ExecutionChildCommand, ParentExecutionAggregate

        children = [ExecutionChildCommand.from_payload(json.loads(payload)) for payload in payloads]
        return ParentExecutionAggregate.create(str(intent_id), children)

    def persist_execution_plan(self, intent_id: str, children: list[Any]) -> Any:
        """Persist every immutable child before the first venue write."""

        from beidou_safety.execution.command_aggregate import ParentExecutionAggregate

        aggregate = ParentExecutionAggregate.create(str(intent_id), children)
        if not self._db_path:
            parent = next(
                (intent for intent in self._memory_outbox if str(intent.intent_id) == str(intent_id)),
                None,
            )
            parent_key = parent.idempotency_key or self._hash(parent) if parent is not None else ""
            if parent is None or self._states.get(parent_key) is not OutboxState.SENDING:
                raise ValueError("EXECUTION_PLAN_PARENT_NOT_CLAIMED")
            existing = self._memory_execution_plans.get(str(intent_id))
            if existing is not None:
                if [child.command_hash for child in existing.children] != [
                    child.command_hash for child in aggregate.children
                ]:
                    raise ValueError("EXECUTION_PLAN_CONFLICT")
                return existing
            self._memory_execution_plans[str(intent_id)] = aggregate
            return aggregate

        now = datetime.now(timezone.utc).isoformat()
        with self._db_lock, closing(self._connect()) as conn, conn:
            parent = conn.execute(
                "SELECT intent_id FROM intent_outbox WHERE intent_id=? AND state=?",
                (str(intent_id), OutboxState.SENDING.value),
            ).fetchone()
            if parent is None:
                raise ValueError("EXECUTION_PLAN_PARENT_NOT_CLAIMED")
            rows = conn.execute(
                "SELECT command_hash,payload FROM execution_commands WHERE parent_intent_id=? ORDER BY sequence",
                (str(intent_id),),
            ).fetchall()
            if rows:
                existing_hashes = [str(row["command_hash"]) for row in rows]
                requested_hashes = [child.command_hash for child in aggregate.children]
                if existing_hashes != requested_hashes:
                    raise ValueError("EXECUTION_PLAN_CONFLICT")
                return self._execution_aggregate_from_payloads(str(intent_id), [str(row["payload"]) for row in rows])
            for child in aggregate.children:
                payload = json.dumps(child.to_payload(), sort_keys=True)
                conn.execute(
                    "INSERT INTO execution_commands "
                    "(parent_intent_id,sequence,client_order_id,command_hash,payload,state,"
                    "exchange_order_id,filled_quantity,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(intent_id),
                        child.sequence,
                        child.client_order_id,
                        child.command_hash,
                        payload,
                        child.state.value,
                        None,
                        str(child.filled_quantity),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO execution_command_events "
                    "(event_id,parent_intent_id,sequence,from_state,to_state,payload,occurred_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"plan:{intent_id}:{child.sequence}",
                        str(intent_id),
                        child.sequence,
                        None,
                        child.state.value,
                        json.dumps({"command_hash": child.command_hash}, sort_keys=True),
                        now,
                    ),
                )
        return aggregate

    def restore_execution_plan(self, intent_id: str) -> Any | None:
        if not self._db_path:
            return self._memory_execution_plans.get(str(intent_id))
        with self._db_lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT payload FROM execution_commands WHERE parent_intent_id=? ORDER BY sequence",
                (str(intent_id),),
            ).fetchall()
        if not rows:
            return None
        return self._execution_aggregate_from_payloads(str(intent_id), [str(row["payload"]) for row in rows])

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
        """Apply one idempotent child event and return the rebuilt parent."""

        if not self._db_path:
            aggregate = self.restore_execution_plan(intent_id)
            if aggregate is None:
                raise ValueError("EXECUTION_PLAN_NOT_FOUND")
            updated = aggregate.transition_child(
                sequence,
                state,
                event_id=event_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_quantity=cumulative_filled_quantity,
            )
            self._memory_execution_plans[str(intent_id)] = updated
            return updated

        now = datetime.now(timezone.utc).isoformat()
        with self._db_lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT payload FROM execution_commands WHERE parent_intent_id=? ORDER BY sequence",
                (str(intent_id),),
            ).fetchall()
            if not rows:
                raise ValueError("EXECUTION_PLAN_NOT_FOUND")
            duplicate = conn.execute(
                "SELECT event_id FROM execution_command_events WHERE event_id=?",
                (str(event_id),),
            ).fetchone()
            aggregate = self._execution_aggregate_from_payloads(str(intent_id), [str(row["payload"]) for row in rows])
            if duplicate is not None:
                return aggregate
            previous = aggregate.children[sequence].state.value if 0 <= sequence < len(aggregate.children) else ""
            updated = aggregate.transition_child(
                sequence,
                state,
                event_id=event_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_quantity=cumulative_filled_quantity,
            )
            child = updated.children[sequence]
            changed = conn.execute(
                "UPDATE execution_commands SET payload=?,state=?,exchange_order_id=?,filled_quantity=?,updated_at=? "
                "WHERE parent_intent_id=? AND sequence=? AND command_hash=?",
                (
                    json.dumps(child.to_payload(), sort_keys=True),
                    child.state.value,
                    child.exchange_order_id or None,
                    str(child.filled_quantity),
                    now,
                    str(intent_id),
                    sequence,
                    child.command_hash,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("EXECUTION_COMMAND_CONCURRENT_CHANGE")
            conn.execute(
                "INSERT INTO execution_command_events "
                "(event_id,parent_intent_id,sequence,from_state,to_state,payload,occurred_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(event_id),
                    str(intent_id),
                    sequence,
                    previous or None,
                    child.state.value,
                    json.dumps(
                        {
                            "exchange_order_id": child.exchange_order_id,
                            "cumulative_filled_quantity": str(child.filled_quantity),
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        return updated

    def project_user_order_update(self, update: Any) -> Any:
        """Project one normalized venue order event into its exact child."""

        from beidou_safety.execution.command_aggregate import ChildCommandState

        client_order_id = str(getattr(update, "client_order_id", "") or "")
        if not client_order_id:
            raise ValueError("USER_ORDER_UPDATE_CLIENT_ID_MISSING")
        if not self._db_path:
            identity = next(
                (
                    (intent_id, child.sequence)
                    for intent_id, aggregate in self._memory_execution_plans.items()
                    for child in aggregate.children
                    if child.client_order_id == client_order_id
                ),
                None,
            )
        else:
            with self._db_lock, closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT parent_intent_id,sequence FROM execution_commands WHERE client_order_id=?",
                    (client_order_id,),
                ).fetchone()
            identity = (str(row["parent_intent_id"]), int(row["sequence"])) if row is not None else None
        if identity is None:
            # The account stream also contains protection orders, orders from
            # older deployments and externally owned orders.  They still
            # belong in the global durable projector, but must not mutate or
            # invalidate an unrelated execution aggregate.
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
                identity[0],
                identity[1],
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
                return self.restore_execution_plan(str(identity[0]))
            raise

    def inflight_signed_quantity(self, symbol: str) -> Decimal:
        """Return signed remaining child exposure, including UNKNOWN."""

        symbol_value = str(symbol).upper()
        if not self._db_path:
            aggregates = list(self._memory_execution_plans.values())
        else:
            with self._db_lock, closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT parent_intent_id,payload FROM execution_commands "
                    "WHERE state IN ('PLANNED','SENDING','ACKED','PARTIALLY_FILLED','UNKNOWN') "
                    "ORDER BY parent_intent_id,sequence"
                ).fetchall()
            by_parent: dict[str, list[str]] = {}
            for row in rows:
                by_parent.setdefault(str(row["parent_intent_id"]), []).append(str(row["payload"]))
            aggregates = [
                self._execution_aggregate_from_payloads(parent_id, payloads)
                for parent_id, payloads in by_parent.items()
            ]
        return sum(
            (
                child.signed_remaining_quantity
                for aggregate in aggregates
                for child in aggregate.children
                if child.symbol == symbol_value
            ),
            Decimal("0"),
        )

    def _db_intents(self, states: tuple[str, ...]) -> list[OrderIntent]:
        if not self._db_path:
            return []
        if len(states) != 3:
            raise ValueError("persistent outbox state query requires exactly three states")
        with self._db_lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT payload FROM intent_outbox WHERE state IN (?, ?, ?) ORDER BY created_at",
                states,
            ).fetchall()
        return [self._deserialize_intent(str(row["payload"])) for row in rows]

    @property
    def dead_letter_count(self) -> int:
        return self._dead_letter_count

    def dead_letter(self, intent_id: str, reason: str, idempotency_key: str = "") -> None:
        """标记 intent 为永久死信，不再重试。"""
        intent_id_str = str(intent_id)
        self._dead_letter_count += 1
        self._dead_letter_ids.append(intent_id_str)
        if len(self._dead_letter_ids) > 20:
            self._dead_letter_ids = self._dead_letter_ids[-20:]
        # 从活跃状态移除
        self._inbox.pop(intent_id_str, None)
        self._states.pop(intent_id_str, None)
        if self._db_path:
            with self._db_lock, closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE intent_outbox SET state=? WHERE intent_id=?",
                    ("DEAD_LETTER", intent_id_str),
                )
                conn.commit()

    @property
    def dead_letter_ids(self) -> list[str]:
        return list(self._dead_letter_ids)

    def duplicate_order_count_24h(self) -> int | None:
        """Return durable duplicate-identity count for the last 24 hours.

        The in-memory compatibility queue is not an authoritative execution
        source, so it deliberately returns ``None``.  Persistent SQLite rows
        must carry both a client order ID and an idempotency key; malformed or
        unidentified rows remain UNKNOWN rather than being counted as zero.
        """

        if not self._db_path:
            return None
        with self._db_lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT payload FROM intent_outbox "
                "WHERE julianday(created_at) >= julianday('now', '-24 hours') ORDER BY created_at"
            ).fetchall()
        client_ids: list[str] = []
        idempotency_keys: list[str] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                return None
            if not isinstance(payload, dict):
                return None
            client_order_id = str(payload.get("client_order_id") or "")
            idempotency_key = str(payload.get("idempotency_key") or "")
            if not client_order_id or not idempotency_key:
                return None
            client_ids.append(client_order_id)
            idempotency_keys.append(idempotency_key)
        return max(len(client_ids) - len(set(client_ids)), len(idempotency_keys) - len(set(idempotency_keys)))

    @property
    def stats(self) -> dict:
        if self._db_path:
            with self._db_lock, closing(self._connect()) as conn, conn:
                counts = conn.execute("SELECT state, COUNT(*) AS count FROM intent_outbox GROUP BY state").fetchall()
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
                # Readiness/reconciliation consumers must be able to see
                # UNKNOWN/DEAD_LETTER explicitly; a single pending_count is
                # insufficient to distinguish a recoverable queue from an
                # ambiguous execution fact.
                "state_counts": state_counts,
            }
        state_counts_memory: dict[str, int] = {}
        for state in self._states.values():
            state_counts_memory[state.value] = state_counts_memory.get(state.value, 0) + 1
        return {
            "outbox_size": len(self._memory_outbox),
            "inbox_size": len(self._inbox),
            "processed_count": len(self._processed),
            "dead_letter_count": self._dead_letter_count,
            "total_committed": self._total_committed,
            "total_acked": self._total_acked,
            "pending_count": self.pending_count(),
            # memory 分支必须用 memory 统计(UnboundLocalError 修复)
            "state_counts": state_counts_memory,
        }

    def commit(self, intent: OrderIntent) -> str:
        key = intent.idempotency_key or self._hash(intent)
        if self._db_path:
            now = datetime.now(timezone.utc).isoformat()
            payload = self._serialize_intent(intent, key)
            try:
                with self._db_lock, closing(self._connect()) as conn, conn:
                    approval_id = str(intent.risk_approval_id or "")
                    signature = str(intent.risk_approval_signature or "")
                    if bool(approval_id) != bool(signature):
                        raise ValueError("RISK_APPROVAL_ENVELOPE_INCOMPLETE")
                    if approval_id and signature:
                        if intent.risk_expires_at is None:
                            raise ValueError("Risk approval expiry is required for durable intent commit")
                        required_approval_fields = {
                            "risk_proposal_hash": intent.risk_proposal_hash,
                            "risk_intent_hash": intent.risk_intent_hash,
                            "risk_account_snapshot_hash": intent.risk_account_snapshot_hash,
                            "risk_snapshot_hash": intent.risk_snapshot_hash,
                            "risk_policy_version": intent.risk_policy_version,
                            "risk_nonce": intent.risk_nonce,
                        }
                        missing = [name for name, value in required_approval_fields.items() if not str(value or "")]
                        if missing:
                            raise ValueError(f"RISK_APPROVAL_FIELDS_REQUIRED: {','.join(missing)}")
                        # Approval and intent are committed in this same SQLite
                        # transaction.  The signing key never enters the DB;
                        # only the signed envelope needed for restart
                        # verification is durable.
                        conn.execute(
                            "INSERT INTO risk_approvals "
                            "(approval_id,intent_id,decision,signature,proposal_hash,account_snapshot_hash,"
                            "risk_snapshot_hash,intent_hash,policy_version,nonce,expires_at,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                approval_id,
                                intent.intent_id,
                                "APPROVED",
                                signature,
                                str(intent.risk_proposal_hash),
                                str(intent.risk_account_snapshot_hash),
                                str(intent.risk_snapshot_hash),
                                str(intent.risk_intent_hash),
                                str(intent.risk_policy_version),
                                str(intent.risk_nonce),
                                float(intent.risk_expires_at),
                                now,
                                now,
                            ),
                        )
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

    def restore_pending_approvals(self) -> list[dict[str, object]]:
        """Restore only approvals attached to unresolved durable intents.

        ACKED/FAILED approvals are intentionally excluded: an approval is
        single-use and a restart must not make a completed intent reusable.
        The returned envelope contains no signing key.
        """

        if not self._db_path:
            return []
        with self._db_lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT r.approval_id,r.intent_id,r.decision,r.signature,r.proposal_hash,"
                "r.account_snapshot_hash,r.risk_snapshot_hash,r.intent_hash,r.policy_version,r.nonce,r.expires_at "
                "FROM risk_approvals AS r JOIN intent_outbox AS i ON i.intent_id=r.intent_id "
                "WHERE i.state IN (?, ?, ?) AND r.decision=? ORDER BY r.created_at",
                (
                    OutboxState.PENDING.value,
                    OutboxState.SENDING.value,
                    OutboxState.UNKNOWN.value,
                    "APPROVED",
                ),
            ).fetchall()
        return [dict(row) for row in rows]

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
            for pid in stale[: max(0, excess)]:
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
            with self._db_lock, closing(self._connect()) as conn, conn:
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
        state_key = idempotency_key
        if not state_key:
            matched = next((item for item in self._memory_outbox if item.intent_id == intent_id), None)
            if matched is not None:
                state_key = matched.idempotency_key or self._hash(matched)
        self._inbox.pop(intent_id, None)
        self._memory_outbox = [i for i in self._memory_outbox if i.intent_id != intent_id]
        if state_key:
            self._states[state_key] = OutboxState.ACKED
        self._total_acked += 1
        stale_keys = [k for k, v in self._states.items() if v == OutboxState.PENDING]
        for k in stale_keys:
            if len(self._states) > self._MAX_PROCESSED_RETENTION:
                self._states.pop(k, None)

    def reject(self, intent_id: str, reason: str, idempotency_key: str = "") -> None:
        """在交易所写入前的确定性拒绝，保留幂等记录和失败原因。"""

        if self._db_path:
            now = datetime.now(timezone.utc).isoformat()
            with self._db_lock, closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE intent_outbox SET state=?, last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                    "WHERE intent_id=? OR idempotency_key=?",
                    (OutboxState.FAILED.value, reason, now, intent_id, idempotency_key),
                )
            return
        self._processed.add(intent_id)
        if idempotency_key:
            self._processed.add(idempotency_key)
        key = idempotency_key
        if not key:
            for item in self._memory_outbox:
                if item.intent_id == intent_id:
                    key = item.idempotency_key or self._hash(item)
                    break
        self._memory_outbox = [i for i in self._memory_outbox if i.intent_id != intent_id]
        if key:
            self._states[key] = OutboxState.FAILED

    def unacked(self) -> list[OrderIntent]:
        if self._db_path:
            return self._db_intents((OutboxState.PENDING.value, OutboxState.SENDING.value, OutboxState.UNKNOWN.value))
        # Check both _inbox and _outbox for unprocessed intents.  UNKNOWN is
        # deliberately excluded: it requires an independent venue query
        # before any retry, even in the compatibility/in-memory path.
        inbox_unacked = [
            v
            for k, v in self._inbox.items()
            if k not in self._processed
            and self._states.get(v.idempotency_key or self._hash(v), OutboxState.PENDING)
            in {OutboxState.PENDING, OutboxState.SENDING}
        ]
        outbox_unacked = [
            i
            for i in self._memory_outbox
            if i.intent_id not in self._processed
            and self._states.get(i.idempotency_key or self._hash(i), OutboxState.PENDING)
            in {OutboxState.PENDING, OutboxState.SENDING}
        ]
        return inbox_unacked + outbox_unacked

    def pending_count(self) -> int:
        if self._db_path:
            with self._db_lock, closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM intent_outbox WHERE state IN (?, ?, ?)",
                    (OutboxState.PENDING.value, OutboxState.SENDING.value, OutboxState.UNKNOWN.value),
                ).fetchone()
            return int(row["count"] if row else 0)
        return max(0, len(self._memory_outbox))

    def claim(self, owner: str, lease_seconds: float = 30.0) -> OrderIntent | None:
        """单写者 claim；UNKNOWN 不会被盲目重发。"""

        if not self._db_path:
            for pending in self._memory_outbox:
                if pending.intent_id in self._processed:
                    continue
                key = pending.idempotency_key or self._hash(pending)
                if self._states.get(key, OutboxState.PENDING) is not OutboxState.PENDING:
                    continue
                self._states[key] = OutboxState.SENDING
                return pending
            return None
        now = datetime.now(timezone.utc).isoformat()
        lease_until = datetime.now(timezone.utc).timestamp() + lease_seconds
        with self._db_lock, closing(self._connect()) as conn, conn:
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
            for intent in self._memory_outbox:
                if intent.intent_id == intent_id:
                    self._states[intent.idempotency_key or self._hash(intent)] = OutboxState.UNKNOWN
                    break
            return
        with self._db_lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE intent_id=?",
                (OutboxState.UNKNOWN.value, reason, datetime.now(timezone.utc).isoformat(), intent_id),
            )

    def get_unknown_intents(self) -> list[dict[str, str]]:
        """Return only the stable identity fields required for venue recovery."""

        if self._db_path:
            with self._db_lock, closing(self._connect()) as conn, conn:
                rows = conn.execute(
                    "SELECT payload FROM intent_outbox WHERE state=? ORDER BY created_at",
                    (OutboxState.UNKNOWN.value,),
                ).fetchall()
            intents = [self._deserialize_intent(str(row["payload"])) for row in rows]
        else:
            intents = [
                intent
                for intent in self._memory_outbox
                if self._states.get(intent.idempotency_key or self._hash(intent)) is OutboxState.UNKNOWN
            ]
        return [
            {
                "intent_id": str(intent.intent_id),
                "symbol": str(intent.instrument_id),
                "client_order_id": str(intent.client_order_id or ""),
            }
            for intent in intents
        ]

    def resolve_unknown(self, intent_id: str, *, exchange_order_found: bool) -> None:
        """只有查询得出结论后才允许 ACK 或重新进入 PENDING。"""

        state = OutboxState.ACKED.value if exchange_order_found else OutboxState.PENDING.value
        if not self._db_path:
            intent = next((item for item in self._memory_outbox if item.intent_id == intent_id), None)
            if intent is None:
                return
            key = intent.idempotency_key or self._hash(intent)
            if exchange_order_found:
                self.ack(intent_id, idempotency_key=key)
            else:
                self._states[key] = OutboxState.PENDING
            return
        with self._db_lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE intent_outbox SET state=?, last_error=NULL, updated_at=? WHERE intent_id=? AND state=?",
                (state, datetime.now(timezone.utc).isoformat(), intent_id, OutboxState.UNKNOWN.value),
            )
