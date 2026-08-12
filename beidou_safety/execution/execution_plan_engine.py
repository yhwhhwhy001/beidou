"""BD-CV40: ExecutionPlan 引擎。

无适用算法 → NOT_EXECUTABLE，不得默认 EMERGENCY_REDUCE_ONLY。
Emergency plan 绝不增加绝对仓位。
绑定后 PlanSlice 任一经济字段不可修改。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionAlgorithm(str, Enum):
    TWAP = "TWAP"
    VWAP = "VWAP"
    POV = "POV"
    AGGRESSIVE_LIMIT = "AGGRESSIVE_LIMIT"
    MARKET = "MARKET"
    EMERGENCY = "EMERGENCY"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"


@dataclass(frozen=True)
class BoundPlanSlice:
    """BD-CV40: 绑定后不可修改的经济字段。"""

    slice_id: str
    symbol: str
    side: str
    quantity: str
    limit_price: str
    order_type: str
    time_in_force: str
    reduce_only: bool = False
    position_effect: str = "UNKNOWN"
    client_order_id: str = ""
    rule_snapshot_id: str = ""
    slice_hash: str = ""

    def compute_hash(self) -> str:
        data = {
            "slice_id": self.slice_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "position_effect": self.position_effect,
            "client_order_id": self.client_order_id,
            "rule_snapshot_id": self.rule_snapshot_id,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @classmethod
    def bind(cls, **fields: Any) -> BoundPlanSlice:
        """创建绑定切片并计算 hash。"""
        s = cls(**{k: v for k, v in fields.items() if k in cls.__dataclass_fields__})
        object.__setattr__(s, "slice_hash", s.compute_hash())
        return s


@dataclass
class ExecutionPlanEngine:
    """BD-CV40: 执行计划引擎。

    选择算法 → 生成切片 → 绑定 → 不可修改。
    """

    def select_algorithm(
        self,
        symbol: str,
        quantity: float,
        urgency: str = "normal",
        market_impact_bps: float = 5.0,
        spread_bps: float = 2.0,
        daily_volume: float = 0.0,
        is_emergency: bool = False,
    ) -> ExecutionAlgorithm:
        """BD-CV40: 算法选择器。

        无适用算法时返回 NOT_EXECUTABLE。
        """
        if is_emergency:
            return ExecutionAlgorithm.EMERGENCY
        if quantity <= 0:
            return ExecutionAlgorithm.NOT_EXECUTABLE

        # 极小量 → MARKET
        if quantity < 0.001:
            return ExecutionAlgorithm.MARKET

        # 大单/高冲击 → TWAP
        if market_impact_bps > 10.0 or (daily_volume > 0 and quantity / max(daily_volume, 0.01) > 0.01):
            return ExecutionAlgorithm.TWAP

        # 中等单 → AGGRESSIVE_LIMIT
        if spread_bps < 5.0:
            return ExecutionAlgorithm.AGGRESSIVE_LIMIT

        # 默认 → TWAP
        return ExecutionAlgorithm.TWAP

    def create_emergency_plan(self, symbol: str, current_position: float, side: str) -> list[BoundPlanSlice]:
        """BD-CV40: 创建 Emergency plan。

        真实 current_position 推导 side/qty。
        slice.reduce_only=True, position_effect=REDUCE_ONLY。
        绝不增加绝对仓位。
        """
        if current_position == 0:
            return []

        is_long = current_position > 0
        qty = abs(current_position)

        return [
            BoundPlanSlice.bind(
                slice_id=f"emerg-{symbol}-1",
                symbol=symbol,
                side="SELL" if is_long else "BUY",
                quantity=str(qty),
                limit_price="0",
                order_type="MARKET",
                time_in_force="IOC",
                reduce_only=True,
                position_effect="REDUCE_ONLY",
                client_order_id=(
                    f"emerg-{symbol}-{int(hashlib.sha256(str(current_position).encode()).hexdigest()[:8], 16)}"
                ),
            )
        ]

    def create_twap_plan(
        self, symbol: str, side: str, total_qty: float, n_slices: int = 5, limit_price: str = "0"
    ) -> list[BoundPlanSlice]:
        """BD-CV40: 创建 TWAP 执行计划。"""
        if n_slices <= 0 or total_qty <= 0 or side not in {"BUY", "SELL"}:
            return []

        qty_per_slice = str(total_qty / n_slices)
        slices = []
        for i in range(n_slices):
            slices.append(
                BoundPlanSlice.bind(
                    slice_id=f"twap-{symbol}-{i + 1}",
                    symbol=symbol,
                    side=side,
                    quantity=qty_per_slice,
                    limit_price=limit_price,
                    order_type="LIMIT",
                    time_in_force="GTC",
                    client_order_id=f"twap-{symbol}-{i + 1}-{int(time.time() * 1000)}",
                )
            )
        return slices

    def validate_emergency_plan(self, plan: list[BoundPlanSlice], current_position: float) -> bool:
        """BD-CV40 AC-40-02: Emergency plan 绝不增加绝对仓位。"""
        if current_position == 0:
            return not plan
        expected_side = "SELL" if current_position > 0 else "BUY"
        try:
            quantities = [float(item.quantity) for item in plan]
        except (TypeError, ValueError):
            return False
        return (
            all(quantity > 0 for quantity in quantities)
            and all(item.reduce_only for item in plan)
            and all(item.position_effect == "REDUCE_ONLY" for item in plan)
            and all(item.side == expected_side for item in plan)
            and sum(quantities) <= abs(current_position)
        )
