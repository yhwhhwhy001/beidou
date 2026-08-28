"""Durable order-level DecisionTrace for Testnet verification.

The trace is an append-only, fsync-backed JSONL journal of immutable
snapshots.  Each update carries the complete current snapshot, so a restart
can rebuild state without relying on log text or an in-memory object.  Secrets
and venue HMAC material are rejected/redacted at the boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast


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
        self._load()

    def _load(self) -> None:
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

    def _append_snapshot(self, trace: DecisionTrace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "event": "TRACE_SNAPSHOT",
            "schema_version": "1.0",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "trace": trace.to_dict(),
        }
        encoded = (
            json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Persist the directory entry as well when the platform supports it.
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            directory = -1
        if directory >= 0:
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

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
