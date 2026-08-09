"""SQLite 持久化层 — 账本、订单状态、保护单、检查点、报告。

提供断线重启后的状态恢复能力。WAL 模式支持并发读写。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


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
            raise RuntimeError(
                f"PersistentStore already bound to {cls._instance._db_path!r}; "
                f"requested {db_path!r}"
            )
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
            CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id, venue_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_correlation ON ledger_entries(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_postings_tx ON ledger_postings(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON order_states(status);
            CREATE INDEX IF NOT EXISTS idx_protection_position ON protection_orders(position_id);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_module ON checkpoints(module_name);
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol ON market_snapshots(symbol, timestamp);
        """)
        conn.commit()

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
            conn.execute(
                """INSERT OR IGNORE INTO ledger_transactions
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
        transactions = [dict(row) for row in conn.execute(
            "SELECT * FROM ledger_transactions ORDER BY timestamp, transaction_id"
        ).fetchall()]
        postings = [dict(row) for row in conn.execute(
            "SELECT * FROM ledger_postings ORDER BY transaction_id, posting_id"
        ).fetchall()]
        by_transaction: dict[str, list[dict[str, Any]]] = {}
        for posting in postings:
            by_transaction.setdefault(str(posting["transaction_id"]), []).append(posting)
        for transaction in transactions:
            transaction["postings"] = by_transaction.get(str(transaction["transaction_id"]), [])
        return transactions

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
    ) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO order_states (order_id, symbol, side, order_type, quantity, price, status, filled_qty, avg_price, client_order_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM order_states WHERE order_id=?),?),?)",
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
    ) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO protection_orders (protection_id, position_id, symbol, side, trigger_price, order_price, quantity, order_type, status, stop_type, take_profit_type, created_at, triggered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM protection_orders WHERE protection_id=?),?),CASE WHEN ? IN ('TRIGGERED','EXECUTED') THEN ? ELSE (SELECT triggered_at FROM protection_orders WHERE protection_id=?) END)",
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

    def clean_stale_new_orders(self, max_age_hours: int = 24) -> int:
        """清理前次 session 遗留的 NEW 状态订单。

        NEW 订单超过 max_age_hours 未推进到后续状态即为陈旧，
        应在启动时自动清理，避免状态残留污染新 session。
        返回清理数量。
        """
        conn = self._get_conn()
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        cutoff_str = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        deleted = conn.execute(
            "DELETE FROM order_states WHERE status = 'NEW' AND updated_at < ?",
            (cutoff_str,),
        ).rowcount
        conn.commit()
        return deleted

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
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
