"""监控核心类型 — V1.1 (MON01-01/02: INV-001~012)。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AccountPositionMode(str, Enum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"
    UNKNOWN = "UNKNOWN"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class CheckSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class HealthStatus(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


class FrequencyLevel(str, Enum):
    ALERT = "ALERT"
    NORMAL = "NORMAL"
    STABLE = "STABLE"


class IncidentStatus(str, Enum):
    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    MITIGATING = "MITIGATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    LOCKED = "LOCKED"


class TraceStage(str, Enum):
    SIGNAL_CREATED = "SIGNAL_CREATED"
    INTENT_CREATED = "INTENT_CREATED"
    PRE_RISK_DONE = "PRE_RISK_DONE"
    RISK_APPROVED = "RISK_APPROVED"
    OUTBOX_ENQUEUED = "OUTBOX_ENQUEUED"
    EXECUTOR_SENT = "EXECUTOR_SENT"
    EXCHANGE_ACKED = "EXCHANGE_ACKED"
    FILLED = "FILLED"
    LEDGER_POSTED = "LEDGER_POSTED"
    POSITION_OBSERVED = "POSITION_OBSERVED"
    PROTECTION_CONFIRMED = "PROTECTION_CONFIRMED"
    RECONCILED = "RECONCILED"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


class RolloutMode(str, Enum):
    OBSERVE = "observe"
    ENFORCE = "enforce"


@dataclass(frozen=True, slots=True)
class PositionKey:
    venue: str
    account_id: str
    symbol: str
    position_side: PositionSide

    @classmethod
    def build(cls, *, venue, account_id, symbol, mode, position_side=PositionSide.BOTH):
        if mode == AccountPositionMode.ONE_WAY:
            return cls(venue=venue, account_id=account_id, symbol=symbol, position_side=PositionSide.BOTH)
        if mode == AccountPositionMode.HEDGE:
            if position_side not in (PositionSide.LONG, PositionSide.SHORT):
                raise ValueError("HEDGE requires LONG/SHORT")
            return cls(venue=venue, account_id=account_id, symbol=symbol, position_side=position_side)
        return cls(venue=venue, account_id=account_id, symbol=symbol, position_side=position_side)

    def __str__(self):
        return f"{self.venue}/{self.account_id}/{self.symbol}/{self.position_side.value}"


@dataclass(slots=True)
class PositionModeEvidence:
    account_id: str
    venue: str
    mode: AccountPositionMode
    source: str = ""
    source_timestamp: float | None = None
    observed_at: float = 0.0
    raw_response: dict | None = None
    evidence_hash: str = ""
    error: str | None = None


@dataclass(slots=True)
class MonitoringCheckResult:
    check_id: str
    entity_type: str = ""
    entity_id: str = ""
    status: CheckStatus = CheckStatus.UNKNOWN
    severity: CheckSeverity = CheckSeverity.P2
    message: str = ""
    expected: Any = None
    actual: Any = None
    source: str = ""
    source_timestamp: float | None = None
    observed_at: float = 0.0
    fact_age_ms: float | None = None
    trace_id: str = ""
    correlation_id: str = ""
    remediation: str = ""
    remediation_result: str = ""
    evidence_hash: str = ""
    rollout_mode: str = "enforce"
    policy_version: str = "1.1"

    def compute_evidence_hash(self):
        p = {
            "check_id": self.check_id,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "expected": str(self.expected),
            "actual": str(self.actual),
            "source": self.source,
            "source_timestamp": self.source_timestamp,
            "observed_at": self.observed_at,
            "trace_id": self.trace_id,
        }
        return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:32]


@dataclass(slots=True)
class FrequencyState:
    level: FrequencyLevel = FrequencyLevel.ALERT
    interval_seconds: int = 600
    promotion_clean_streak: int = 0
    stable_since: float = 0.0
    last_check_at: float = 0.0
    next_due_at: float = 0.0
    last_level_change_at: float = 0.0
    last_reason: str = ""
    policy_version: str = "1.1"


@dataclass(slots=True)
class Incident:
    incident_id: str
    dedupe_key: str
    status: IncidentStatus = IncidentStatus.DETECTED
    severity: CheckSeverity = CheckSeverity.P1
    title: str = ""
    description: str = ""
    source_check_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    root_cause_fingerprint: str = ""
    detected_at: float = 0.0
    confirmed_at: float | None = None
    mitigated_at: float | None = None
    verified_at: float | None = None
    resolved_at: float | None = None
    evidence_hashes: list = field(default_factory=list)
    safe_action_taken: str = ""
    remediation_result: str = ""
    parent_incident_id: str | None = None
    is_systemic: bool = False


@dataclass(slots=True)
class IncidentLink:
    parent_incident_id: str
    child_incident_id: str
    linked_at: float = 0.0
    root_cause_fingerprint: str = ""


@dataclass(slots=True)
class OrderTraceEvent:
    trace_id: str
    correlation_id: str
    strategy_id: str
    intent_id: str
    client_order_id: str
    stage: TraceStage
    source_timestamp: float
    observed_at: float = 0.0
    symbol: str = ""
    side: str = ""
    quantity: str = ""
    price: str = ""
    order_id: str = ""
    evidence: dict | None = None


@dataclass(slots=True)
class ModuleProgressContract:
    module_id: str
    criticality: CheckSeverity
    heartbeat_indicator: str = ""
    progress_indicator: str = ""
    progress_timeout_seconds: float = 300.0
    expected_min_progress_delta: float = 1.0
    functional_probe: str = ""
    dependencies: list = field(default_factory=list)
    lifecycle_state: str = "ACTIVE"
    last_progress_at: float = 0.0
    last_probe_at: float = 0.0


@dataclass(slots=True)
class RolloutState:
    check_id: str
    enabled: bool = True
    mode: RolloutMode = RolloutMode.ENFORCE
    locked_safety_check: bool = False
    introduced_version: str = "1.1"
    policy_version: str = "1.1"


@dataclass(slots=True)
class ComponentHealth:
    component: str
    healthy: bool
    last_heartbeat: float = 0.0
    last_error: str = ""
    consecutive_failures: int = 0
    safe_action: str = ""
    recovery_gate: str = ""


INVARIANTS = {
    "INV-001": "Position must have SL",
    "INV-002": "API failure != empty",
    "INV-003": "Safety checks independent of audit gate",
    "INV-004": "Alive != healthy",
    "INV-005": "Repair != success",
    "INV-006": "UNKNOWN != GREEN",
    "INV-007": "P0 not diluted by averages",
    "INV-008": "Position Mode authoritative",
    "INV-009": "Monitor not break execution reserve",
    "INV-010": "Auto remediation allowlisted",
    "INV-011": "Evidence failure != fully healthy",
    "INV-012": "72h real elapsed time",
}


def compute_evidence_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def check_invariant(inv_id, condition, detail=""):
    if condition:
        return True, f"{inv_id}: OK"
    return False, f"{inv_id} VIOLATED: {INVARIANTS.get(inv_id, 'Unknown')}. {detail}".strip()
