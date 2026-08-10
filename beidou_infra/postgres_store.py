"""PostgreSQL runtime projection store.

This adapter deliberately mirrors the small ``PersistentStore`` surface used
by the engine while keeping PostgreSQL as the authoritative durability
boundary.  Domain-specific tables are migrated incrementally; the v3 runtime
record/event tables provide an explicit, append-audited compatibility layer so
the engine never silently falls back to a second SQLite truth source.

The adapter is not a production certificate.  The caller must run the
forward-only migrations first, and release gates still require real crash,
replay, PITR, venue reconciliation and dual-worker evidence.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ClassVar, Iterator


def _value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "amount", value))


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("POSTGRES_RUNTIME_PAYLOAD_NOT_OBJECT")
    return parsed


def _timestamp(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return str(value)


class PostgresPersistentStore:
    """Durable engine projection store backed by one PostgreSQL connection.

    ``connection_factory`` is intentionally injectable for DB-API contract
    tests.  Production construction requires a DSN and validates that
    ``004_runtime_records`` has already been applied; it never creates a
    missing schema implicitly.
    """

    _instances: ClassVar[dict[str, "PostgresPersistentStore"]] = {}
    _instances_lock = threading.Lock()
    _required_tables: ClassVar[tuple[str, ...]] = (
        "schema_migrations",
        "v3_runtime_records",
        "v3_runtime_events",
        "v3_order_intents",
        "v3_risk_approvals",
        "v3_transactional_outbox",
        "v3_outbox_events",
    )
    _required_migration_versions: ClassVar[tuple[str, ...]] = (
        "001_initial_schema.up",
        "002_v3_fact_chain.up",
        "003_execution_outbox.up",
        "004_runtime_records.up",
        "005_risk_approval_intent_hash.up",
    )

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
        validate_schema: bool = True,
    ) -> None:
        if not dsn and connection_factory is None:
            raise ValueError("PostgreSQL DSN or connection_factory is required")
        self._dsn = dsn
        self._connection_factory = connection_factory or self._default_factory(dsn)
        self._conn: Any | None = None
        self._lock = threading.RLock()
        if validate_schema:
            try:
                self._validate_schema()
            except Exception:
                self.close()
                raise

    @staticmethod
    def _default_factory(dsn: str) -> Callable[[], Any]:
        def connect() -> Any:
            import psycopg

            # Keep standalone reads out of an implicit long-lived transaction;
            # explicit ``_transaction`` blocks still provide atomic writes.
            return psycopg.connect(dsn, autocommit=True)

        return connect

    @classmethod
    def get_instance(cls, dsn: str) -> "PostgresPersistentStore":
        with cls._instances_lock:
            existing = cls._instances.get(dsn)
            if existing is not None:
                return existing
            instance = cls(dsn)
            cls._instances[dsn] = instance
            return instance

    def _get_conn(self) -> Any:
        if self._conn is None:
            self._conn = self._connection_factory()
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._lock:
            conn = self._get_conn()
            transaction = getattr(conn, "transaction", None)
            if callable(transaction):
                with transaction():
                    yield conn
                return
            try:
                yield conn
            except Exception:
                rollback = getattr(conn, "rollback", None)
                if callable(rollback):
                    rollback()
                raise
            else:
                commit = getattr(conn, "commit", None)
                if callable(commit):
                    commit()

    def _validate_schema(self) -> None:
        with self._transaction() as conn:
            missing: list[str] = []
            for table in self._required_tables:
                cursor = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                row = cursor.fetchone()
                if row is None or row[0] is None:
                    missing.append(table)
            if missing:
                raise RuntimeError("POSTGRES_SCHEMA_NOT_READY: " + ",".join(missing))
            migration_rows = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                (list(self._required_migration_versions),),
            ).fetchall()
            applied_versions = {str(row[0]) for row in (migration_rows or []) if row and row[0] is not None}
            missing_versions = [
                version for version in self._required_migration_versions if version not in applied_versions
            ]
            if missing_versions:
                raise RuntimeError("POSTGRES_SCHEMA_MIGRATIONS_NOT_READY: " + ",".join(missing_versions))

    @staticmethod
    def _row_payload(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return _decode(row.get("payload"))
        return _decode(row[0])

    def _get_record(self, record_type: str, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._get_conn().execute(
                "SELECT payload::text FROM v3_runtime_records WHERE record_type=%s AND record_id=%s",
                (str(record_type), str(record_id)),
            )
            row = cursor.fetchone()
        return self._row_payload(row) if row is not None else None

    def _records(self, record_type: str) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._get_conn().execute(
                "SELECT record_id,payload::text FROM v3_runtime_records "
                "WHERE record_type=%s ORDER BY updated_at,record_id",
                (str(record_type),),
            )
            rows = cursor.fetchall() or []
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                record_id = str(row.get("record_id", ""))
                payload = _decode(row.get("payload"))
            else:
                record_id = str(row[0])
                payload = _decode(row[1])
            payload.setdefault("record_id", record_id)
            result.append(payload)
        return result

    def _write_record(
        self,
        record_type: str,
        record_id: str,
        payload: dict[str, Any],
        *,
        immutable: bool = False,
        event_type: str = "UPSERT",
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self._transaction() as conn:
            if immutable:
                cursor = conn.execute(
                    "INSERT INTO v3_runtime_records(record_type,record_id,payload,created_at,updated_at) "
                    "VALUES (%s,%s,CAST(%s AS jsonb),%s,%s) ON CONFLICT(record_type,record_id) DO NOTHING",
                    (record_type, record_id, _json(payload), now, now),
                )
                inserted = int(getattr(cursor, "rowcount", 1) or 0) == 1
                if not inserted:
                    return False
            else:
                conn.execute(
                    "INSERT INTO v3_runtime_records(record_type,record_id,payload,created_at,updated_at) "
                    "VALUES (%s,%s,CAST(%s AS jsonb),%s,%s) "
                    "ON CONFLICT(record_type,record_id) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=excluded.updated_at",
                    (record_type, record_id, _json(payload), now, now),
                )
                inserted = True
            conn.execute(
                "INSERT INTO v3_runtime_events(event_id,record_type,record_id,event_type,payload,occurred_at) "
                "VALUES (%s,%s,%s,%s,CAST(%s AS jsonb),%s)",
                (f"evt-{uuid.uuid4().hex}", record_type, record_id, event_type, _json(payload), now),
            )
        return inserted

    def _delete_record(self, record_type: str, record_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM v3_runtime_records WHERE record_type=%s AND record_id=%s",
                (record_type, record_id),
            )
            conn.execute(
                "INSERT INTO v3_runtime_events(event_id,record_type,record_id,event_type,payload) "
                "VALUES (%s,%s,%s,'DELETE','{}'::jsonb)",
                (f"evt-{uuid.uuid4().hex}", record_type, record_id),
            )

    # --- Ledger ---

    def save_ledger_entry(
        self,
        entry_id: str,
        account_id: str,
        venue_id: str,
        instrument_id: str | None,
        debit: str,
        credit: str,
        description: str,
        correlation_id: str | None,
        timestamp: str,
    ) -> None:
        self._write_record(
            "ledger_entry",
            str(entry_id),
            {
                "entry_id": str(entry_id),
                "account_id": str(account_id),
                "venue_id": str(venue_id),
                "instrument_id": instrument_id,
                "debit_amount": str(debit),
                "credit_amount": str(credit),
                "description": description,
                "correlation_id": correlation_id,
                "timestamp": str(timestamp),
            },
            immutable=True,
            event_type="LEDGER_ENTRY",
        )

    def restore_ledger_entries(self) -> list[dict[str, Any]]:
        return self._records("ledger_entry")

    def save_ledger_transaction(self, transaction: Any) -> None:
        transaction_id = str(transaction.transaction_id)
        source_event_id = str(transaction.source_event_id or "")
        postings = [
            {
                "posting_id": str(posting.posting_id),
                "account_id": str(posting.account_id),
                "account_type": _enum(posting.account_type),
                "venue_id": str(posting.venue_id),
                "instrument_id": str(posting.instrument_id) if posting.instrument_id is not None else None,
                "amount": str(posting.amount.amount),
                "currency": str(posting.amount.currency),
                "decimals": int(posting.amount.decimals),
                "side": _enum(posting.side),
                "description": str(posting.description or ""),
            }
            for posting in transaction.postings
        ]
        existing = self._get_record("ledger_transaction", transaction_id)
        if existing is not None:
            if str(existing.get("source_event_id", "")) != source_event_id:
                raise RuntimeError(f"ledger transaction source conflict: {source_event_id!r}")
            existing_ids = {str(item.get("posting_id")) for item in existing.get("postings", [])}
            if all(str(item["posting_id"]) in existing_ids for item in postings):
                return
            merged = dict(existing)
            merged["postings"] = list(existing.get("postings", [])) + [
                item for item in postings if str(item["posting_id"]) not in existing_ids
            ]
            self._write_record("ledger_transaction", transaction_id, merged, event_type="LEDGER_POSTINGS_COMPLETED")
            return
        self._write_record(
            "ledger_transaction",
            transaction_id,
            {
                "transaction_id": transaction_id,
                "transaction_type": _enum(transaction.transaction_type),
                "source_event_id": source_event_id,
                "correlation_id": str(transaction.correlation_id) if transaction.correlation_id else None,
                "timestamp": _timestamp(transaction.timestamp),
                "is_correction": bool(transaction.is_correction),
                "reverses_transaction_id": str(transaction.reverses_transaction_id or ""),
                "metadata": dict(transaction.metadata or {}),
                "postings": postings,
            },
            event_type="LEDGER_TRANSACTION",
        )

    def restore_ledger_transactions(self) -> list[dict[str, Any]]:
        rows = self._records("ledger_transaction")
        return sorted(rows, key=lambda row: (str(row.get("timestamp", "")), str(row.get("transaction_id", ""))))

    # --- Reconciliation facts ---

    def save_reconciliation_snapshot(self, snapshot_id: str, side: str, facts: Any) -> None:
        margin = facts.margin_used
        payload = {
            "snapshot_id": str(snapshot_id),
            "side": str(side),
            "account_id": str(facts.account_id),
            "venue_id": str(facts.venue_id),
            "balance_amount": str(facts.balance.amount),
            "balance_currency": str(facts.balance.currency),
            "balance_decimals": int(facts.balance.decimals),
            "positions": {str(k): str(v.amount) for k, v in facts.positions.items()},
            "open_orders": [str(order_id) for order_id in facts.open_orders],
            "margin_amount": str(margin.amount) if margin is not None else None,
            "margin_currency": str(margin.currency) if margin is not None else None,
            "margin_decimals": int(margin.decimals) if margin is not None else None,
            "timestamp": _timestamp(facts.timestamp),
            "correlation_id": str(facts.correlation_id) if facts.correlation_id else None,
            "source": str(getattr(facts, "source", "UNKNOWN")),
            "fact_version": str(getattr(facts, "fact_version", "")),
            "complete": bool(getattr(facts, "complete", False)),
        }
        self._write_record("reconciliation_snapshot", f"{snapshot_id}:{side}", payload, immutable=True)

    def save_reconciliation_result(
        self,
        result_id: str,
        result: Any,
        *,
        system_snapshot_id: str = "",
        exchange_snapshot_id: str = "",
        event_snapshot_id: str = "",
    ) -> None:
        self._write_record(
            "reconciliation_result",
            str(result_id),
            {
                "result_id": str(result_id),
                "account_id": str(result.system_facts.account_id if result.system_facts else "UNKNOWN"),
                "venue_id": str(result.system_facts.venue_id if result.system_facts else "UNKNOWN"),
                "status": _enum(result.status),
                "matched": bool(result.matched),
                "differences": list(result.differences),
                "checked_at": _timestamp(result.checked_at),
                "system_snapshot_id": str(system_snapshot_id or ""),
                "exchange_snapshot_id": str(exchange_snapshot_id or ""),
                "event_snapshot_id": str(event_snapshot_id or ""),
            },
            event_type="RECONCILIATION_RESULT",
        )

    def restore_latest_reconciliation_snapshot(
        self, account_id: str, venue_id: str, side: str
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self._records("reconciliation_snapshot")
            if str(row.get("account_id")) == str(account_id)
            and str(row.get("venue_id")) == str(venue_id)
            and str(row.get("side")) == str(side)
        ]
        return max(rows, key=lambda row: str(row.get("timestamp", "")), default=None)

    # --- User stream ---

    def save_user_stream_event(self, update: Any, *, continuity_status: str) -> bool:
        event = update.event
        event_id = str(event.event_id)
        raw_event = event.raw_event
        existing = self._get_record("user_stream_event", event_id)
        if existing is not None:
            if _json(existing.get("raw_event", {})) != _json(raw_event):
                raise RuntimeError(f"user-stream event identity conflict: {event_id}")
            return False
        commission = getattr(update, "commission", None)
        realized_pnl = getattr(update, "realized_pnl", None)
        self._write_record(
            "user_stream_event",
            event_id,
            {
                "event_id": event_id,
                "event_type": str(event.event_type),
                "sequence": event.sequence,
                "event_time_ms": int(event.event_time_ms),
                "transaction_time_ms": event.transaction_time_ms,
                "order_id": str(getattr(update, "order_id", "")) or None,
                "client_order_id": str(getattr(update, "client_order_id", "")) or None,
                "symbol": str(getattr(update, "symbol", "")) or None,
                "side": _enum(getattr(update, "side", None)),
                "order_type": _enum(getattr(update, "order_type", None)),
                "order_status": _enum(getattr(update, "order_status", None)),
                "execution_type": str(getattr(update, "execution_type", "")) or None,
                "original_quantity": _value(getattr(update, "original_quantity", None)),
                "cumulative_quantity": _value(getattr(update, "cumulative_quantity", None)),
                "last_quantity": _value(getattr(update, "last_quantity", None)),
                "last_price": _value(getattr(update, "last_price", None)),
                "average_price": _value(getattr(update, "average_price", None)),
                "trade_id": str(getattr(update, "trade_id", "")) or None,
                "commission_amount": _value(commission),
                "commission_currency": str(getattr(commission, "currency", "")) or None,
                "realized_pnl_amount": _value(realized_pnl),
                "raw_event": raw_event,
                "continuity_status": str(continuity_status),
                "applied_state": "PENDING",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "applied_at": None,
            },
            event_type="USER_STREAM_EVENT",
        )
        return True

    def mark_user_stream_event_applied(self, event_id: str, applied_at: str | None = None) -> None:
        row = self._get_record("user_stream_event", str(event_id))
        if row is None:
            raise RuntimeError(f"user-stream event {event_id} disappeared before apply")
        state = str(row.get("applied_state", "PENDING"))
        if state == "APPLIED":
            return
        if state != "PENDING":
            raise RuntimeError(f"user-stream event {event_id} has unexpected state {state!r}")
        row["applied_state"] = "APPLIED"
        row["applied_at"] = applied_at or datetime.now(timezone.utc).isoformat()
        self._write_record("user_stream_event", str(event_id), row, event_type="USER_STREAM_APPLIED")

    def get_user_stream_event(self, event_id: str) -> dict[str, Any] | None:
        return self._get_record("user_stream_event", str(event_id))

    def restore_user_stream_events(self, *, applied_only: bool = False) -> list[dict[str, Any]]:
        rows = self._records("user_stream_event")
        if applied_only:
            rows = [row for row in rows if str(row.get("applied_state")) == "APPLIED"]
        return sorted(rows, key=lambda row: (int(row.get("event_time_ms") or 0), str(row.get("event_id", ""))))

    def save_user_stream_projection(self, snapshot: Any, *, last_sequence: int | None) -> None:
        self._write_record(
            "user_stream_projection",
            f"{snapshot.account_id}:{snapshot.venue_id}",
            {
                "account_id": str(snapshot.account_id),
                "venue_id": str(snapshot.venue_id),
                "balance_amount": str(snapshot.balance.amount),
                "balance_currency": str(snapshot.balance.currency),
                "balance_decimals": int(snapshot.balance.decimals),
                "positions": {str(k): str(v.amount) for k, v in snapshot.positions.items()},
                "open_orders": [str(order_id) for order_id in snapshot.open_orders],
                "last_event_time_ms": int(snapshot.timestamp.timestamp() * 1000) if snapshot.timestamp else None,
                "last_sequence": last_sequence,
                "source": str(getattr(snapshot, "source", "USER_STREAM")),
                "fact_version": str(getattr(snapshot, "fact_version", "")),
                "complete": bool(getattr(snapshot, "complete", False)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            event_type="USER_STREAM_PROJECTION",
        )

    def restore_user_stream_projection(self, account_id: str, venue_id: str) -> dict[str, Any] | None:
        return self._get_record("user_stream_projection", f"{account_id}:{venue_id}")

    # --- Fill, position and opening facts ---

    def save_fill_event(
        self,
        fill_event_id: str,
        order_id: str,
        symbol: str,
        side: str,
        cumulative_qty: str,
        delta_qty: str,
        price: str,
        status: str,
        event_time: str | None = None,
    ) -> bool:
        payload = {
            "fill_event_id": str(fill_event_id),
            "order_id": str(order_id),
            "symbol": str(symbol),
            "side": str(side),
            "cumulative_qty": str(cumulative_qty),
            "delta_qty": str(delta_qty),
            "price": str(price),
            "status": str(status),
            "event_time": event_time or datetime.now(timezone.utc).isoformat(),
            "processing_state": "PENDING",
            "committed_at": None,
        }
        existing = self._get_record("fill_event", str(fill_event_id))
        if existing is not None:
            if _json(existing) != _json(payload) and str(existing.get("processing_state")) != "COMMITTED":
                raise RuntimeError(f"fill event identity conflict: {fill_event_id}")
            return False
        return self._write_record("fill_event", str(fill_event_id), payload, immutable=True, event_type="FILL_EVENT")

    def get_fill_event(self, fill_event_id: str) -> dict[str, Any] | None:
        return self._get_record("fill_event", str(fill_event_id))

    def mark_fill_event_committed(self, fill_event_id: str, committed_at: str | None = None) -> None:
        row = self.get_fill_event(fill_event_id)
        if row is None:
            raise RuntimeError(f"fill event {fill_event_id} disappeared before commit")
        if str(row.get("processing_state", "PENDING")) == "COMMITTED":
            return
        if str(row.get("processing_state", "PENDING")) != "PENDING":
            raise RuntimeError(f"fill event {fill_event_id} has unexpected state {row.get('processing_state')!r}")
        row["processing_state"] = "COMMITTED"
        row["committed_at"] = committed_at or datetime.now(timezone.utc).isoformat()
        self._write_record("fill_event", str(fill_event_id), row, event_type="FILL_COMMITTED")

    def restore_fill_events(self) -> list[dict[str, Any]]:
        return sorted(
            self._records("fill_event"),
            key=lambda row: (str(row.get("event_time", "")), str(row.get("fill_event_id", ""))),
        )

    def save_account_opening_projection(
        self,
        projection_id: str,
        account_id: str,
        venue_id: str,
        balance_amount: str,
        balance_currency: str,
        balance_decimals: int,
        positions: dict[str, str],
        open_orders: list[str],
        captured_at: str,
        source: str,
        fact_version: str,
        evidence_hash: str,
        approval_id: str,
        complete: bool = True,
    ) -> None:
        required = (projection_id, account_id, venue_id, source, fact_version, evidence_hash, approval_id)
        if not complete or any(not str(value).strip() for value in required):
            raise ValueError("complete opening projection requires durable provenance and approval")
        self._write_record(
            "account_opening_projection",
            f"{account_id}:{venue_id}",
            {
                "projection_id": str(projection_id),
                "account_id": str(account_id),
                "venue_id": str(venue_id),
                "balance_amount": str(balance_amount),
                "balance_currency": str(balance_currency),
                "balance_decimals": int(balance_decimals),
                "positions": {str(k): str(v) for k, v in positions.items()},
                "open_orders": [str(order_id) for order_id in open_orders],
                "captured_at": str(captured_at),
                "source": str(source),
                "fact_version": str(fact_version),
                "evidence_hash": str(evidence_hash),
                "approval_id": str(approval_id),
                "complete": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            event_type="ACCOUNT_OPENING_PROJECTION",
        )

    def restore_account_opening_projection(self, account_id: str, venue_id: str) -> dict[str, Any] | None:
        return self._get_record("account_opening_projection", f"{account_id}:{venue_id}")

    def save_strategy_risk_state(self, strategy_id: str, state: dict) -> None:
        self._write_record("strategy_risk", str(strategy_id), state, event_type="RISK_STATE_UPDATE")

    def restore_strategy_risk_states(self) -> dict[str, dict]:
        records = self._records("strategy_risk")
        return {str(r.get("strategy_id", r.get("record_id", ""))): r for r in records}

    def save_position_projection(
        self,
        symbol: str,
        signed_quantity: str,
        entry_price: str,
        position_generation: int,
        source_event_id: str,
        updated_at: str | None = None,
    ) -> None:
        self._write_record(
            "position_projection",
            str(symbol),
            {
                "symbol": str(symbol),
                "signed_quantity": str(signed_quantity),
                "entry_price": str(entry_price),
                "position_generation": int(position_generation),
                "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
                "source_event_id": str(source_event_id),
            },
            event_type="POSITION_PROJECTION",
        )

    def restore_position_projection(self) -> list[dict[str, Any]]:
        return self._records("position_projection")

    def get_account_balance(self, account_id: str, venue_id: str) -> float:
        total = Decimal("0")
        for row in self.restore_ledger_entries():
            if str(row.get("account_id")) != str(account_id) or str(row.get("venue_id")) != str(venue_id):
                continue
            total += Decimal(str(row.get("debit_amount", "0"))) - Decimal(str(row.get("credit_amount", "0")))
        return float(total)

    # --- Orders and protections ---

    def save_order_state(
        self,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None,
        status: str,
        filled_qty: str = "0",
        avg_price: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        existing = self._get_record("order_state", str(order_id))
        self._write_record(
            "order_state",
            str(order_id),
            {
                "order_id": str(order_id),
                "symbol": str(symbol),
                "side": str(side),
                "order_type": str(order_type),
                "quantity": str(quantity),
                "price": price,
                "status": str(status),
                "filled_qty": str(filled_qty),
                "avg_price": avg_price,
                "client_order_id": client_order_id,
                "created_at": str(existing.get("created_at")) if existing else datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            event_type="ORDER_STATE",
        )

    def restore_order_states(self) -> list[dict[str, Any]]:
        return self._records("order_state")

    def get_active_orders(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self.restore_order_states()
            if str(row.get("status")) in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
        ]

    def save_protection(
        self,
        protection_id: str,
        position_id: str,
        symbol: str,
        side: str,
        trigger_price: str,
        order_price: str | None,
        quantity: str,
        order_type: str,
        status: str,
        stop_type: str | None = None,
        take_profit_type: str | None = None,
        owner_id: str = "UNKNOWN",
        position_generation: int = 0,
        session_id: str = "",
        exchange_order_id: str | None = None,
    ) -> None:
        existing = self._get_record("protection", str(protection_id))
        self._write_record(
            "protection",
            str(protection_id),
            {
                "protection_id": str(protection_id),
                "position_id": str(position_id),
                "symbol": str(symbol),
                "side": str(side),
                "trigger_price": str(trigger_price),
                "order_price": order_price,
                "quantity": str(quantity),
                "order_type": str(order_type),
                "status": str(status),
                "stop_type": stop_type,
                "take_profit_type": take_profit_type,
                "owner_id": str(owner_id or "UNKNOWN"),
                "position_generation": int(position_generation),
                "session_id": str(session_id or ""),
                "exchange_order_id": exchange_order_id,
                "created_at": str(existing.get("created_at")) if existing else datetime.now(timezone.utc).isoformat(),
                "triggered_at": datetime.now(timezone.utc).isoformat()
                if str(status) in {"TRIGGERED", "EXECUTED"}
                else (existing.get("triggered_at") if existing else None),
            },
            event_type="PROTECTION_STATE",
        )

    def restore_protections(self) -> list[dict[str, Any]]:
        return [row for row in self._records("protection") if str(row.get("status")) == "ACTIVE"]

    def remove_protection(self, position_id: str) -> None:
        for row in self._records("protection"):
            if str(row.get("position_id")) == str(position_id) and str(row.get("status")) == "ACTIVE":
                row["status"] = "CANCELLED"
                self._write_record("protection", str(row.get("protection_id")), row, event_type="PROTECTION_CANCELLED")

    # --- Checkpoints, pool, reports and market snapshots ---

    def save_checkpoint(
        self, checkpoint_id: str, module_name: str, state: dict[str, Any], sequence: int, invariants_valid: bool = True
    ) -> None:
        self._write_record(
            "checkpoint",
            str(checkpoint_id),
            {
                "checkpoint_id": str(checkpoint_id),
                "module_name": str(module_name),
                "state_snapshot": state,
                "sequence_number": int(sequence),
                "invariants_valid": bool(invariants_valid),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            event_type="CHECKPOINT",
        )

    def restore_checkpoints(self, module_name: str) -> list[dict[str, Any]]:
        rows = [row for row in self._records("checkpoint") if str(row.get("module_name")) == str(module_name)]
        return sorted(rows, key=lambda row: int(row.get("sequence_number") or 0), reverse=True)[:10]

    def save_trading_pool_event(self, event: dict[str, Any]) -> None:
        instrument_id = str(event.get("instrument_id", ""))
        payload = dict(event)
        payload.update(
            {
                "instrument_id": instrument_id,
                "status": str(event.get("status", "OBSERVING")),
                "score": float(event.get("score", 0.0)),
                "score_detail": event.get("score_detail", {}),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._write_record("trading_pool", instrument_id, payload, event_type="TRADING_POOL")

    def restore_trading_pool_state(self) -> list[dict[str, Any]]:
        return self._records("trading_pool")

    def save_report(
        self, report_id: str, report_type: str, title: str, checksum: str, status: str, export_data: str
    ) -> None:
        self._write_record(
            "report",
            str(report_id),
            {
                "report_id": str(report_id),
                "report_type": str(report_type),
                "title": str(title),
                "checksum": str(checksum),
                "status": str(status),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "export_data": export_data,
            },
            event_type="REPORT",
        )

    def get_reports(self, report_type: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._records("report")
        if report_type:
            rows = [row for row in rows if str(row.get("report_type")) == str(report_type)]
        return sorted(rows, key=lambda row: str(row.get("generated_at", "")), reverse=True)[:limit]

    def save_market_snapshot(
        self,
        symbol: str,
        price: float,
        bid: float | None,
        ask: float | None,
        spread_bps: float | None,
        volume_24h: float | None,
    ) -> None:
        record_id = f"{symbol}:{datetime.now(timezone.utc).isoformat()}:{uuid.uuid4().hex[:8]}"
        self._write_record(
            "market_snapshot",
            record_id,
            {
                "symbol": str(symbol),
                "price": float(price),
                "bid": bid,
                "ask": ask,
                "spread_bps": spread_bps,
                "volume_24h": volume_24h,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            event_type="MARKET_SNAPSHOT",
        )

    def get_recent_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = [row for row in self._records("market_snapshot") if str(row.get("symbol")) == str(symbol)]
        return sorted(rows, key=lambda row: str(row.get("timestamp", "")), reverse=True)[:limit]

    # --- Maintenance ---

    def clean_stale_new_orders(self, max_age_hours: int = 24) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        changed = 0
        for row in self.get_active_orders():
            if str(row.get("status")) != "NEW":
                continue
            try:
                updated = datetime.fromisoformat(str(row.get("updated_at"))).timestamp()
            except ValueError:
                continue
            if updated < cutoff:
                row["status"] = "UNKNOWN"
                self._write_record("order_state", str(row.get("order_id")), row, event_type="STALE_ORDER_UNKNOWN")
                changed += 1
        return changed

    def cleanup_old_data(self, retention_days: int = 90) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        deleted = 0
        for row in self._records("market_snapshot") + self._records("report"):
            timestamp = row.get("timestamp", row.get("generated_at"))
            try:
                if datetime.fromisoformat(str(timestamp)).timestamp() < cutoff:
                    record_id = str(row.get("record_id") or row.get("report_id") or "")
                    record_type = "market_snapshot" if "timestamp" in row else "report"
                    if record_id:
                        self._delete_record(record_type, record_id)
                        deleted += 1
            except (TypeError, ValueError):
                continue
        return deleted

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                close = getattr(self._conn, "close", None)
                if callable(close):
                    close()
                self._conn = None
