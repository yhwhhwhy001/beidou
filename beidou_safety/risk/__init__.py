"""风险评估模块。风险链：Pre-Risk → Risk Decision → Approval → Post-Risk。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import AccountRef, CorrelationId, InstrumentId, MonetaryValue, OrderId, Quantity, RiskApprovalId, RiskDecision, VenueId

class RiskRuleLevel(str, Enum):
    R0 = "R0"; R1 = "R1"; R2 = "R2"; R3 = "R3"; R4 = "R4"
    R5 = "R5"; R6 = "R6"; R7 = "R7"; R8 = "R8"; R9 = "R9"; R10 = "R10"

@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    rule_level: RiskRuleLevel
    decision: RiskDecision
    reason: str
    correlation_id: CorrelationId
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass
class PreRiskContext:
    account_ref: AccountRef
    instrument_id: InstrumentId
    order_quantity: Quantity
    order_price: MonetaryValue | None = None
    leverage: float | None = None
    current_position: Quantity | None = None
    current_margin: MonetaryValue | None = None
    pending_orders: list[OrderId] = field(default_factory=list)
    correlation_id: CorrelationId | None = None

# Concrete implementations from engine module
from .engine import (
    PreRiskCheckerImpl as PreRiskChecker,
    RiskEngineImpl as RiskEngine,
    RiskApprovalSignerImpl as RiskApprovalSigner,
    RiskApprovalStateMachine,
    PostRiskMonitor,
    RiskSnapshot,
)
