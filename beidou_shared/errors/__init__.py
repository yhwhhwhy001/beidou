"""错误语义与故障恢复类型。查询失败≠空结果。UNKNOWN 时必须 Fail-Closed。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

class ErrorCategory(str, Enum):
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    EXCHANGE_UNAVAILABLE = "EXCHANGE_UNAVAILABLE"
    EXCHANGE_ERROR = "EXCHANGE_ERROR"
    EXCHANGE_MAINTENANCE = "EXCHANGE_MAINTENANCE"
    EXCHANGE_RULE_CHANGE = "EXCHANGE_RULE_CHANGE"
    AUTH_FAILURE = "AUTH_FAILURE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    IP_RESTRICTED = "IP_RESTRICTED"
    DATA_CORRUPTION = "DATA_CORRUPTION"
    DATA_GAP = "DATA_GAP"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    DATA_QUALITY_FAIL = "DATA_QUALITY_FAIL"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_LIMIT = "POSITION_LIMIT"
    RISK_DENIED = "RISK_DENIED"
    APPROVAL_MISSING = "APPROVAL_MISSING"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    STATE_CORRUPTION = "STATE_CORRUPTION"
    UNKNOWN = "UNKNOWN"

class FaultSeverity(str, Enum):
    P0_CRITICAL = "P0_CRITICAL"
    P1_MAJOR = "P1_MAJOR"
    P2_MINOR = "P2_MINOR"

class RecoveryAction(str, Enum):
    NOOP = "NOOP"
    RETRY = "RETRY"
    FAILOVER = "FAILOVER"
    CHECKPOINT_ROLLBACK = "CHECKPOINT_ROLLBACK"
    DEGRADE = "DEGRADE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    LOCK = "LOCK"
    ALERT_ONLY = "ALERT_ONLY"
    MANUAL = "MANUAL"

@dataclass(frozen=True, slots=True)
class DomainError:
    category: ErrorCategory
    severity: FaultSeverity
    message: str
    correlation_id: str | None = None
    venue_id: str | None = None
    account_id: str | None = None
    instrument_id: str | None = None
    raw_response: dict[str, Any] | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    retryable: bool = False
    recommended_action: RecoveryAction = RecoveryAction.NOOP

    def should_fail_closed(self) -> bool:
        return self.category in (
            ErrorCategory.UNKNOWN,
            ErrorCategory.AUTH_FAILURE,
            ErrorCategory.PERMISSION_DENIED,
            ErrorCategory.STATE_CORRUPTION,
            ErrorCategory.CONFIGURATION_ERROR,
        )
