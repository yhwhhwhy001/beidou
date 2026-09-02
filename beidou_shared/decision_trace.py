"""Durable order-level DecisionTrace for Testnet verification.

The trace is an append-only, fsync-backed JSONL journal of immutable
snapshots.  Each update carries the complete current snapshot, so a restart
can rebuild state without relying on log text or an in-memory object.  Secrets
and venue HMAC material are rejected/redacted at the boundary.
"""

from __future__ import annotations

import copy
import fcntl
import gzip
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, cast


class TraceStatus(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


_STATUS_TRANSITIONS: dict[TraceStatus, frozenset[TraceStatus]] = {
    TraceStatus.PREPARED: frozenset({TraceStatus.SUBMITTED, TraceStatus.UNKNOWN, TraceStatus.FAILED}),
    TraceStatus.SUBMITTED: frozenset({TraceStatus.ACKED, TraceStatus.UNKNOWN, TraceStatus.FAILED}),
    TraceStatus.ACKED: frozenset({TraceStatus.FILLED, TraceStatus.CLOSED, TraceStatus.UNKNOWN, TraceStatus.FAILED}),
    TraceStatus.FILLED: frozenset({TraceStatus.CLOSED, TraceStatus.UNKNOWN, TraceStatus.FAILED}),
    TraceStatus.CLOSED: frozenset(),
    TraceStatus.UNKNOWN: frozenset(
        {
            TraceStatus.SUBMITTED,
            TraceStatus.ACKED,
            TraceStatus.FILLED,
            TraceStatus.CLOSED,
            TraceStatus.UNKNOWN,
            TraceStatus.FAILED,
        }
    ),
    TraceStatus.FAILED: frozenset(),
}

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "secret",
        "signature",
        "private_key",
        "privatekey",
        "authorization",
        "x-mbx-apikey",
    }
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower().replace("-", "_") in _SECRET_KEYS:
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _parse_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class DecisionTrace:
    """One order-level decision and execution truth record."""

    trace_id: str
    intent_id: str
    client_order_id: str
    environment: str = "TESTNET"
    status: TraceStatus = TraceStatus.PREPARED
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pool: dict[str, Any] = field(default_factory=dict)
    market_data_hash: str = ""
    factor_outputs: list[dict[str, Any]] = field(default_factory=list)
    strategy: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    sizing: dict[str, Any] = field(default_factory=dict)
    exchange_rules_hash: str = ""
    leverage_request: int | None = None
    leverage_readback: int | None = None
    order_request: dict[str, Any] = field(default_factory=dict)
    order_request_hash: str = ""
    exchange_order_id: str = ""
    order_ack: dict[str, Any] = field(default_factory=dict)
    order_status: str = ""
    executed_qty: str = "0"
    position_after: dict[str, Any] = field(default_factory=dict)
    fees: dict[str, Any] = field(default_factory=dict)
    funding: dict[str, Any] = field(default_factory=dict)
    slippage: dict[str, Any] = field(default_factory=dict)
    pnl: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.trace_id = str(self.trace_id).strip()
        self.intent_id = str(self.intent_id).strip()
        self.client_order_id = str(self.client_order_id).strip()
        if not self.trace_id or not self.intent_id or not self.client_order_id:
            raise ValueError("trace_id, intent_id and client_order_id are required")
        self.environment = str(self.environment).strip().upper()
        if self.environment != "TESTNET":
            raise ValueError("DecisionTrace only accepts TESTNET")
        if not isinstance(self.status, TraceStatus):
            self.status = TraceStatus(str(self.status).upper())
        self.timestamp = _parse_datetime(self.timestamp)
        self.updated_at = _parse_datetime(self.updated_at)
        # Deep-copy caller-owned containers so a trace already prepared for a
        # network write cannot be silently mutated after persistence.
        for field_name in (
            "pool",
            "strategy",
            "portfolio",
            "sizing",
            "order_request",
            "order_ack",
            "position_after",
            "fees",
            "funding",
            "slippage",
            "pnl",
            "reconciliation",
            "error",
            "metadata",
        ):
            setattr(self, field_name, copy.deepcopy(getattr(self, field_name)))
        self.factor_outputs = copy.deepcopy(list(self.factor_outputs))

    def payload(self) -> dict[str, Any]:
        """Return the canonical, secret-redacted trace payload."""

        data = {
            "trace_id": self.trace_id,
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "environment": self.environment,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "updated_at": self.updated_at,
            "pool": self.pool,
            "market_data_hash": self.market_data_hash,
            "factor_outputs": self.factor_outputs,
            "strategy": self.strategy,
            "portfolio": self.portfolio,
            "sizing": self.sizing,
            "exchange_rules_hash": self.exchange_rules_hash,
            "leverage_request": self.leverage_request,
            "leverage_readback": self.leverage_readback,
            "order_request": self.order_request,
            "order_request_hash": self.order_request_hash,
            "exchange_order_id": self.exchange_order_id,
            "order_ack": self.order_ack,
            "order_status": self.order_status,
            "executed_qty": self.executed_qty,
            "position_after": self.position_after,
            "fees": self.fees,
            "funding": self.funding,
            "slippage": self.slippage,
            "pnl": self.pnl,
            "reconciliation": self.reconciliation,
            "error": self.error,
            "metadata": self.metadata,
        }
        return cast(dict[str, Any], _redact(_serialize(data)))

    def compute_hash(self) -> str:
        material = dict(self.payload())
        material.pop("updated_at", None)
        material.pop("status", None)
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload["trace_hash"] = self.compute_hash()
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DecisionTrace:
        if not isinstance(value, dict):
            raise ValueError("trace snapshot must be an object")
        allowed = {
            "trace_id",
            "intent_id",
            "client_order_id",
            "environment",
            "status",
            "timestamp",
            "updated_at",
            "pool",
            "market_data_hash",
            "factor_outputs",
            "strategy",
            "portfolio",
            "sizing",
            "exchange_rules_hash",
            "leverage_request",
            "leverage_readback",
            "order_request",
            "order_request_hash",
            "exchange_order_id",
            "order_ack",
            "order_status",
            "executed_qty",
            "position_after",
            "fees",
            "funding",
            "slippage",
            "pnl",
            "reconciliation",
            "error",
            "metadata",
        }
        return cls(**{key: value[key] for key in allowed if key in value})


# The package uses both names for the same order-level fact contract.
ExecutionTruth = DecisionTrace


class DecisionTraceStore:
    """Crash-safe append-only local DecisionTrace store."""

    def __init__(self, path: str | os.PathLike[str] = ".beidou/testnet_verification/decision-trace.jsonl") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records: dict[str, DecisionTrace] = {}
        self.corrupt_tail_lines = 0
        self.loaded_event_count = 0
        self._load()

    def _load(self) -> None:
        self._records.clear()
        self.corrupt_tail_lines = 0
        self.loaded_event_count = 0
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("event") != "TRACE_SNAPSHOT":
                        raise ValueError("unknown trace event")
                    trace = DecisionTrace.from_dict(event["trace"])
                except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                    # A torn final line cannot erase the last complete fsynced
                    # snapshot.  Keep an audit count so callers can expose the
                    # recovery condition rather than silently pretending the
                    # journal was perfect.
                    self.corrupt_tail_lines += 1
                    continue
                self._records[trace.trace_id] = trace
                self.loaded_event_count += 1

    @contextmanager
    def _exclusive_journal_lock(self) -> Iterator[None]:
        """Serialize appends and compaction across local processes."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _encoded_snapshot(trace: DecisionTrace) -> bytes:
        event = {
            "event": "TRACE_SNAPSHOT",
            "schema_version": "1.0",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "trace": trace.to_dict(),
        }
        return (
            json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_snapshot(self, trace: DecisionTrace) -> None:
        encoded = self._encoded_snapshot(trace)
        with self._exclusive_journal_lock():
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_directory(self.path.parent)
        self.loaded_event_count += 1

    def compact(self) -> dict[str, Any]:
        """Archive the complete journal and atomically retain latest snapshots.

        The original byte stream is preserved as a verified gzip archive. The
        active path is replaced only after a complete compacted journal has
        been fsynced, while a hard link protects the original inode. No trace,
        including unresolved recovery state, is discarded.
        """

        with self._lock, self._exclusive_journal_lock():
            self._load()
            trace_count = len(self._records)
            unresolved_count = self.unresolved_count
            if not self.path.exists() or self.path.stat().st_size == 0:
                return {
                    "status": "NO_JOURNAL",
                    "trace_count": trace_count,
                    "unresolved_count": unresolved_count,
                }
            if self.loaded_event_count <= trace_count and self.corrupt_tail_lines == 0:
                return {
                    "status": "NOT_NEEDED",
                    "source_event_count": self.loaded_event_count,
                    "active_event_count": trace_count,
                    "trace_count": trace_count,
                    "unresolved_count": unresolved_count,
                }

            compacted = b"".join(self._encoded_snapshot(self._records[key]) for key in sorted(self._records))
            temporary = self.path.with_name(f".{self.path.name}.compact-{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, compacted)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

            source_sha256 = self._file_sha256(self.path)
            source_size = self.path.stat().st_size
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive_dir = self.path.parent / f"{self.path.stem}.archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_dir.chmod(0o700)
            raw_archive = archive_dir / f"{self.path.stem}.{stamp}.{source_sha256[:16]}.jsonl"
            os.link(self.path, raw_archive)
            self._fsync_directory(archive_dir)
            os.replace(temporary, self.path)
            self._fsync_directory(self.path.parent)

            compressed_archive = raw_archive.with_suffix(f"{raw_archive.suffix}.gz")
            compressed_temporary = compressed_archive.with_suffix(f"{compressed_archive.suffix}.tmp")
            compressed_descriptor = os.open(compressed_temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(compressed_descriptor, "wb") as destination:
                    with (
                        gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressor,
                        raw_archive.open("rb") as source,
                    ):
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            compressor.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
            except Exception:
                compressed_temporary.unlink(missing_ok=True)
                raise

            digest = hashlib.sha256()
            with gzip.open(compressed_temporary, "rb") as restored:
                for chunk in iter(lambda: restored.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != source_sha256:
                compressed_temporary.unlink(missing_ok=True)
                raise OSError("compressed DecisionTrace archive failed hash verification")
            os.replace(compressed_temporary, compressed_archive)
            compressed_archive.chmod(0o400)
            raw_archive.unlink()
            self._fsync_directory(archive_dir)

            compacted_sha256 = self._file_sha256(self.path)
            report: dict[str, Any] = {
                "status": "COMPACTED",
                "compacted_at": datetime.now(timezone.utc).isoformat(),
                "source_sha256": source_sha256,
                "source_size_bytes": source_size,
                "source_event_count": self.loaded_event_count,
                "corrupt_tail_lines_archived": self.corrupt_tail_lines,
                "archive_path": str(compressed_archive),
                "archive_compression": "gzip",
                "active_sha256": compacted_sha256,
                "active_size_bytes": self.path.stat().st_size,
                "active_event_count": trace_count,
                "trace_count": trace_count,
                "unresolved_count": unresolved_count,
            }
            manifest_path = archive_dir / "manifest.jsonl"
            manifest_descriptor = os.open(manifest_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(
                    manifest_descriptor,
                    (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
                )
                os.fsync(manifest_descriptor)
            finally:
                os.close(manifest_descriptor)
            self._fsync_directory(archive_dir)
            self.loaded_event_count = trace_count
            self.corrupt_tail_lines = 0
            return report

    def prepare(self, trace: DecisionTrace) -> DecisionTrace:
        """Durably create PREPARED before the first risk-increasing write."""

        if trace.status is not TraceStatus.PREPARED:
            raise ValueError("new trace must start PREPARED")
        with self._lock:
            existing = self._records.get(trace.trace_id)
            if existing is not None:
                if existing.intent_id != trace.intent_id or existing.client_order_id != trace.client_order_id:
                    raise ValueError("trace identity conflict")
                return existing
            self._append_snapshot(trace)
            self._records[trace.trace_id] = trace
            return trace

    def get(self, trace_id: str) -> DecisionTrace | None:
        with self._lock:
            trace = self._records.get(str(trace_id))
            return copy.deepcopy(trace) if trace is not None else None

    def all(self) -> list[DecisionTrace]:
        with self._lock:
            return [copy.deepcopy(trace) for trace in self._records.values()]

    def find_by_client_order_id(self, client_order_id: str) -> DecisionTrace | None:
        with self._lock:
            for trace in self._records.values():
                if trace.client_order_id == str(client_order_id):
                    return copy.deepcopy(trace)
        return None

    def update(self, trace_id: str, status: TraceStatus | str, **updates: Any) -> DecisionTrace:
        """Atomically append one valid state transition and its facts."""

        new_status = status if isinstance(status, TraceStatus) else TraceStatus(str(status).upper())
        with self._lock:
            current = self._records.get(str(trace_id))
            if current is None:
                raise KeyError(f"unknown trace_id: {trace_id}")
            if new_status != current.status and new_status not in _STATUS_TRANSITIONS[current.status]:
                raise ValueError(f"invalid trace transition {current.status.value}->{new_status.value}")
            if any(str(key).lower().replace("-", "_") in _SECRET_KEYS for key in updates):
                raise ValueError("secret fields cannot be persisted")
            illegal = set(updates) - set(DecisionTrace.__dataclass_fields__)
            if illegal:
                raise ValueError(f"unknown trace fields: {sorted(illegal)}")
            candidate = copy.deepcopy(current)
            candidate.status = new_status
            candidate.updated_at = datetime.now(timezone.utc)
            for key, value in updates.items():
                setattr(candidate, key, copy.deepcopy(value))
            candidate.__post_init__()
            if (
                new_status in {TraceStatus.ACKED, TraceStatus.FILLED, TraceStatus.CLOSED}
                and not candidate.exchange_order_id
            ):
                raise ValueError("venue order identity is required before acknowledgement/terminal status")
            self._append_snapshot(candidate)
            self._records[candidate.trace_id] = candidate
            return copy.deepcopy(candidate)

    def recovery_candidates(self) -> list[DecisionTrace]:
        """Return traces requiring query-before-retry or execution resume."""

        with self._lock:
            return [
                copy.deepcopy(trace)
                for trace in self._records.values()
                if trace.status
                in {
                    TraceStatus.PREPARED,
                    TraceStatus.SUBMITTED,
                    TraceStatus.UNKNOWN,
                    TraceStatus.ACKED,
                    TraceStatus.FILLED,
                }
            ]

    @property
    def unresolved_count(self) -> int:
        return sum(
            1 for trace in self._records.values() if trace.status not in {TraceStatus.CLOSED, TraceStatus.FAILED}
        )


__all__ = ["DecisionTrace", "DecisionTraceStore", "ExecutionTruth", "TraceStatus"]
