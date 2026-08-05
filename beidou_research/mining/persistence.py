"""BF-07: 持久化 FactorRegistry 与证据存储。

支持:
- PostgreSQL adapter (生产) — 参数化查询，Fail-Closed
- SQLite adapter (本地研究)
- JSON file adapter (最小依赖)
- append-only audit log
- S3/本地文件 artifact 存储
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


# ================================================================
# 存储接口
# ================================================================

class FactorStore(Protocol):
    """因子存储接口。"""

    def save_factor_version(self, factor_id: str, version: str, data: dict) -> bool: ...
    def get_factor_version(self, factor_id: str, version: str) -> dict | None: ...
    def list_versions(self, factor_id: str) -> list[str]: ...
    def save_gate_decision(self, factor_id: str, decision: dict) -> bool: ...
    def get_gate_history(self, factor_id: str) -> list[dict]: ...


class ArtifactStore(Protocol):
    """Artifact 存储接口（S3 / 本地文件）。"""

    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes | None: ...
    def exists(self, key: str) -> bool: ...


# ================================================================
# PostgreSQL Factor Store (生产)
# ================================================================

class PostgreSQLFactorStore:
    """基于 PostgreSQL 的因子存储。

    使用 psycopg 参数化查询。Fail-Closed: 数据库故障时所有操作返回 False。
    """

    def __init__(self, conn_string: str = "", auto_connect: bool = True) -> None:
        self.conn_string = conn_string or os.environ.get(
            "BEIDOU_DATABASE_URL",
            "postgresql://localhost:5432/beidou",
        )
        self._conn = None
        self._available = False
        if auto_connect:
            self._try_connect()

    def _try_connect(self) -> bool:
        """尝试连接数据库。连接失败时 Fail-Closed。"""
        try:
            import psycopg
            self._conn = psycopg.connect(self.conn_string)
            self._available = True
            self._init_schema()
            return True
        except Exception:
            self._available = False
            return False

    def _init_schema(self) -> None:
        """初始化核心表（使用最小 schema，完整版见 schema_pg.sql）。"""
        if not self._available or self._conn is None:
            return
        try:
            with self._conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS factor_versions (
                        factor_id TEXT NOT NULL,
                        version TEXT NOT NULL,
                        data_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        artifact_hash TEXT NOT NULL,
                        PRIMARY KEY (factor_id, version)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gate_decisions (
                        id SERIAL PRIMARY KEY,
                        factor_id TEXT NOT NULL,
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        evidence_bundle_hash TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT DEFAULT '',
                        operator_id TEXT DEFAULT 'system',
                        decided_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                self._conn.commit()
        except Exception:
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available

    def save_factor_version(self, factor_id: str, version: str, data: dict) -> bool:
        if not self._available:
            return False
        try:
            data_json = json.dumps(data, sort_keys=True, default=str)
            artifact_hash = hashlib.sha256(data_json.encode()).hexdigest()[:16]
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO factor_versions (factor_id, version, data_json, artifact_hash)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (factor_id, version) DO UPDATE
                       SET data_json = EXCLUDED.data_json,
                           artifact_hash = EXCLUDED.artifact_hash""",
                    (factor_id, version, data_json, artifact_hash),
                )
                self._conn.commit()
            return True
        except Exception:
            self._available = False
            return False

    def get_factor_version(self, factor_id: str, version: str) -> dict | None:
        if not self._available:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT data_json FROM factor_versions WHERE factor_id=%s AND version=%s",
                    (factor_id, version),
                )
                row = cur.fetchone()
            if row:
                return json.loads(row[0])
            return None
        except Exception:
            return None

    def list_versions(self, factor_id: str) -> list[str]:
        if not self._available:
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM factor_versions WHERE factor_id=%s ORDER BY version",
                    (factor_id,),
                )
                return [r[0] for r in cur.fetchall()]
        except Exception:
            return []

    def save_gate_decision(self, factor_id: str, decision: dict) -> bool:
        if not self._available:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gate_decisions
                       (factor_id, from_state, to_state, evidence_bundle_hash, decision, reason, operator_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        factor_id,
                        decision.get("from_state", ""),
                        decision.get("to_state", ""),
                        decision.get("evidence_bundle_hash", ""),
                        decision.get("decision", ""),
                        decision.get("reason", ""),
                        decision.get("operator", "system"),
                    ),
                )
                self._conn.commit()
            return True
        except Exception:
            self._available = False
            return False

    def get_gate_history(self, factor_id: str) -> list[dict]:
        if not self._available:
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT from_state, to_state, evidence_bundle_hash, decision, reason, operator_id, decided_at
                       FROM gate_decisions WHERE factor_id=%s ORDER BY decided_at""",
                    (factor_id,),
                )
                return [
                    {"from_state": r[0], "to_state": r[1], "evidence_bundle_hash": r[2],
                     "decision": r[3], "reason": r[4], "operator": r[5], "decided_at": str(r[6])}
                    for r in cur.fetchall()
                ]
        except Exception:
            return []

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass


# ================================================================
# SQLite Factor Store (本地研究)
# ================================================================

@dataclass
class SQLiteFactorStore:
    """基于 SQLite 的因子存储（本地研究用）。"""

    db_path: str = "beidou_factors.db"

    def __post_init__(self) -> None:
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factor_versions (
                    factor_id TEXT NOT NULL, version TEXT NOT NULL,
                    data_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    artifact_hash TEXT NOT NULL,
                    PRIMARY KEY (factor_id, version)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gate_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    factor_id TEXT NOT NULL, from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL, evidence_bundle_hash TEXT NOT NULL,
                    decision TEXT NOT NULL, reason TEXT, operator TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_gate_factor
                ON gate_decisions(factor_id, decided_at)
            """)
            conn.commit()

    def save_factor_version(self, factor_id: str, version: str, data: dict) -> bool:
        try:
            data_json = json.dumps(data, sort_keys=True, default=str)
            artifact_hash = hashlib.sha256(data_json.encode()).hexdigest()[:16]
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO factor_versions
                       (factor_id, version, data_json, created_at, artifact_hash)
                       VALUES (?, ?, ?, ?, ?)""",
                    (factor_id, version, data_json,
                     datetime.now(timezone.utc).isoformat(), artifact_hash),
                )
                conn.commit()
            return True
        except Exception:
            return False

    def get_factor_version(self, factor_id: str, version: str) -> dict | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT data_json FROM factor_versions WHERE factor_id=? AND version=?",
                    (factor_id, version),
                ).fetchone()
            if row:
                return json.loads(row[0])
            return None
        except Exception:
            return None

    def list_versions(self, factor_id: str) -> list[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT version FROM factor_versions WHERE factor_id=? ORDER BY version",
                    (factor_id,),
                ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def save_gate_decision(self, factor_id: str, decision: dict) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO gate_decisions
                       (factor_id, from_state, to_state, evidence_bundle_hash,
                        decision, reason, operator, decided_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (factor_id, decision.get("from_state", ""), decision.get("to_state", ""),
                     decision.get("evidence_bundle_hash", ""), decision.get("decision", ""),
                     decision.get("reason", ""), decision.get("operator", "system"),
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            return True
        except Exception:
            return False

    def get_gate_history(self, factor_id: str) -> list[dict]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """SELECT from_state, to_state, evidence_bundle_hash,
                              decision, reason, operator, decided_at
                       FROM gate_decisions WHERE factor_id=? ORDER BY decided_at""",
                    (factor_id,),
                ).fetchall()
            return [
                {"from_state": r[0], "to_state": r[1], "evidence_bundle_hash": r[2],
                 "decision": r[3], "reason": r[4], "operator": r[5], "decided_at": r[6]}
                for r in rows
            ]
        except Exception:
            return []


# ================================================================
# JSON File Factor Store (最小依赖)
# ================================================================

class JSONFileFactorStore:
    """基于 JSON 文件的因子存储。"""

    def __init__(self, base_dir: str = "evidence/factors") -> None:
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def save_factor_version(self, factor_id: str, version: str, data: dict) -> bool:
        factor_dir = os.path.join(self.base_dir, factor_id)
        os.makedirs(factor_dir, exist_ok=True)
        path = os.path.join(factor_dir, f"{version}.json")
        try:
            with open(path, "w") as f:
                json.dump({
                    "factor_id": factor_id, "version": version, "data": data,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2, default=str)
            return True
        except Exception:
            return False

    def get_factor_version(self, factor_id: str, version: str) -> dict | None:
        path = os.path.join(self.base_dir, factor_id, f"{version}.json")
        try:
            with open(path) as f:
                return json.load(f).get("data")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def list_versions(self, factor_id: str) -> list[str]:
        factor_dir = os.path.join(self.base_dir, factor_id)
        if not os.path.isdir(factor_dir):
            return []
        return sorted([f[:-5] for f in os.listdir(factor_dir) if f.endswith(".json")])

    def save_gate_decision(self, factor_id: str, decision: dict) -> bool:
        audit_dir = os.path.join(self.base_dir, factor_id, "audit")
        os.makedirs(audit_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = os.path.join(audit_dir, f"gate-{ts}.json")
        try:
            with open(path, "w") as f:
                json.dump(decision, f, indent=2, default=str)
            return True
        except Exception:
            return False

    def get_gate_history(self, factor_id: str) -> list[dict]:
        audit_dir = os.path.join(self.base_dir, factor_id, "audit")
        if not os.path.isdir(audit_dir):
            return []
        history = []
        for fname in sorted(os.listdir(audit_dir)):
            if fname.startswith("gate-"):
                try:
                    with open(os.path.join(audit_dir, fname)) as f:
                        history.append(json.load(f))
                except json.JSONDecodeError:
                    pass
        return history


# ================================================================
# 本地文件 Artifact Store
# ================================================================

class LocalArtifactStore:
    """本地文件 artifact 存储。"""

    def __init__(self, base_dir: str = "evidence/artifacts") -> None:
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def put(self, key: str, data: bytes) -> str:
        path = os.path.join(self.base_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()

    def get(self, key: str) -> bytes | None:
        path = os.path.join(self.base_dir, key)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return os.path.isfile(os.path.join(self.base_dir, key))
