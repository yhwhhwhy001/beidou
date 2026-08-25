"""Neutral Alpha-to-Execution data and protocol contracts.

Only value objects, enums, and protocols belong here.  Concrete exchange,
safety, launcher, and client construction stays in later composition tasks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId


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
