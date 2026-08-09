"""订单执行模块。单活 Executor、Lease 机制、Fencing 保护。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import (
    AccountRef,
    CorrelationId,
    ExecutionId,
    InstrumentId,
    MonetaryValue,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueId,
)


class ExecutorState(str, Enum):
    STANDBY = "STANDBY"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    account_ref: AccountRef
    instrument_id: InstrumentId
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    price: Price | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str | None = None
    correlation_id: CorrelationId | None = None
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    risk_approval_id: str | None = None
    reduce_only: bool = False
    close_position: bool = False
    emergency_policy_signed: bool = False
    # Final-send approval envelope.  These fields are immutable intent data;
    # the executor must revalidate them immediately before the adapter call.
    risk_approval_signature: str | None = None
    risk_proposal_hash: str = ""
    risk_account_snapshot_hash: str = ""
    risk_snapshot_hash: str = ""
    risk_policy_version: str = ""
    risk_nonce: str = ""
    risk_expires_at: float | None = None
    # Execution economics must be explicit; zero means unavailable and blocks
    # risk-increasing live sends at the final planner boundary.
    net_alpha_bps: float = 0.0
    predicted_cost_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    execution_id: ExecutionId
    order_id: OrderId
    intent_id: str
    status: OrderStatus
    filled_quantity: Quantity | None = None
    average_price: Price | None = None
    commission: MonetaryValue | None = None
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response: dict | None = None


# Note: Concrete implementations are in submodules:
# - beidou_safety.executor_impl: LeaseManager, FencingProtection
# - beidou_safety.execution.intent: IntentOutbox
# - beidou_safety.execution.order_state: OrderStateTracker, OrderEvent, UnknownRecoveryHandler
# - beidou_safety.execution.ledger: ImmutableLedger, JournalEntry
# - beidou_safety.execution.reconciliation: ReconciliationEngine, AccountFactSnapshot
# - beidou_safety.execution.conditional: EmergencyFlattenPolicy, PositionManager
# - beidou_safety.execution.algorithms: ExecutionAlgorithmSelector, TWAP/POV/AdaptiveSlice/IOC/etc.
# Import them directly from their submodules to avoid circular imports.
