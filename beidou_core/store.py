"""SQLite 持久化层 — 账本、订单状态、保护单、检查点、报告。

提供断线重启后的状态恢复能力。WAL 模式支持并发读写。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

# 订单终态集合 —— save_order_state 单调守卫共用(BD-FIX: 竞态回写防护)
_TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


class PersistentStore:
    """SQLite 持久化存储。线程安全，WAL 模式。"""

    _instance: PersistentStore | None = None
    _lock = threading.Lock()

    def __init__(self, db_path: str = "beidou_state.db") -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    @classmethod
    def get_instance(cls, db_path: str = "beidou_state.db") -> PersistentStore:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        elif cls._instance._db_path != db_path:
            # A singleton silently serving another database would split the
            # execution truth across files.  Refuse the ambiguity instead of
            # allowing a second engine to start on stale state.
            raise RuntimeError(f"PersistentStore already bound to {cls._instance._db_path!r}; requested {db_path!r}")
        return cls._instance

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            # FULL is required for a trading truth journal: a successful
            # commit must survive a process/host crash before an ACK can be
            # interpreted by the control plane.
            self._local.conn.execute("PRAGMA synchronous=FULL")
            self._local.conn.execute("PRAGMA busy_timeout=10000")  # BD-FIX: 10s 忙等
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT UNIQUE NOT NULL,
                account_id TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                instrument_id TEXT,
                debit_amount TEXT NOT NULL,
                credit_amount TEXT NOT NULL,
                description TEXT,
                correlation_id TEXT,
                timestamp TEXT NOT NULL,
                is_reversible INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS ledger_transactions (
                transaction_id TEXT PRIMARY KEY,
                transaction_type TEXT NOT NULL,
                source_event_id TEXT,
                correlation_id TEXT,
                timestamp TEXT NOT NULL,
                is_correction INTEGER DEFAULT 0,
                reverses_transaction_id TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS ledger_postings (
                posting_id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                account_type TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                instrument_id TEXT,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                decimals INTEGER NOT NULL DEFAULT 8,
                side TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS order_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price TEXT,
                status TEXT NOT NULL,
                filled_qty TEXT DEFAULT '0',
                avg_price TEXT,
                client_order_id TEXT,
                reduce_only TEXT,   -- M16-F02: 参数级对账防线
                stop_price TEXT,    -- M16-F02: STOP 单有效价位
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS protection_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protection_id TEXT UNIQUE NOT NULL,
                position_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trigger_price TEXT NOT NULL,
                order_price TEXT,
                quantity TEXT NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                stop_type TEXT,
                take_profit_type TEXT,
                created_at TEXT NOT NULL,
                triggered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkpoint_id TEXT UNIQUE NOT NULL,
                module_name TEXT NOT NULL,
                state_snapshot TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                invariants_valid INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trading_pool_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OBSERVING',
                score REAL DEFAULT 0.0,
                score_detail TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trading_pool_instrument
            ON trading_pool_events(instrument_id);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE NOT NULL,
                report_type TEXT NOT NULL,
                title TEXT NOT NULL,
                checksum TEXT,
                status TEXT,
                generated_at TEXT NOT NULL,
                export_data TEXT
            );
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                bid REAL,
                ask REAL,
                spread_bps REAL,
                volume_24h REAL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL,
                side TEXT NOT NULL,
                account_id TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                balance_amount TEXT NOT NULL,
                balance_currency TEXT NOT NULL,
                balance_decimals INTEGER NOT NULL DEFAULT 8,
                positions TEXT NOT NULL,
                open_orders TEXT NOT NULL,
                open_orders_detail TEXT,  -- M16-F01: 参数级明细(M13-R2 PG 对称)
                margin_amount TEXT,
                margin_currency TEXT,
                margin_decimals INTEGER,
                timestamp TEXT NOT NULL,
                correlation_id TEXT,
                source TEXT NOT NULL,
                fact_version TEXT NOT NULL DEFAULT '',
                complete INTEGER NOT NULL DEFAULT 0,
                UNIQUE(snapshot_id, side)
            );
            CREATE TABLE IF NOT EXISTS reconciliation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id TEXT UNIQUE NOT NULL,
                account_id TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                status TEXT NOT NULL,
                matched INTEGER NOT NULL,
                differences TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                system_snapshot_id TEXT,
                exchange_snapshot_id TEXT,
                event_snapshot_id TEXT
            );
            CREATE TABLE IF NOT EXISTS fill_events (
                fill_event_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                cumulative_qty TEXT NOT NULL,
                delta_qty TEXT NOT NULL,
                price TEXT NOT NULL,
                status TEXT NOT NULL,
                event_time TEXT NOT NULL,
                processing_state TEXT NOT NULL DEFAULT 'PENDING',
                committed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_stream_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                sequence INTEGER,
                event_time_ms INTEGER NOT NULL,
                transaction_time_ms INTEGER,
                order_id TEXT,
                client_order_id TEXT,
                symbol TEXT,
                side TEXT,
                order_type TEXT,
                order_status TEXT,
                execution_type TEXT,
                original_quantity TEXT,
                cumulative_quantity TEXT,
                last_quantity TEXT,
                last_price TEXT,
                average_price TEXT,
                trade_id TEXT,
                commission_amount TEXT,
                commission_currency TEXT,
                realized_pnl_amount TEXT,
                raw_event TEXT NOT NULL,
                continuity_status TEXT NOT NULL,
                applied_state TEXT NOT NULL DEFAULT 'PENDING',
                received_at TEXT NOT NULL,
                applied_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_stream_projections (
                account_id TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                balance_amount TEXT NOT NULL,
                balance_currency TEXT NOT NULL,
                balance_decimals INTEGER NOT NULL DEFAULT 8,
                positions TEXT NOT NULL,
                open_orders TEXT NOT NULL,
                last_event_time_ms INTEGER,
                last_sequence INTEGER,
                source TEXT NOT NULL,
                fact_version TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account_id, venue_id)
            );
            CREATE TABLE IF NOT EXISTS position_projection (
                symbol TEXT PRIMARY KEY,
                signed_quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                position_generation INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                source_event_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_opening_projections (
                projection_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                venue_id TEXT NOT NULL,
                balance_amount TEXT NOT NULL,
                balance_currency TEXT NOT NULL,
                balance_decimals INTEGER NOT NULL DEFAULT 8,
                positions TEXT NOT NULL,
                open_orders TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL,
                fact_version TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, venue_id)
            );
            CREATE TABLE IF NOT EXISTS v3_runtime_records (
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (record_type, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id, venue_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_correlation ON ledger_entries(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_postings_tx ON ledger_postings(transaction_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_source_event
                ON ledger_transactions(source_event_id)
                WHERE source_event_id IS NOT NULL AND source_event_id <> '';
            CREATE INDEX IF NOT EXISTS idx_orders_status ON order_states(status);
            CREATE INDEX IF NOT EXISTS idx_protection_position ON protection_orders(position_id);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_module ON checkpoints(module_name);
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol ON market_snapshots(symbol, timestamp);
            CREATE INDEX IF NOT EXISTS idx_recon_snapshots_key ON reconciliation_snapshots(account_id, venue_id, side, timestamp);
            CREATE INDEX IF NOT EXISTS idx_recon_results_checked ON reconciliation_results(account_id, venue_id, checked_at);
            CREATE INDEX IF NOT EXISTS idx_fill_events_order ON fill_events(order_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_user_stream_events_time ON user_stream_events(event_time_ms, event_id);
            CREATE INDEX IF NOT EXISTS idx_user_stream_events_order ON user_stream_events(order_id, event_time_ms);
            CREATE INDEX IF NOT EXISTS idx_opening_projection_key ON account_opening_projections(account_id, venue_id, captured_at);
            CREATE INDEX IF NOT EXISTS idx_v3_runtime_records_type_updated
                ON v3_runtime_records(record_type, updated_at, record_id);
        """)
        conn.commit()

        # Existing local databases predate the explicit reconciliation source
        # columns.  Migrations are additive and fail closed if a future schema
        # cannot be upgraded rather than silently dropping fact provenance.
        self._ensure_column(conn, "protection_orders", "owner_id", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        self._ensure_column(conn, "protection_orders", "position_generation", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "protection_orders", "session_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "protection_orders", "exchange_order_id", "TEXT")
        # Rows written by the previous fill implementation were already
        # journaled and projected; only newly observed fills start PENDING.
        self._ensure_column(conn, "fill_events", "processing_state", "TEXT NOT NULL DEFAULT 'COMMITTED'")
        self._ensure_column(conn, "fill_events", "committed_at", "TEXT")
        self._ensure_column(conn, "reconciliation_results", "event_snapshot_id", "TEXT")
        # M16-F01: 镜像漂移 —— PG 版(M13-R2)已序列化 open_orders_detail,
        # SQLite 版同步(否则双 store 快照审计证据不对称)
        self._ensure_column(conn, "reconciliation_snapshots", "open_orders_detail", "TEXT")
        # M16-F02: order_states 扩列 —— 参数级对账的 reduce_only/stopPrice
        # 防线(M13-R2 因无列移除比较,此处兑现登记项)
        self._ensure_column(conn, "order_states", "reduce_only", "TEXT")
        self._ensure_column(conn, "order_states", "stop_price", "TEXT")
        conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO ledger_entries (entry_id, account_id, venue_id, instrument_id, debit_amount, credit_amount, description, correlation_id, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
            (entry_id, account_id, venue_id, instrument_id, debit, credit, description, correlation_id, timestamp),
        )
        conn.commit()

    def restore_ledger_entries(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def save_ledger_transaction(self, transaction: Any) -> None:
        """Persist the immutable transaction and every posting atomically."""

        conn = self._get_conn()
        transaction_id = str(transaction.transaction_id)
        try:
            existing = conn.execute(
                """SELECT transaction_id, source_event_id, transaction_type
                   FROM ledger_transactions
                   WHERE transaction_id=?
                      OR (source_event_id=? AND source_event_id <> '')
                   LIMIT 1""",
                (transaction_id, str(transaction.source_event_id or "")),
            ).fetchone()
            if existing is not None:
                existing_id = str(existing["transaction_id"])
                existing_source = str(existing["source_event_id"] or "")
                if existing_id != transaction_id:
                    raise RuntimeError(
                        "ledger source_event_id conflict: "
                        f"{transaction.source_event_id!r} already belongs to {existing_id}"
                    )
                if existing_source != str(transaction.source_event_id or ""):
                    raise RuntimeError(f"ledger transaction id {transaction_id} has a different source_event_id")
                existing_postings = conn.execute(
                    "SELECT COUNT(*) FROM ledger_postings WHERE transaction_id=?",
                    (transaction_id,),
                ).fetchone()[0]
                if int(existing_postings) >= len(transaction.postings):
                    # Exact transaction replay is idempotent.
                    return
                # A crash may have committed the header before all postings;
                # complete that same transaction, never replace it.
                for posting in transaction.postings:
                    amount = posting.amount
                    conn.execute(
                        """INSERT OR IGNORE INTO ledger_postings
                           (posting_id, transaction_id, account_id, account_type, venue_id,
                            instrument_id, amount, currency, decimals, side, description)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(posting.posting_id),
                            transaction_id,
                            str(posting.account_id),
                            str(getattr(posting.account_type, "value", posting.account_type)),
                            str(posting.venue_id),
                            str(posting.instrument_id) if posting.instrument_id is not None else None,
                            str(amount.amount),
                            str(amount.currency),
                            int(amount.decimals),
                            str(getattr(posting.side, "value", posting.side)),
                            str(posting.description or ""),
                        ),
                    )
                conn.commit()
                return
            conn.execute(
                """INSERT INTO ledger_transactions
                   (transaction_id, transaction_type, source_event_id, correlation_id,
                    timestamp, is_correction, reverses_transaction_id, metadata)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    transaction_id,
                    str(getattr(transaction.transaction_type, "value", transaction.transaction_type)),
                    str(transaction.source_event_id or ""),
                    str(transaction.correlation_id) if transaction.correlation_id is not None else None,
                    transaction.timestamp.isoformat(),
                    int(bool(transaction.is_correction)),
                    str(transaction.reverses_transaction_id or ""),
                    json.dumps(transaction.metadata or {}, sort_keys=True, default=str),
                ),
            )
            for posting in transaction.postings:
                amount = posting.amount
                conn.execute(
                    """INSERT OR IGNORE INTO ledger_postings
                       (posting_id, transaction_id, account_id, account_type, venue_id,
                        instrument_id, amount, currency, decimals, side, description)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(posting.posting_id),
                        transaction_id,
                        str(posting.account_id),
                        str(getattr(posting.account_type, "value", posting.account_type)),
                        str(posting.venue_id),
                        str(posting.instrument_id) if posting.instrument_id is not None else None,
                        str(amount.amount),
                        str(amount.currency),
                        int(amount.decimals),
                        str(getattr(posting.side, "value", posting.side)),
                        str(posting.description or ""),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def restore_ledger_transactions(self) -> list[dict[str, Any]]:
        """Return durable journal rows for reconstruction by the domain ledger."""

        conn = self._get_conn()
        transactions = [
            dict(row)
            for row in conn.execute("SELECT * FROM ledger_transactions ORDER BY timestamp, transaction_id").fetchall()
        ]
        postings = [
            dict(row)
            for row in conn.execute("SELECT * FROM ledger_postings ORDER BY transaction_id, posting_id").fetchall()
        ]
        by_transaction: dict[str, list[dict[str, Any]]] = {}
        for posting in postings:
            by_transaction.setdefault(str(posting["transaction_id"]), []).append(posting)
        for transaction in transactions:
            transaction["postings"] = by_transaction.get(str(transaction["transaction_id"]), [])
        return transactions

    # --- Reconciliation facts ---

    def save_reconciliation_snapshot(self, snapshot_id: str, side: str, facts: Any) -> None:
        """Persist an immutable copy of one independently captured fact source."""

        conn = self._get_conn()
        margin = facts.margin_used
        conn.execute(
            """INSERT OR IGNORE INTO reconciliation_snapshots
               (snapshot_id, side, account_id, venue_id, balance_amount,
                balance_currency, balance_decimals, positions, open_orders,
                open_orders_detail, margin_amount, margin_currency,
                margin_decimals, timestamp, correlation_id, source,
                fact_version, complete)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(snapshot_id),
                str(side),
                str(facts.account_id),
                str(facts.venue_id),
                str(facts.balance.amount),
                str(facts.balance.currency),
                int(facts.balance.decimals),
                json.dumps({str(k): str(v.amount) for k, v in facts.positions.items()}, sort_keys=True),
                json.dumps([str(order_id) for order_id in facts.open_orders], sort_keys=True),
                # M16-F01: 参数级明细随快照持久化(与 PG 版 M13-R2 对齐)
                # M16-R2: None 值落空串(字面量 "None" 会触发 INVALID_PRICE_FACT 假阳性)
                json.dumps(
                    {
                        str(order_id): {str(k): (str(v) if v is not None else "") for k, v in detail.items()}
                        for order_id, detail in getattr(facts, "open_orders_detail", {}).items()
                    },
                    sort_keys=True,
                ),
                str(margin.amount) if margin is not None else None,
                str(margin.currency) if margin is not None else None,
                int(margin.decimals) if margin is not None else None,
                facts.timestamp.isoformat(),
                str(facts.correlation_id) if facts.correlation_id is not None else None,
                str(getattr(facts, "source", "UNKNOWN")),
                str(getattr(facts, "fact_version", "")),
                int(bool(getattr(facts, "complete", False))),
            ),
        )
        conn.commit()

    def save_reconciliation_result(
        self,
        result_id: str,
        result: Any,
        *,
        system_snapshot_id: str = "",
        exchange_snapshot_id: str = "",
        event_snapshot_id: str = "",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO reconciliation_results
               (result_id, account_id, venue_id, status, matched, differences,
                checked_at, system_snapshot_id, exchange_snapshot_id, event_snapshot_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                str(result_id),
                str(result.system_facts.account_id if result.system_facts else "UNKNOWN"),
                str(result.system_facts.venue_id if result.system_facts else "UNKNOWN"),
                str(getattr(result.status, "value", result.status)),
                int(bool(result.matched)),
                json.dumps(list(result.differences), ensure_ascii=False, sort_keys=True),
                result.checked_at.isoformat(),
                str(system_snapshot_id or ""),
                str(exchange_snapshot_id or ""),
                str(event_snapshot_id or ""),
            ),
        )
        conn.commit()

    def restore_latest_reconciliation_snapshot(
        self, account_id: str, venue_id: str, side: str
    ) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM reconciliation_snapshots WHERE account_id=? AND venue_id=? AND side=? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (account_id, venue_id, side),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["positions"] = json.loads(str(data["positions"]))
        data["open_orders"] = json.loads(str(data["open_orders"]))
        # M16-F01: 解析参数级明细(与 PG 版镜像对称)
        _detail = data.get("open_orders_detail")
        data["open_orders_detail"] = json.loads(str(_detail)) if _detail else {}
        return data

    # --- User-stream event journal ---

    def save_user_stream_event(self, update: Any, *, continuity_status: str) -> bool:
        """Append one normalized user event, rejecting identity conflicts.

        Event identity is durable before projection.  Replaying the exact
        event is idempotent; reusing its id with different raw bytes is a
        truth-journal conflict and must freeze the caller.
        """

        event = update.event
        event_id = str(event.event_id)
        raw_event = json.dumps(event.raw_event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT raw_event, applied_state FROM user_stream_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            existing_raw = str(existing[0])
            if existing_raw != raw_event:
                raise RuntimeError(f"user-stream event identity conflict: {event_id}")
            return False
        commission = getattr(update, "commission", None)
        realized_pnl = getattr(update, "realized_pnl", None)
        conn.execute(
            """INSERT INTO user_stream_events
               (event_id, event_type, sequence, event_time_ms, transaction_time_ms,
                order_id, client_order_id, symbol, side, order_type, order_status,
                execution_type, original_quantity, cumulative_quantity, last_quantity,
                last_price, average_price, trade_id, commission_amount,
                commission_currency, realized_pnl_amount, raw_event, continuity_status,
                applied_state, received_at, applied_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                str(event.event_type),
                event.sequence,
                int(event.event_time_ms),
                event.transaction_time_ms,
                str(getattr(update, "order_id", "")) or None,
                str(getattr(update, "client_order_id", "")) or None,
                str(getattr(update, "symbol", "")) or None,
                str(getattr(getattr(update, "side", None), "value", getattr(update, "side", ""))) or None,
                str(getattr(getattr(update, "order_type", None), "value", getattr(update, "order_type", ""))) or None,
                str(getattr(getattr(update, "order_status", None), "value", getattr(update, "order_status", "")))
                or None,
                str(getattr(update, "execution_type", "")) or None,
                str(getattr(getattr(update, "original_quantity", None), "amount", "")) or None,
                str(getattr(getattr(update, "cumulative_quantity", None), "amount", "")) or None,
                str(getattr(getattr(update, "last_quantity", None), "amount", "")) or None,
                str(getattr(getattr(update, "last_price", None), "amount", "")) or None,
                str(getattr(getattr(update, "average_price", None), "amount", "")) or None,
                str(getattr(update, "trade_id", "")) or None,
                str(getattr(commission, "amount", "")) or None,
                str(getattr(commission, "currency", "")) or None,
                str(getattr(realized_pnl, "amount", "")) or None,
                raw_event,
                str(continuity_status),
                "PENDING",
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        conn.commit()
        return True

    def mark_user_stream_event_applied(self, event_id: str, applied_at: str | None = None) -> None:
        """Mark an event applied only after its projection is durable."""

        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE user_stream_events
               SET applied_state='APPLIED', applied_at=?
               WHERE event_id=? AND applied_state='PENDING'""",
            (applied_at or datetime.now(timezone.utc).isoformat(), str(event_id)),
        )
        conn.commit()
        if cursor.rowcount == 1:
            return
        row = conn.execute(
            "SELECT applied_state FROM user_stream_events WHERE event_id=?",
            (str(event_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"user-stream event {event_id} disappeared before apply")
        if str(row[0]) != "APPLIED":
            raise RuntimeError(f"user-stream event {event_id} has unexpected state {row[0]!r}")

    def get_user_stream_event(self, event_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM user_stream_events WHERE event_id=?",
            (str(event_id),),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["raw_event"] = json.loads(str(data["raw_event"]))
        return data

    def restore_user_stream_events(self, *, applied_only: bool = False) -> list[dict[str, Any]]:
        conn = self._get_conn()
        query = "SELECT * FROM user_stream_events"
        params: tuple[Any, ...] = ()
        if applied_only:
            query += " WHERE applied_state='APPLIED'"
        query += " ORDER BY event_time_ms, event_id"
        rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["raw_event"] = json.loads(str(data["raw_event"]))
            result.append(data)
        return result

    def save_user_stream_projection(self, snapshot: Any, *, last_sequence: int | None) -> None:
        """Persist the event-derived projection before marking its event applied."""

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO user_stream_projections
               (account_id, venue_id, balance_amount, balance_currency, balance_decimals,
                positions, open_orders, last_event_time_ms, last_sequence, source,
                fact_version, complete, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, venue_id) DO UPDATE SET
                balance_amount=excluded.balance_amount,
                balance_currency=excluded.balance_currency,
                balance_decimals=excluded.balance_decimals,
                positions=excluded.positions,
                open_orders=excluded.open_orders,
                last_event_time_ms=excluded.last_event_time_ms,
                last_sequence=excluded.last_sequence,
                source=excluded.source,
                fact_version=excluded.fact_version,
                complete=excluded.complete,
                updated_at=excluded.updated_at""",
            (
                str(snapshot.account_id),
                str(snapshot.venue_id),
                str(snapshot.balance.amount),
                str(snapshot.balance.currency),
                int(snapshot.balance.decimals),
                json.dumps({str(k): str(v.amount) for k, v in snapshot.positions.items()}, sort_keys=True),
                json.dumps([str(order_id) for order_id in snapshot.open_orders], sort_keys=True),
                int(snapshot.timestamp.timestamp() * 1000) if snapshot.timestamp else None,
                last_sequence,
                str(getattr(snapshot, "source", "USER_STREAM")),
                str(getattr(snapshot, "fact_version", "")),
                int(bool(getattr(snapshot, "complete", False))),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    def restore_user_stream_projection(self, account_id: str, venue_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM user_stream_projections WHERE account_id=? AND venue_id=?",
            (str(account_id), str(venue_id)),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["positions"] = json.loads(str(data["positions"]))
        data["open_orders"] = json.loads(str(data["open_orders"]))
        return data

    # --- Fill and position projections ---

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
        """Append one idempotent fill observation.

        The unique event key is checked before any ledger/position mutation.
        A duplicate poll therefore becomes a harmless no-op instead of a
        second cash/position posting.
        """

        conn = self._get_conn()
        expected = {
            "order_id": str(order_id),
            "symbol": str(symbol),
            "side": str(side),
            "cumulative_qty": str(cumulative_qty),
            "delta_qty": str(delta_qty),
            "price": str(price),
            "status": str(status),
        }
        existing = self.get_fill_event(fill_event_id)
        if existing is not None:
            if any(str(existing.get(field)) != value for field, value in expected.items()) or (
                event_time is not None and str(existing.get("event_time")) != str(event_time)
            ):
                raise RuntimeError(f"fill event identity conflict: {fill_event_id}")
            return False
        cursor = conn.execute(
            """INSERT OR IGNORE INTO fill_events
               (fill_event_id, order_id, symbol, side, cumulative_qty, delta_qty,
                price, status, event_time, processing_state, committed_at)
               VALUES (?,?,?,?,?,?,?,?,?,'PENDING',NULL)""",
            (
                str(fill_event_id),
                str(order_id),
                str(symbol),
                str(side),
                str(cumulative_qty),
                str(delta_qty),
                str(price),
                str(status),
                event_time or datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1

    def get_fill_event(self, fill_event_id: str) -> dict[str, Any] | None:
        """Return one fill fact and its commit state without mutating it."""

        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM fill_events WHERE fill_event_id=?",
            (str(fill_event_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_fill_event_committed(self, fill_event_id: str, committed_at: str | None = None) -> None:
        """Close a pending fill only after ledger and position facts are durable."""

        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE fill_events
               SET processing_state='COMMITTED', committed_at=?
               WHERE fill_event_id=? AND processing_state='PENDING'""",
            (committed_at or datetime.now(timezone.utc).isoformat(), str(fill_event_id)),
        )
        conn.commit()
        if cursor.rowcount != 1:
            row = self.get_fill_event(fill_event_id)
            if row is None:
                raise RuntimeError(f"fill event {fill_event_id} disappeared before commit")
            if str(row.get("processing_state")) != "COMMITTED":
                raise RuntimeError(f"fill event {fill_event_id} has unexpected state {row.get('processing_state')!r}")

    def restore_fill_events(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM fill_events ORDER BY event_time, fill_event_id").fetchall()
        return [dict(row) for row in rows]

    # --- Explicit account opening projection ---

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
        """Persist an operator-authorized opening fact; never auto-derived.

        The unique account/venue key prevents two competing baselines from
        existing in one local truth store.  Replacing it is an explicit
        recovery operation and therefore must carry a new projection id,
        evidence hash and approval id.
        """

        required = {
            "projection_id": projection_id,
            "account_id": account_id,
            "venue_id": venue_id,
            "source": source,
            "fact_version": fact_version,
            "evidence_hash": evidence_hash,
            "approval_id": approval_id,
        }
        if not complete or any(not str(value).strip() for value in required.values()):
            raise ValueError("complete opening projection requires durable provenance and approval")
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO account_opening_projections
                   (projection_id, account_id, venue_id, balance_amount,
                    balance_currency, balance_decimals, positions, open_orders,
                    captured_at, source, fact_version, evidence_hash,
                    approval_id, complete, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, venue_id) DO UPDATE SET
                    projection_id=excluded.projection_id,
                    balance_amount=excluded.balance_amount,
                    balance_currency=excluded.balance_currency,
                    balance_decimals=excluded.balance_decimals,
                    positions=excluded.positions,
                    open_orders=excluded.open_orders,
                    captured_at=excluded.captured_at,
                    source=excluded.source,
                    fact_version=excluded.fact_version,
                    evidence_hash=excluded.evidence_hash,
                    approval_id=excluded.approval_id,
                    complete=excluded.complete,
                    created_at=excluded.created_at""",
                (
                    str(projection_id),
                    str(account_id),
                    str(venue_id),
                    str(balance_amount),
                    str(balance_currency),
                    int(balance_decimals),
                    json.dumps({str(k): str(v) for k, v in positions.items()}, sort_keys=True),
                    json.dumps([str(order_id) for order_id in open_orders], sort_keys=True),
                    str(captured_at),
                    str(source),
                    str(fact_version),
                    str(evidence_hash),
                    str(approval_id),
                    int(complete),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def save_strategy_risk_state(self, strategy_id: str, state: dict) -> None:
        import json

        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO v3_runtime_records (record_type, record_id, payload, updated_at) VALUES (?,?,?,datetime('now'))",
            ("strategy_risk", str(strategy_id), json.dumps(state)),
        )
        conn.commit()

    def restore_strategy_risk_states(self) -> dict[str, dict]:
        import json

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT record_id, payload FROM v3_runtime_records WHERE record_type='strategy_risk'"
        ).fetchall()
        return {str(row[0]): (json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])) for row in rows}

    def restore_account_opening_projection(self, account_id: str, venue_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM account_opening_projections WHERE account_id=? AND venue_id=?",
            (str(account_id), str(venue_id)),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["positions"] = json.loads(str(data["positions"]))
        data["open_orders"] = json.loads(str(data["open_orders"]))
        return data

    def save_position_projection(
        self,
        symbol: str,
        signed_quantity: str,
        entry_price: str,
        position_generation: int,
        source_event_id: str,
        updated_at: str | None = None,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO position_projection
               (symbol, signed_quantity, entry_price, position_generation,
                updated_at, source_event_id)
               VALUES (?,?,?,?,?,?)""",
            (
                str(symbol),
                str(signed_quantity),
                str(entry_price),
                int(position_generation),
                updated_at or datetime.now(timezone.utc).isoformat(),
                str(source_event_id),
            ),
        )
        conn.commit()

    def restore_position_projection(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM position_projection ORDER BY symbol").fetchall()
        return [dict(row) for row in rows]

    def get_account_balance(self, account_id: str, venue_id: str) -> float:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT SUM(CAST(debit_amount AS REAL) - CAST(credit_amount AS REAL)) as balance FROM ledger_entries WHERE account_id=? AND venue_id=?",
            (account_id, venue_id),
        ).fetchone()
        return float(row["balance"] or 0)

    # --- Order States ---

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
        *,
        reduce_only: str | None = None,  # M16-F02: 参数级对账防线
        stop_price: str | None = None,  # M16-F02: STOP 单有效价位
    ) -> None:
        conn = self._get_conn()
        # BD-FIX: 终态/部分成交不得被迟到的下单响应回写。实测 14:07
        # XRP/DOGE/ATOM 三单 user stream 先落 FILLED、REST 下单响应后到
        # 携 status=NEW 覆盖终态 → system 侧假挂单 → recon 恒 BLOCKED。
        existing_row = conn.execute("SELECT status FROM order_states WHERE order_id=?", (order_id,)).fetchone()
        if existing_row is not None:
            prev_status = str(existing_row["status"] or "")
            if prev_status in _TERMINAL_ORDER_STATUSES and status not in _TERMINAL_ORDER_STATUSES:
                return
            if prev_status == "PARTIALLY_FILLED" and status == "NEW":
                return
        now = datetime.now(timezone.utc).isoformat()
        # M16-R2: reduce_only/stop_price 缺省时保留旧值 —— INSERT OR
        # REPLACE 全行替换会抹除先前落库的防线值(部分成交/UNKNOWN
        # 等更新路径不传 kwargs,抹值后防线再次死代码)
        conn.execute(
            "INSERT OR REPLACE INTO order_states (order_id, symbol, side, order_type, quantity, price, status, filled_qty, avg_price, client_order_id, reduce_only, stop_price, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE(?,(SELECT reduce_only FROM order_states WHERE order_id=?)),COALESCE(?,(SELECT stop_price FROM order_states WHERE order_id=?)),COALESCE((SELECT created_at FROM order_states WHERE order_id=?),?),?)",
            (
                order_id,
                symbol,
                side,
                order_type,
                quantity,
                price,
                status,
                filled_qty,
                avg_price,
                client_order_id,
                reduce_only,
                order_id,
                stop_price,
                order_id,
                order_id,
                now,
                now,
            ),
        )
        conn.commit()

    def restore_order_states(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM order_states ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_active_orders(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM order_states WHERE status IN ('NEW','PARTIALLY_FILLED','PENDING_CANCEL')"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Protection Orders ---

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
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO protection_orders (protection_id, position_id, symbol, side, trigger_price, order_price, quantity, order_type, status, stop_type, take_profit_type, owner_id, position_generation, session_id, exchange_order_id, created_at, triggered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM protection_orders WHERE protection_id=?),?),CASE WHEN ? IN ('TRIGGERED','EXECUTED') THEN ? ELSE (SELECT triggered_at FROM protection_orders WHERE protection_id=?) END)",
            (
                protection_id,
                position_id,
                symbol,
                side,
                trigger_price,
                order_price,
                quantity,
                order_type,
                status,
                stop_type,
                take_profit_type,
                str(owner_id or "UNKNOWN"),
                int(position_generation),
                str(session_id or ""),
                str(exchange_order_id) if exchange_order_id is not None else None,
                protection_id,
                now,
                status,
                now,
                protection_id,
            ),
        )
        conn.commit()

    def restore_protections(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM protection_orders WHERE status='ACTIVE'").fetchall()
        return [dict(r) for r in rows]

    def remove_protection(self, position_id: str) -> None:
        """BD-FIX: 从DB中标记保护单为非活跃。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE protection_orders SET status='CANCELLED' WHERE position_id=? AND status='ACTIVE'",
            (position_id,),
        )
        conn.commit()

    # --- Checkpoints ---

    def save_checkpoint(
        self, checkpoint_id: str, module_name: str, state: dict[str, Any], sequence: int, invariants_valid: bool = True
    ) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, module_name, state_snapshot, sequence_number, invariants_valid, created_at) VALUES (?,?,?,?,?,?)",
            (checkpoint_id, module_name, json.dumps(state), sequence, int(invariants_valid), now),
        )
        conn.commit()

    def restore_checkpoints(self, module_name: str) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE module_name=? ORDER BY sequence_number DESC LIMIT 10",
            (module_name,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["state_snapshot"] = json.loads(d["state_snapshot"])
            results.append(d)
        return results

    # --- Trading Pool State ---

    def save_trading_pool_event(self, event: dict[str, Any]) -> None:
        """Persist a trading pool lifecycle event (UPSERT by instrument_id)."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        instrument_id = str(event.get("instrument_id", ""))
        status = str(event.get("status", "OBSERVING"))
        score = float(event.get("score", 0.0))
        score_detail = json.dumps(event.get("score_detail", {}))
        conn.execute(
            """INSERT INTO trading_pool_events (instrument_id, status, score, score_detail, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(instrument_id) DO UPDATE SET
               status=excluded.status, score=excluded.score,
               score_detail=excluded.score_detail, updated_at=excluded.updated_at""",
            (instrument_id, status, score, score_detail, now),
        )
        conn.commit()

    def restore_trading_pool_state(self) -> list[dict[str, Any]]:
        """Restore persisted trading pool state for engine initialization."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT instrument_id, status, score, score_detail, updated_at FROM trading_pool_events"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Reports ---

    def save_report(
        self, report_id: str, report_type: str, title: str, checksum: str, status: str, export_data: str
    ) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO reports (report_id, report_type, title, checksum, status, generated_at, export_data) VALUES (?,?,?,?,?,?,?)",
            (report_id, report_type, title, checksum, status, now, export_data),
        )
        conn.commit()

    def get_reports(self, report_type: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if report_type:
            rows = conn.execute(
                "SELECT * FROM reports WHERE report_type=? ORDER BY generated_at DESC LIMIT ?",
                (report_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY generated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Market Snapshots ---

    def save_market_snapshot(
        self,
        symbol: str,
        price: float,
        bid: float | None,
        ask: float | None,
        spread_bps: float | None,
        volume_24h: float | None,
    ) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO market_snapshots (symbol, price, bid, ask, spread_bps, volume_24h, timestamp) VALUES (?,?,?,?,?,?,?)",
            (symbol, price, bid, ask, spread_bps, volume_24h, now),
        )
        conn.commit()

    def get_recent_snapshots(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM market_snapshots WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Maintenance ---

    def clean_stale_new_orders(
        self,
        max_age_hours: int = 1,
        *,
        venue_terminal_statuses: dict[str, str] | None = None,
    ) -> int:
        """Resolve stale NEW facts only from explicit venue terminal readback.

        Age is diagnostic evidence, never proof that an exchange order no
        longer exists.  The durable row is retained and transitioned only
        when the venue confirms a terminal state.
        """
        conn = self._get_conn()
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        terminal_statuses = venue_terminal_statuses or {}
        allowed_terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        changed = 0
        for row in self.get_active_orders():
            if str(row.get("status")) != "NEW":
                continue
            try:
                updated = datetime.fromisoformat(str(row.get("updated_at"))).timestamp()
            except ValueError:
                continue
            if updated >= cutoff:
                continue
            order_id = str(row.get("order_id"))
            terminal = str(terminal_statuses.get(order_id, "")).upper()
            if terminal not in allowed_terminal:
                continue
            conn.execute(
                "UPDATE order_states SET status=?, updated_at=? WHERE order_id=? AND status='NEW'",
                (terminal, datetime.now(timezone.utc).isoformat(), order_id),
            )
            changed += 1
        conn.commit()
        return changed

    def cleanup_old_data(self, retention_days: int = 90) -> int:
        conn = self._get_conn()
        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        cutoff_str = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        deleted = 0
        deleted += conn.execute("DELETE FROM market_snapshots WHERE timestamp < ?", (cutoff_str,)).rowcount
        deleted += conn.execute(
            "DELETE FROM reports WHERE generated_at < ? AND report_type='DAILY'", (cutoff_str,)
        ).rowcount
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def close(self) -> None:
        """Close the current thread's connection.

        ``PersistentStore`` uses thread-local connections, so shutdown code
        must call this method from every worker thread that touched the store.
        The destructor below is only a last-resort leak guard; it is not a
        substitute for an explicit service shutdown hook.
        """

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with suppress(Exception):
                conn.close()
            self._local.conn = None

    def __enter__(self) -> "PersistentStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup for short-lived test/diagnostic stores.  Runtime
        # ownership remains explicit via ``close``/context-manager shutdown.
        with suppress(Exception):
            self.close()
