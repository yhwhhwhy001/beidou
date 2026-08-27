"""监控持久化 — 幂等schema, atomic writes, health probe (MON01-05/06/09)。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone

from beidou_observability.monitoring.contracts import (
    ComponentHealth,
    FrequencyLevel,
    FrequencyState,
    RolloutMode,
    RolloutState,
)


class MonitoringRepository:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, db_path="beidou_state.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    @classmethod
    def get_instance(cls, db_path="beidou_state.db"):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance

    def _get_conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def close(self) -> None:
        """Close this thread's monitoring connection during service shutdown."""

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with suppress(Exception):
                conn.close()
            self._local.conn = None

    def __enter__(self) -> "MonitoringRepository":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    def _init_schema(self):
        self._get_conn().executescript("""
            CREATE TABLE IF NOT EXISTS monitor_frequency_state (id INTEGER PRIMARY KEY CHECK(id=1), level TEXT DEFAULT 'FAST', interval_seconds INTEGER DEFAULT 600, promotion_clean_streak INTEGER DEFAULT 0, stable_since REAL DEFAULT 0, last_check_at REAL DEFAULT 0, next_due_at REAL DEFAULT 0, last_level_change_at REAL DEFAULT 0, last_reason TEXT DEFAULT '', policy_version TEXT DEFAULT '1.1');
            CREATE TABLE IF NOT EXISTS monitor_check_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, check_id TEXT NOT NULL, entity_type TEXT DEFAULT '', entity_id TEXT DEFAULT '', status TEXT NOT NULL, severity TEXT NOT NULL, message TEXT DEFAULT '', expected TEXT DEFAULT '', actual TEXT DEFAULT '', source TEXT DEFAULT '', source_timestamp REAL, observed_at REAL NOT NULL, fact_age_ms REAL, trace_id TEXT DEFAULT '', correlation_id TEXT DEFAULT '', remediation TEXT DEFAULT '', remediation_result TEXT DEFAULT '', evidence_hash TEXT NOT NULL, rollout_mode TEXT DEFAULT 'enforce', policy_version TEXT DEFAULT '1.1', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_rollout_state (check_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, mode TEXT DEFAULT 'enforce', locked_safety_check INTEGER DEFAULT 0, introduced_version TEXT DEFAULT '1.1', policy_version TEXT DEFAULT '1.1');
            CREATE TABLE IF NOT EXISTS monitor_order_trace_events (id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, correlation_id TEXT NOT NULL, strategy_id TEXT DEFAULT '', intent_id TEXT DEFAULT '', client_order_id TEXT DEFAULT '', stage TEXT NOT NULL, source_timestamp REAL NOT NULL, observed_at REAL NOT NULL, symbol TEXT DEFAULT '', side TEXT DEFAULT '', quantity TEXT DEFAULT '', price TEXT DEFAULT '', order_id TEXT DEFAULT '', evidence TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS monitor_module_progress (module_id TEXT PRIMARY KEY, criticality TEXT DEFAULT 'P2', heartbeat_indicator TEXT DEFAULT '', progress_indicator TEXT DEFAULT '', progress_timeout_seconds REAL DEFAULT 300, expected_min_progress_delta REAL DEFAULT 1, functional_probe TEXT DEFAULT '', dependencies TEXT DEFAULT '[]', lifecycle_state TEXT DEFAULT 'ACTIVE', last_progress_at REAL DEFAULT 0, last_probe_at REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS monitor_retention_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL, tier TEXT NOT NULL, deleted_count INTEGER DEFAULT 0, dry_run INTEGER DEFAULT 0, tombstone_summary TEXT DEFAULT '', error TEXT DEFAULT '');
        """)
        self._get_conn().execute(
            "INSERT OR IGNORE INTO monitor_frequency_state (id,level,interval_seconds) VALUES (1,'FAST',600)"
        )
        self._get_conn().execute("UPDATE monitor_frequency_state SET level='FAST' WHERE level='ALERT'")
        self._get_conn().commit()

    def health_probe(self):
        try:
            self._get_conn().execute("SELECT 1 FROM monitor_frequency_state WHERE id=1").fetchone()
            probe_ok = True
            return ComponentHealth(component="repository", healthy=probe_ok, last_heartbeat=time.monotonic())
        except Exception as e:
            return ComponentHealth(
                component="repository",
                healthy=False,
                last_error=str(e),
                consecutive_failures=1,
                safe_action="NO_NEW_RISK",
                recovery_gate="tx_probe",
            )

    def write_check_evidence(self, r):
        eh = r.compute_evidence_hash()
        self._get_conn().execute(
            "INSERT INTO monitor_check_evidence VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.check_id,
                r.entity_type,
                r.entity_id,
                r.status.value,
                r.severity.value,
                r.message,
                str(r.expected),
                str(r.actual),
                r.source,
                r.source_timestamp,
                r.observed_at,
                r.fact_age_ms,
                r.trace_id,
                r.correlation_id,
                r.remediation,
                r.remediation_result,
                eh,
                r.rollout_mode,
                r.policy_version,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._get_conn().commit()

    def get_frequency_state(self):
        row = self._get_conn().execute("SELECT * FROM monitor_frequency_state WHERE id=1").fetchone()
        if row is None:
            return FrequencyState()
        return FrequencyState(
            level=FrequencyLevel(row["level"]),
            interval_seconds=int(row["interval_seconds"]),
            promotion_clean_streak=int(row["promotion_clean_streak"]),
            stable_since=float(row["stable_since"] or 0),
            last_check_at=float(row["last_check_at"] or 0),
            next_due_at=float(row["next_due_at"] or 0),
            last_level_change_at=float(row["last_level_change_at"] or 0),
            last_reason=str(row["last_reason"] or ""),
            policy_version=str(row["policy_version"] or "1.1"),
        )

    def save_frequency_state(self, s):
        self._get_conn().execute(
            "UPDATE monitor_frequency_state SET level=?,interval_seconds=?,promotion_clean_streak=?,stable_since=?,last_check_at=?,next_due_at=?,last_level_change_at=?,last_reason=?,policy_version=? WHERE id=1",
            (
                s.level.value,
                s.interval_seconds,
                s.promotion_clean_streak,
                s.stable_since,
                s.last_check_at,
                s.next_due_at,
                s.last_level_change_at,
                s.last_reason,
                s.policy_version,
            ),
        )
        self._get_conn().commit()

    def upsert_rollout_state(self, s):
        self._get_conn().execute(
            "INSERT OR REPLACE INTO monitor_rollout_state VALUES (?,?,?,?,?,?)",
            (
                s.check_id,
                int(s.enabled),
                s.mode.value,
                int(s.locked_safety_check),
                s.introduced_version,
                s.policy_version,
            ),
        )
        self._get_conn().commit()

    def get_rollout_state(self, check_id):
        row = self._get_conn().execute("SELECT * FROM monitor_rollout_state WHERE check_id=?", (check_id,)).fetchone()
        if row is None:
            return None
        return RolloutState(
            check_id=row["check_id"],
            enabled=bool(row["enabled"]),
            mode=RolloutMode(row["mode"]),
            locked_safety_check=bool(row["locked_safety_check"]),
            introduced_version=row["introduced_version"] or "1.1",
            policy_version=row["policy_version"] or "1.1",
        )

    def can_disable_check(self, check_id, profile):
        s = self.get_rollout_state(check_id)
        if s is None:
            return True, ""
        if s.locked_safety_check and profile in ("testnet", "production"):
            return False, f"locked P0 '{check_id}' in {profile}"
        return True, ""

    def get_recent_evidence(self, check_id=None, limit=100):
        conn = self._get_conn()
        if check_id:
            rows = conn.execute(
                "SELECT * FROM monitor_check_evidence WHERE check_id=? ORDER BY created_at DESC LIMIT ?",
                (check_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM monitor_check_evidence ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
