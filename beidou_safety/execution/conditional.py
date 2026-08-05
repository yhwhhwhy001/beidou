"""条件订单、仓位生命周期、资金费感知与应急平仓协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import (
    AccountId,
    CorrelationId,
    InstrumentId,
    OrderId,
    Price,
    Quantity,
    VenueInstrument,
)


@dataclass
class EmergencyFlattenPolicy:
    """应急平仓协议。签名、版本化、不可在事故中热改。"""

    policy_id: str
    version: str
    signature: str
    position_ordering: str = "MOST_LIQUID_FIRST"
    execution_strategy: str = "AGGRESSIVE_TWAP"
    max_slippage_bps: float = 100.0
    partial_failure_handling: str = "CONTINUE_WITH_NEXT"
    cancel_protection_orders: bool = True
    post_flatten_lock: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ConditionalOrder:
    order_id: OrderId
    venue_instrument: VenueInstrument
    trigger_price: Price
    order_price: Price | None = None
    trigger_type: str = "STOP_MARKET"
    reduce_only: bool = False
    quantity: Quantity | None = None
    status: str = "ACTIVE"
    correlation_id: CorrelationId | None = None


class PositionManager:
    """仓位生命周期管理器。资金费感知，应急平仓。"""

    def __init__(self):
        self._positions: dict[str, dict] = {}

    def update_position(
        self, account_id: AccountId, instrument_id: InstrumentId, qty: Quantity, entry_price: Price
    ) -> None:
        self._positions[f"{account_id}:{instrument_id}"] = {"qty": qty, "entry_price": entry_price}

    def funding_rate_aware_position_size(self, current_qty: float, funding_rate: float, net_alpha: float) -> float:
        """资金费是净收益和风险输入，不是机械加仓信号。"""
        if abs(funding_rate) > 0.001:
            return current_qty * 0.8  # Reduce position, don't increase
        return current_qty

    def emergency_flatten(self, policy: EmergencyFlattenPolicy) -> dict[str, str]:
        return {
            "action": "FLATTEN_ALL",
            "strategy": policy.execution_strategy,
            "max_slippage": str(policy.max_slippage_bps),
            "post_action": "LOCK" if policy.post_flatten_lock else "NO_NEW_RISK",
        }
