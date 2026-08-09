"""订单执行模块。单活 Executor、Lease 机制、Fencing 保护。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

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
    # HMAC-bound canonical order material.  ``risk_proposal_hash`` describes
    # the strategy/risk proposal; this separate digest prevents a durable
    # payload mutation from changing the exact venue order after approval.
    risk_intent_hash: str = ""
    risk_account_snapshot_hash: str = ""
    risk_snapshot_hash: str = ""
    risk_policy_version: str = ""
    risk_nonce: str = ""
    risk_expires_at: float | None = None
    # Execution economics must be explicit; zero means unavailable and blocks
    # risk-increasing live sends at the final planner boundary.
    net_alpha_bps: float = 0.0
    predicted_cost_bps: float = 0.0


def order_intent_binding_hash(intent: Any) -> str:
    """Hash every venue-relevant field of an intent deterministically.

    The digest intentionally excludes approval/signature metadata: those
    fields are the envelope that signs this digest.  It includes identity,
    sizing, order semantics, idempotency and client ID so changing any part of
    the final adapter request invalidates the approval at send time.
    """

    price = getattr(intent, "price", None)
    material = {
        "intent_id": str(getattr(intent, "intent_id", "")),
        "account_venue_id": str(getattr(getattr(intent, "account_ref", None), "venue_id", "")),
        "account_id": str(getattr(getattr(intent, "account_ref", None), "account_id", "")),
        "instrument_id": str(getattr(intent, "instrument_id", "")),
        "side": str(getattr(getattr(intent, "side", None), "value", getattr(intent, "side", ""))),
        "order_type": str(getattr(getattr(intent, "order_type", None), "value", getattr(intent, "order_type", ""))),
        "quantity": str(getattr(getattr(intent, "quantity", None), "amount", "")),
        "quantity_decimals": int(getattr(getattr(intent, "quantity", None), "decimals", 8)),
        "price": str(getattr(price, "amount", "")) if price is not None else None,
        "price_decimals": int(getattr(price, "decimals", 8)) if price is not None else 8,
        "time_in_force": str(
            getattr(getattr(intent, "time_in_force", None), "value", getattr(intent, "time_in_force", ""))
        ),
        "client_order_id": str(getattr(intent, "client_order_id", "") or ""),
        "idempotency_key": str(getattr(intent, "idempotency_key", "") or ""),
        "reduce_only": bool(getattr(intent, "reduce_only", False)),
        "close_position": bool(getattr(intent, "close_position", False)),
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
