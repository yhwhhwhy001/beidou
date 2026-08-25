"""SQLite-backed append-only ExperimentRun ledger.

SQLite is used only as the local offline adapter.  Every accepted event is
durable before replay, and all integrity errors are fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .contracts import EXPERIMENT_RUN_SCHEMA_VERSION, GENESIS_HASH, ExperimentRunIdentity, LedgerEvent, canonical_json


class LedgerError(RuntimeError):
    """Base error for an operation that was not accepted."""


class LedgerIntegrityError(LedgerError):
    """An event violates the ledger state machine or append-only invariant."""


class NotVerifiableError(LedgerIntegrityError):
    """History cannot be proven; callers must not repair or skip it."""


class DuplicateEventConflictError(LedgerError):
    """An idempotency key was reused for a different immutable fact."""


class StaleWriterError(LedgerError):
    """The writer has been fenced by a newer fencing token."""


# Short names are part of the small offline adapter API; the concrete class
# names retain the conventional Error suffix for static quality checks.
NotVerifiable = NotVerifiableError
DuplicateEventConflict = DuplicateEventConflictError


_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"CREATED"}),
    "CREATED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


class ExperimentRunLedger:
    """One run's durable, append-only event stream."""

    def __init__(
        self,
        path: str | Path,
        identity: ExperimentRunIdentity,
        writer_id: str,
        fencing_token: str,
        *,
        register: bool = True,
    ) -> None:
        if not writer_id or not fencing_token:
            raise ValueError("writer_id and fencing_token are required")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.writer_id = writer_id
        self.fencing_token = fencing_token
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._create_schema()
        self._ensure_identity(register=register)

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS experiment_runs (
                run_id TEXT PRIMARY KEY,
                identity_json TEXT NOT NULL,
                identity_digest TEXT NOT NULL,
                active_fencing_token TEXT NOT NULL,
                schema_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
                sequence INTEGER NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                writer_id TEXT NOT NULL,
                fencing_token TEXT NOT NULL,
                stage TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence),
                UNIQUE (run_id, idempotency_key)
            );
            """
        )

    def _ensure_identity(self, *, register: bool) -> None:
        row = self._conn.execute("SELECT * FROM experiment_runs WHERE run_id = ?", (self.identity.run_id,)).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO experiment_runs VALUES (?, ?, ?, ?, ?)",
                (
                    self.identity.run_id,
                    canonical_json(self.identity.as_dict()),
                    self.identity.digest,
                    self.fencing_token,
                    self.identity.schema_version,
                ),
            )
            return
        if str(row["identity_digest"]) != self.identity.digest or str(row["identity_json"]) != canonical_json(
            self.identity.as_dict()
        ):
            raise LedgerIntegrityError("IDENTITY_MISMATCH")
        if register:
            self._conn.execute(
                "UPDATE experiment_runs SET active_fencing_token = ? WHERE run_id = ?",
                (self.fencing_token, self.identity.run_id),
            )

    def _check_fence(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT active_fencing_token FROM experiment_runs WHERE run_id = ?", (self.identity.run_id,)
        ).fetchone()
        if row is None or str(row[0]) != self.fencing_token:
            raise StaleWriterError("STALE_WRITER")

    def _rows(self, conn: sqlite3.Connection | None = None) -> list[LedgerEvent]:
        source = conn or self._conn
        return [
            LedgerEvent.from_row(row)
            for row in source.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (self.identity.run_id,)
            )
        ]

    def _validate(self, events: list[LedgerEvent]) -> str | None:
        previous = GENESIS_HASH
        stage: str | None = None
        seen: set[str] = set()
        for expected_sequence, event in enumerate(events, start=1):
            if event.schema_version != EXPERIMENT_RUN_SCHEMA_VERSION or event.run_id != self.identity.run_id:
                raise NotVerifiable("UNKNOWN_OR_FOREIGN_EVENT_VERSION")
            if event.sequence != expected_sequence or event.previous_hash != previous:
                raise NotVerifiable("OUT_OF_ORDER_OR_FORKED_CHAIN")
            if event.idempotency_key in seen:
                raise NotVerifiable("DUPLICATE_IDEMPOTENCY_KEY")
            try:
                payload = json.loads(event.payload_json)
            except (TypeError, ValueError) as exc:
                raise NotVerifiable("INVALID_PAYLOAD_JSON") from exc
            if event.payload_digest != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
                raise NotVerifiable("PAYLOAD_DIGEST_MISMATCH")
            if event.stage not in _TRANSITIONS or event.stage not in _TRANSITIONS[stage]:
                raise NotVerifiable("INVALID_TRANSITION")
            expected_hash = hashlib.sha256(canonical_json(event.hash_input()).encode()).hexdigest()
            if expected_hash != event.event_hash:
                raise NotVerifiable("EVENT_HASH_MISMATCH")
            seen.add(event.idempotency_key)
            previous, stage = event.event_hash, event.stage
        return stage

    def append(
        self,
        stage: str,
        payload: Mapping[str, Any],
        *,
        timestamp: str,
        idempotency_key: str,
        writer_id: str | None = None,
        fencing_token: str | None = None,
        schema_version: str = EXPERIMENT_RUN_SCHEMA_VERSION,
    ) -> LedgerEvent:
        if schema_version != EXPERIMENT_RUN_SCHEMA_VERSION:
            raise LedgerIntegrityError("UNKNOWN_EVENT_VERSION")
        if not idempotency_key or not timestamp:
            raise ValueError("timestamp and idempotency_key are required")
        event_writer = writer_id or self.writer_id
        event_fence = fencing_token or self.fencing_token
        if event_writer != self.writer_id or event_fence != self.fencing_token:
            raise StaleWriterError("WRITER_IDENTITY_MISMATCH")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._check_fence(conn)
            # Verify the complete existing chain before any idempotency
            # shortcut. A retry must never make corrupt history appear safe.
            events = self._rows(conn)
            current_stage = self._validate(events)
            existing_row = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND idempotency_key = ?", (self.identity.run_id, idempotency_key)
            ).fetchone()
            if existing_row is not None:
                existing = LedgerEvent.from_row(existing_row)
                candidate = LedgerEvent.build(
                    sequence=existing.sequence,
                    previous_hash=existing.previous_hash,
                    run_id=self.identity.run_id,
                    writer_id=event_writer,
                    fencing_token=event_fence,
                    stage=stage,
                    timestamp=timestamp,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    schema_version=schema_version,
                )
                if candidate.fact == existing.fact:
                    conn.execute("COMMIT")
                    return existing
                raise DuplicateEventConflict("IDEMPOTENCY_CONFLICT")
            if stage not in _TRANSITIONS or stage not in _TRANSITIONS[current_stage]:
                raise LedgerIntegrityError("INVALID_TRANSITION")
            previous_hash = events[-1].event_hash if events else GENESIS_HASH
            event = LedgerEvent.build(
                sequence=len(events) + 1,
                previous_hash=previous_hash,
                run_id=self.identity.run_id,
                writer_id=event_writer,
                fencing_token=event_fence,
                stage=stage,
                timestamp=timestamp,
                payload=payload,
                idempotency_key=idempotency_key,
                schema_version=schema_version,
            )
            conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.identity.run_id,
                    event.sequence,
                    event.previous_hash,
                    event.event_hash,
                    event.writer_id,
                    event.fencing_token,
                    event.stage,
                    event.timestamp,
                    event.payload_json,
                    event.payload_digest,
                    event.idempotency_key,
                    event.schema_version,
                ),
            )
            conn.execute("COMMIT")
            return event
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def events(self) -> list[LedgerEvent]:
        return self._rows()

    def replay(self) -> dict[str, Any]:
        events = self._rows()
        stage = self._validate(events)
        return {
            "run_id": self.identity.run_id,
            "identity_digest": self.identity.digest,
            "stage": stage,
            "sequence": len(events),
            "event_hash": events[-1].event_hash if events else GENESIS_HASH,
            "event_hashes": [event.event_hash for event in events],
        }

    def export_events(self) -> bytes:
        return (
            "\n".join(canonical_json(event.as_dict()) for event in self.events()) + ("\n" if self.events() else "")
        ).encode("utf-8")

    def export_to(self, destination: str | Path) -> None:
        destination = Path(destination)
        if destination.exists():
            destination.unlink()
        target = sqlite3.connect(destination)
        try:
            self._conn.backup(target)
        finally:
            target.close()

    @property
    def schema_digest(self) -> str:
        import hashlib

        rows = self._conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return hashlib.sha256(canonical_json([str(row[0]) for row in rows]).encode()).hexdigest()

    def close(self) -> None:
        self._conn.close()
