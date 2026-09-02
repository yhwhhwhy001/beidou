"""Neutral Alpha-to-Execution data and protocol contracts.

Only value objects, enums, and protocols belong here.  Concrete exchange,
safety, launcher, and client construction stays in later composition tasks.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, cast

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId

ALPHA_STATE_SCHEMA_VERSION = "2.0"
POSITION_SOURCE_CLASS = "STRATEGY_SLEEVE_POSITION_FACT"
COST_SOURCE_CLASS = "COST_MODEL_EVIDENCE"
STATEFUL_ALPHA_STRATEGY_ID = StrategyId("offline-alpha-v1")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UPPER_SCOPE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}")


class AlphaStateContractError(ValueError):
    """Stable fail-closed error emitted by the stateful Alpha boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


class AlphaTargetSemantics(str, Enum):
    PRESERVE_VERIFIED_POSITION = "PRESERVE_VERIFIED_POSITION"
    FORECAST_RAW_SCORE = "FORECAST_RAW_SCORE"


def canonical_contract_timestamp(value: datetime) -> str:
    """Return the single UTC representation used by v2 contract digests."""

    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_contract_digest(domain: str, value: Mapping[str, Any]) -> str:
    """Domain-separated SHA-256 over the frozen v2 canonical JSON format."""

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload).hexdigest()


def _finite_number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlphaStateContractError(code)
    result = float(cast(Any, value))
    if not math.isfinite(result):
        raise AlphaStateContractError(code)
    return 0.0 if result == 0.0 else result


def _require_aware(value: datetime, code: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlphaStateContractError(code)


def _require_digest(value: object, code: str) -> None:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None or set(value) == {"0"}:
        raise AlphaStateContractError(code)


def _require_scope(value: object, code: str, *, fixed: str | None = None) -> str:
    if not isinstance(value, str):
        raise AlphaStateContractError(code)
    text = value
    if not text or text != text.strip():
        raise AlphaStateContractError(code)
    if fixed is not None:
        if text != fixed:
            raise AlphaStateContractError(code)
    elif _UPPER_SCOPE.fullmatch(text) is None:
        raise AlphaStateContractError(code)
    return text


@dataclass(frozen=True, slots=True)
class BoundPositionSnapshot:
    """Caller-bound strategy-sleeve position fact; authenticity stays upstream."""

    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    current_weight: float
    observed_at: datetime
    valid_until: datetime
    source_artifact_digest: str
    source_class: str = POSITION_SOURCE_CLASS
    schema_version: str = ALPHA_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_STATE_SCHEMA_VERSION:
            raise AlphaStateContractError("POSITION_SCHEMA_UNSUPPORTED")
        if self.source_class != POSITION_SOURCE_CLASS:
            raise AlphaStateContractError("POSITION_SOURCE_CLASS_INVALID")
        _require_scope(self.strategy_id, "POSITION_SCOPE_INVALID", fixed=str(STATEFUL_ALPHA_STRATEGY_ID))
        _require_scope(self.instrument_id, "POSITION_SCOPE_INVALID")
        _require_scope(self.venue_id, "POSITION_SCOPE_INVALID")
        _finite_number(self.current_weight, "POSITION_VALUE_NOT_FINITE")
        _require_aware(self.observed_at, "POSITION_TIMESTAMP_NOT_AWARE")
        _require_aware(self.valid_until, "POSITION_TIMESTAMP_NOT_AWARE")
        if self.valid_until < self.observed_at:
            raise AlphaStateContractError("POSITION_VALIDITY_WINDOW_INVALID")
        _require_digest(self.source_artifact_digest, "POSITION_SOURCE_DIGEST_INVALID")

    @property
    def binding_digest(self) -> str:
        return canonical_contract_digest(
            "beidou.alpha.position-snapshot.v2",
            {
                "schema_version": self.schema_version,
                "source_class": self.source_class,
                "strategy_id": str(self.strategy_id),
                "instrument_id": str(self.instrument_id),
                "venue_id": str(self.venue_id),
                "current_weight": _finite_number(self.current_weight, "POSITION_VALUE_NOT_FINITE"),
                "observed_at": canonical_contract_timestamp(self.observed_at),
                "valid_until": canonical_contract_timestamp(self.valid_until),
                "source_artifact_digest": self.source_artifact_digest,
            },
        )


@dataclass(frozen=True, slots=True)
class BoundCostSnapshot:
    """Caller-bound cost fact; it does not assert calibration or custodian authenticity."""

    instrument_id: InstrumentId
    venue_id: VenueId
    expected_fee_bps: float
    expected_slippage_bps: float
    expected_funding_bps: float
    observed_at: datetime
    valid_until: datetime
    source_artifact_digest: str
    source_class: str = COST_SOURCE_CLASS
    schema_version: str = ALPHA_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ALPHA_STATE_SCHEMA_VERSION:
            raise AlphaStateContractError("COST_SCHEMA_UNSUPPORTED")
        if self.source_class != COST_SOURCE_CLASS:
            raise AlphaStateContractError("COST_SOURCE_CLASS_INVALID")
        _require_scope(self.instrument_id, "COST_SCOPE_INVALID")
        _require_scope(self.venue_id, "COST_SCOPE_INVALID")
        fee = _finite_number(self.expected_fee_bps, "COST_VALUE_NOT_FINITE")
        slippage = _finite_number(self.expected_slippage_bps, "COST_VALUE_NOT_FINITE")
        _finite_number(self.expected_funding_bps, "COST_VALUE_NOT_FINITE")
        if fee < 0.0 or slippage < 0.0:
            raise AlphaStateContractError("COST_VALUE_NEGATIVE")
        _require_aware(self.observed_at, "COST_TIMESTAMP_NOT_AWARE")
        _require_aware(self.valid_until, "COST_TIMESTAMP_NOT_AWARE")
        if self.valid_until < self.observed_at:
            raise AlphaStateContractError("COST_VALIDITY_WINDOW_INVALID")
        _require_digest(self.source_artifact_digest, "COST_SOURCE_DIGEST_INVALID")

    @property
    def binding_digest(self) -> str:
        return canonical_contract_digest(
            "beidou.alpha.cost-snapshot.v2",
            {
                "schema_version": self.schema_version,
                "source_class": self.source_class,
                "instrument_id": str(self.instrument_id),
                "venue_id": str(self.venue_id),
                "expected_fee_bps": _finite_number(self.expected_fee_bps, "COST_VALUE_NOT_FINITE"),
                "expected_slippage_bps": _finite_number(self.expected_slippage_bps, "COST_VALUE_NOT_FINITE"),
                "expected_funding_bps": _finite_number(self.expected_funding_bps, "COST_VALUE_NOT_FINITE"),
                "observed_at": canonical_contract_timestamp(self.observed_at),
                "valid_until": canonical_contract_timestamp(self.valid_until),
                "source_artifact_digest": self.source_artifact_digest,
            },
        )


class ExecutionAuthorization(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


class ExecutionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AlphaTarget:
    """Alpha's desired exposure; it is not an order or a venue request."""

    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    target_weight: float
    forecast_hash: str
    model_version: SchemaVersion
    timestamp: datetime

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.target_weight)):
            raise ValueError("target_weight must be finite")
        if not self.forecast_hash.strip():
            raise ValueError("forecast_hash must be non-empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Execution-neutral intent that can be handed to an injected port."""

    intent_id: str
    target: AlphaTarget
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        if self.requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionFact:
    """A venue-independent fact returned by a future execution adapter."""

    intent_id: str
    status: ExecutionStatus
    filled_weight: float | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        if self.filled_weight is not None and not math.isfinite(float(self.filled_weight)):
            raise ValueError("filled_weight must be finite when present")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


class ExecutionAuthorizationPort(Protocol):
    """Port for a later composition root to authorize a neutral intent."""

    def authorize(self, intent: OrderIntent) -> ExecutionAuthorization:
        """Return an explicit authorization outcome without constructing a client."""


class ExecutionPort(Protocol):
    """Port for a later composition root to submit an already-authorized intent."""

    def submit(self, intent: OrderIntent) -> ExecutionFact:
        """Return a fact; the protocol does not define transport or retries."""
