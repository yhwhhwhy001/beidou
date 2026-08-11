"""BD-CV21: 统一真实成本与容量模型。

输出 capacity point estimate + CI + limiting factor。
研究/Paper 使用同一成本 contract。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostComponents:
    """一笔交易的真实成本分解。"""

    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    spread_bps: float = 0.0
    funding_rate_hourly: float = 0.0
    latency_bps: float = 0.0
    total_bps: float = 0.0
    is_verified: bool = False

    def compute_total(self) -> float:
        return self.commission_bps + self.slippage_bps + self.spread_bps + self.latency_bps


@dataclass(frozen=True)
class CapacityEstimate:
    """BD-CV21: 容量估算 — point estimate + CI + limiting factor。"""

    symbol: str
    max_position_notional: float = 0.0
    confidence_interval_low: float = 0.0
    confidence_interval_high: float = 0.0
    limiting_factor: str = "UNKNOWN"
    daily_volume: float = 0.0
    orderbook_depth_1pct: float = 0.0
    participation_rate: float = 0.01
    is_stale: bool = True

    @classmethod
    def estimate(
        cls,
        symbol: str,
        daily_volume: float,
        orderbook_depth: float,
        participation_rate: float = 0.01,
        volatility: float = 0.02,
        holding_period_hours: float = 4.0,
    ) -> CapacityEstimate:
        """BD-CV21: 容量模型必须对输入敏感。

        输出 point estimate + CI + limiting factor。
        """
        if daily_volume <= 0 or orderbook_depth <= 0:
            return cls(symbol=symbol, limiting_factor="UNKNOWN_INPUTS", is_stale=True)

        # Volume-based capacity: notional that can be traded at participation_rate
        volume_capacity = daily_volume * participation_rate

        # Depth-based capacity: orderbook depth * buffer
        depth_capacity = orderbook_depth * 10.0  # 10x depth buffer

        # Volatility adjustment: higher vol → lower capacity
        vol_scalar = max(0.25, min(1.0, 0.02 / max(volatility, 0.001)))

        # Holding period adjustment: longer hold → lower capacity (more exposure time)
        hold_scalar = max(0.25, min(1.0, 4.0 / max(holding_period_hours, 0.25)))

        point_estimate = min(volume_capacity, depth_capacity) * vol_scalar * hold_scalar
        ci_low = point_estimate * 0.7
        ci_high = point_estimate * 1.3

        # Determine limiting factor
        if depth_capacity < volume_capacity:
            limiting = "ORDERBOOK_DEPTH"
        elif vol_scalar < 0.5:
            limiting = "VOLATILITY"
        elif hold_scalar < 0.5:
            limiting = "HOLDING_PERIOD"
        else:
            limiting = "DAILY_VOLUME"

        return cls(
            symbol=symbol,
            max_position_notional=point_estimate,
            confidence_interval_low=ci_low,
            confidence_interval_high=ci_high,
            limiting_factor=limiting,
            daily_volume=daily_volume,
            orderbook_depth_1pct=orderbook_depth,
            participation_rate=participation_rate,
            is_stale=False,
        )

    def is_constant(self, other: CapacityEstimate, tolerance: float = 0.01) -> bool:
        """BD-CV21 AC-21-02: 容量模型对显著不同输入不能返回恒定结果。"""
        return abs(self.max_position_notional - other.max_position_notional) < tolerance


@dataclass(frozen=True)
class RealizedExecutionCost:
    """BD-CV21/45: 真实执行成本 — 只从 decision/arrival/fill facts 计算。

    predicted_cost 只能作为预测字段，禁止赋值给 realized_cost。
    无真实成交/费用时样本不可训练。
    """

    symbol: str
    decision_price: float = 0.0
    arrival_price: float = 0.0
    fill_price: float = 0.0
    commission_bps: float = 0.0
    funding_cost_bps: float = 0.0
    realized_cost_bps: float = 0.0
    predicted_cost_bps: float = 0.0  # 预测字段，不可赋值给 realized
    has_fill: bool = False
    has_fee: bool = False

    def compute_realized(self) -> float:
        """只从 decision/arrival/fill/fee facts 计算 realized cost。"""
        if not self.has_fill:
            return 0.0
        slippage = (self.fill_price - self.arrival_price) / max(self.arrival_price, 0.01) * 10000
        delay_cost = (self.arrival_price - self.decision_price) / max(self.decision_price, 0.01) * 10000
        return slippage + delay_cost + self.commission_bps + self.funding_cost_bps

    def can_learn(self) -> bool:
        """BD-CV45 AC-45-02: 无 fill/fee 的样本不会进入 learner。"""
        return self.has_fill and self.has_fee
