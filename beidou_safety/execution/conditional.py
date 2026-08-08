"""条件订单、仓位生命周期、资金费感知与应急平仓协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_exchange.binance_usdm.endpoints import Endpoint
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
        """返回应急平仓操作指令，由调用方引擎执行实际下单。

        返回结构:
          action: "FLATTEN_ALL"
          orders: JSON 序列化的平仓订单列表
          strategy: 执行策略
          max_slippage_bps: 最大滑点
          post_action: 平仓后控制动作
        """
        import json as _json

        orders: list[dict] = []
        for pos_key, pos_data in self._positions.items():
            account_id, instrument_id = pos_key.split(":", 1)
            qty = pos_data.get("qty")
            if qty is None:
                continue
            amount = float(qty.amount) if hasattr(qty, "amount") else float(qty)
            if abs(amount) < 1e-8:
                continue
            # 反向平仓: LONG → SELL, SHORT → BUY
            side = "SELL" if amount > 0 else "BUY"
            orders.append(
                {
                    "instrument_id": instrument_id,
                    "account_id": account_id,
                    "side": side,
                    "quantity": abs(amount),
                    "order_type": "MARKET",
                    "reduce_only": True,
                }
            )

        return {
            "action": "FLATTEN_ALL",
            "orders": _json.dumps(orders),
            "order_count": str(len(orders)),
            "strategy": policy.execution_strategy,
            "max_slippage_bps": str(policy.max_slippage_bps),
            "post_action": "LOCK" if policy.post_flatten_lock else "NO_NEW_RISK",
        }

    async def execute_flatten(
        self,
        policy: EmergencyFlattenPolicy,
        engine: Any = None,
    ) -> dict[str, str]:
        """执行应急平仓 — 通过 engine API 实际下单。

        如果提供 engine 引用，则遍历所有持仓并以市价反向平仓。
        返回执行结果摘要。
        """
        flatten_plan = self.emergency_flatten(policy)
        if engine is None:
            return {**flatten_plan, "executed": "false", "reason": "no_engine_reference"}

        import json as _json

        try:
            orders = _json.loads(flatten_plan.get("orders", "[]"))
        except Exception:
            return {**flatten_plan, "executed": "false", "reason": "order_parse_error"}

        executed = 0
        failed = 0
        for order_spec in orders:
            try:
                symbol = order_spec["instrument_id"]
                side = order_spec["side"]
                qty = order_spec["quantity"]
                # Use engine's exchange API to place MARKET close order
                params = {
                    "symbol": symbol,
                    "side": side,
                    "type": "MARKET",
                    "quantity": f"{qty:.3f}",
                    "reduceOnly": "true",
                }
                result = await engine._api_async(
                    Endpoint.ORDER, method="POST", signed=True, params=params
                )
                if "orderId" in result:
                    executed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {
            **flatten_plan,
            "executed": "true",
            "executed_count": str(executed),
            "failed_count": str(failed),
        }
