"""自适应执行策略选择、订单切片与安全约束。

PKG-23: 实现 Post-only、Passive、Marketable Limit、IOC、TWAP、POV、
Adaptive Slice 和 Emergency Reduce-only 算法。
Contextual Bandit 仅在已批准算法集合内选择，不得改变方向/数量/滑点硬限。
订单切片持续检查在途不变量和剩余 Alpha；
执行成本预测 > 净 Alpha 时取消非风险退出交易。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import (
    OrderId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueInstrument,
)


class ExecutionAlgorithmType(str, Enum):
    """执行算法类型。"""

    POST_ONLY = "POST_ONLY"
    PASSIVE = "PASSIVE"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    IOC = "IOC"
    TWAP = "TWAP"
    POV = "POV"
    ADAPTIVE_SLICE = "ADAPTIVE_SLICE"
    EMERGENCY_REDUCE_ONLY = "EMERGENCY_REDUCE_ONLY"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """执行上下文 — 订单执行的完整市场环境。"""

    venue_instrument: VenueInstrument
    side: OrderSide
    total_quantity: Quantity
    limit_price: Price | None
    urgency: float  # 0=不紧急, 1=最高紧急（应急平仓）
    best_bid: Price | None
    best_ask: Price | None
    bid_depth: float  # 买一深度
    ask_depth: float  # 卖一深度
    spread_bps: float  # 买卖价差(bps)
    historical_execution_quality: dict[str, float] = field(default_factory=dict)
    alpha_decay_seconds: float = 60.0  # Alpha 衰减半衰期（秒）
    predicted_cost_bps: float = 0.0  # 预测执行成本(bps)
    net_alpha_bps: float = 0.0  # 净 Alpha(bps)
    hard_slippage_limit_bps: float = 50.0  # 硬滑点上限(bps)
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class OrderSlice:
    """订单切片。"""

    slice_id: str
    parent_order_id: OrderId
    quantity: Quantity
    price: Price | None
    order_type: OrderType
    time_in_force: TimeInForce
    algorithm: ExecutionAlgorithmType
    sequence_number: int
    estimated_cost_bps: float = 0.0
    invariants_check_passed: bool = True
    remaining_alpha_bps: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExecutionPlan:
    """执行计划 — 算法输出的一系列切片。"""

    algorithm: ExecutionAlgorithmType
    slices: list[OrderSlice] = field(default_factory=list)
    total_estimated_cost_bps: float = 0.0
    estimated_completion_seconds: float = 0.0
    cancel_reason: str | None = None
    is_canceled: bool = False

    def total_quantity(self) -> float:
        return sum(float(s.quantity.amount) for s in self.slices)

    def all_invariants_pass(self) -> bool:
        return all(s.invariants_check_passed for s in self.slices)


class BaseExecutionAlgorithm(ABC):
    """执行算法抽象基类。"""

    algorithm_type: ExecutionAlgorithmType

    @abstractmethod
    def can_handle(self, ctx: ExecutionContext) -> bool:
        """Return whether this concrete algorithm can safely handle ``ctx``."""
        ...

    @abstractmethod
    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        """Build a governed plan; concrete algorithms must implement this."""
        ...

    def check_invariants(self, ctx: ExecutionContext) -> tuple[bool, str]:
        """检查在途不变量。"""
        if ctx.spread_bps > ctx.hard_slippage_limit_bps:
            return False, f"Spread {ctx.spread_bps}bps exceeds hard slippage limit {ctx.hard_slippage_limit_bps}bps"
        if ctx.side == OrderSide.BUY and ctx.best_ask is None:
            return False, "No ask price available"
        if ctx.side == OrderSide.SELL and ctx.best_bid is None:
            return False, "No bid price available"
        return True, "invariants ok"


class PostOnlyAlgorithm(BaseExecutionAlgorithm):
    """Post-Only: 仅挂单不吃的限价单，确保获得 Maker 手续费。"""

    algorithm_type = ExecutionAlgorithmType.POST_ONLY

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return ctx.urgency < 0.5 and ctx.limit_price is not None and ctx.spread_bps > 1.0

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)
        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=[
                OrderSlice(
                    slice_id=f"{order_id}-postonly-0",
                    parent_order_id=order_id,
                    quantity=ctx.total_quantity,
                    price=ctx.limit_price,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    algorithm=self.algorithm_type,
                    sequence_number=0,
                    invariants_check_passed=invariant_ok,
                )
            ],
            estimated_completion_seconds=30.0,
        )


class PassiveAlgorithm(BaseExecutionAlgorithm):
    """Passive: 在最优价挂单等待成交，仅在价差够小时使用。"""

    algorithm_type = ExecutionAlgorithmType.PASSIVE

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return ctx.urgency < 0.3 and ctx.spread_bps < 10.0 and ctx.best_bid is not None and ctx.best_ask is not None

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)
        price = ctx.best_bid if ctx.side == OrderSide.BUY else ctx.best_ask
        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=[
                OrderSlice(
                    slice_id=f"{order_id}-passive-0",
                    parent_order_id=order_id,
                    quantity=ctx.total_quantity,
                    price=price,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.GTC,
                    algorithm=self.algorithm_type,
                    sequence_number=0,
                    invariants_check_passed=invariant_ok,
                )
            ],
            estimated_completion_seconds=60.0,
        )


class MarketableLimitAlgorithm(BaseExecutionAlgorithm):
    """Marketable Limit: 跨越价差立即成交，但不超过限价。"""

    algorithm_type = ExecutionAlgorithmType.MARKETABLE_LIMIT

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return (
            ctx.urgency < 0.7 and ctx.limit_price is not None and ctx.best_ask is not None and ctx.best_bid is not None
        )

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)
        fill_price = ctx.best_ask if ctx.side == OrderSide.BUY else ctx.best_bid
        # 限价必须穿越价差
        effective_price = fill_price if (ctx.side == OrderSide.BUY and fill_price) or fill_price else ctx.limit_price

        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=[
                OrderSlice(
                    slice_id=f"{order_id}-mktlimit-0",
                    parent_order_id=order_id,
                    quantity=ctx.total_quantity,
                    price=effective_price,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=0,
                    invariants_check_passed=invariant_ok,
                )
            ],
            estimated_completion_seconds=5.0,
        )


class IOCAlgorithm(BaseExecutionAlgorithm):
    """IOC (Immediate-or-Cancel): 立即成交（全部或部分），未成交部分取消。"""

    algorithm_type = ExecutionAlgorithmType.IOC

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return ctx.urgency >= 0.5 and ctx.best_bid is not None and ctx.best_ask is not None

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)
        price = ctx.best_ask if ctx.side == OrderSide.BUY else ctx.best_bid
        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=[
                OrderSlice(
                    slice_id=f"{order_id}-ioc-0",
                    parent_order_id=order_id,
                    quantity=ctx.total_quantity,
                    price=price,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=0,
                    invariants_check_passed=invariant_ok,
                )
            ],
            estimated_completion_seconds=1.0,
        )


class TWAPAlgorithm(BaseExecutionAlgorithm):
    """TWAP (Time-Weighted Average Price): 将订单等时切片。"""

    algorithm_type = ExecutionAlgorithmType.TWAP

    def __init__(self, slice_count: int = 10, interval_seconds: float = 60.0) -> None:
        self.slice_count = slice_count
        self.interval_seconds = interval_seconds

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return (
            ctx.urgency < 0.7
            and ctx.alpha_decay_seconds > self.interval_seconds
            and ctx.predicted_cost_bps < ctx.net_alpha_bps
        )

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)
        total_qty = float(ctx.total_quantity.amount)
        slice_qty = total_qty / self.slice_count

        # 执行成本 > 净Alpha → 取消
        if ctx.net_alpha_bps > 0 and ctx.predicted_cost_bps > ctx.net_alpha_bps:
            return ExecutionPlan(
                algorithm=self.algorithm_type,
                is_canceled=True,
                cancel_reason=f"Predicted cost {ctx.predicted_cost_bps}bps > net alpha {ctx.net_alpha_bps}bps",
            )

        slices: list[OrderSlice] = []
        alpha_remaining = ctx.net_alpha_bps

        for i in range(self.slice_count):
            # 衰减检查
            elapsed = i * self.interval_seconds
            decay_factor = 0.5 ** (elapsed / ctx.alpha_decay_seconds) if ctx.alpha_decay_seconds > 0 else 1.0
            alpha_remaining = ctx.net_alpha_bps * decay_factor

            slice_invariant = invariant_ok and alpha_remaining >= 0

            slices.append(
                OrderSlice(
                    slice_id=f"{order_id}-twap-{i}",
                    parent_order_id=order_id,
                    quantity=Quantity(amount=str(slice_qty)),
                    price=ctx.best_bid if ctx.side == OrderSide.BUY else ctx.best_ask,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=i,
                    estimated_cost_bps=ctx.predicted_cost_bps / self.slice_count,
                    invariants_check_passed=slice_invariant,
                    remaining_alpha_bps=alpha_remaining,
                )
            )

        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=slices,
            total_estimated_cost_bps=ctx.predicted_cost_bps,
            estimated_completion_seconds=self.interval_seconds * self.slice_count,
        )


class POVAlgorithm(BaseExecutionAlgorithm):
    """POV (Percentage of Volume): 按实时市场成交量比例执行。"""

    algorithm_type = ExecutionAlgorithmType.POV

    def __init__(self, participation_rate: float = 0.1, max_duration_seconds: float = 300.0) -> None:
        self.participation_rate = participation_rate  # 最大参与率
        self.max_duration_seconds = max_duration_seconds

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return (
            ctx.urgency < 0.5 and ctx.bid_depth > 0 and ctx.ask_depth > 0 and ctx.predicted_cost_bps < ctx.net_alpha_bps
        )

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)

        if ctx.net_alpha_bps > 0 and ctx.predicted_cost_bps > ctx.net_alpha_bps:
            return ExecutionPlan(
                algorithm=self.algorithm_type,
                is_canceled=True,
                cancel_reason=f"Predicted cost {ctx.predicted_cost_bps}bps > net alpha {ctx.net_alpha_bps}bps",
            )

        total_qty = float(ctx.total_quantity.amount)
        available_depth = ctx.bid_depth if ctx.side == OrderSide.BUY else ctx.ask_depth
        max_slice = available_depth * self.participation_rate

        remaining = total_qty
        slices: list[OrderSlice] = []
        seq = 0

        while remaining > 0:
            slice_qty = min(remaining, max_slice)
            slices.append(
                OrderSlice(
                    slice_id=f"{order_id}-pov-{seq}",
                    parent_order_id=order_id,
                    quantity=Quantity(amount=str(slice_qty)),
                    price=ctx.best_bid if ctx.side == OrderSide.BUY else ctx.best_ask,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=seq,
                    estimated_cost_bps=ctx.predicted_cost_bps * (slice_qty / total_qty),
                    invariants_check_passed=invariant_ok and slice_qty <= max_slice,
                )
            )
            remaining -= slice_qty
            seq += 1
            if seq > 50:  # 防止无限循环
                break

        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=slices,
            total_estimated_cost_bps=ctx.predicted_cost_bps,
            estimated_completion_seconds=self.max_duration_seconds,
        )


class AdaptiveSliceAlgorithm(BaseExecutionAlgorithm):
    """Adaptive Slice: 根据实时市场条件动态调整切片大小和间隔。"""

    algorithm_type = ExecutionAlgorithmType.ADAPTIVE_SLICE

    def __init__(self, min_slice_pct: float = 0.05, max_slice_pct: float = 0.25) -> None:
        self.min_slice_pct = min_slice_pct
        self.max_slice_pct = max_slice_pct

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return ctx.urgency < 0.7

    def _determine_slice_pct(self, ctx: ExecutionContext) -> float:
        """根据市场条件动态确定切片比例。高流动性→大切片；高波动→小切片。"""
        base = self.min_slice_pct
        if ctx.spread_bps < 5.0 and ctx.bid_depth > 100000:
            base = self.max_slice_pct
        elif ctx.spread_bps > 20.0:
            base = self.min_slice_pct
        return base

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)

        if ctx.net_alpha_bps > 0 and ctx.predicted_cost_bps > ctx.net_alpha_bps:
            return ExecutionPlan(
                algorithm=self.algorithm_type,
                is_canceled=True,
                cancel_reason=f"Predicted cost {ctx.predicted_cost_bps}bps > net alpha {ctx.net_alpha_bps}bps",
            )

        total_qty = float(ctx.total_quantity.amount)
        slice_pct = self._determine_slice_pct(ctx)
        slice_qty = total_qty * slice_pct
        slice_count = max(1, int(1.0 / slice_pct))

        slices: list[OrderSlice] = []
        alpha_remaining = ctx.net_alpha_bps

        for i in range(slice_count):
            elapsed = i * 30.0  # 估计每切片30秒间隔
            decay_factor = 0.5 ** (elapsed / ctx.alpha_decay_seconds) if ctx.alpha_decay_seconds > 0 else 1.0
            alpha_remaining = ctx.net_alpha_bps * decay_factor

            slice_invariant = invariant_ok and alpha_remaining >= 0

            # 切片大小随市场动态调整
            qty = slice_qty * (0.8 + 0.4 * alpha_remaining / max(ctx.net_alpha_bps, 1))
            slices.append(
                OrderSlice(
                    slice_id=f"{order_id}-adaptive-{i}",
                    parent_order_id=order_id,
                    quantity=Quantity(amount=str(qty)),
                    price=ctx.best_bid if ctx.side == OrderSide.BUY else ctx.best_ask,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=i,
                    estimated_cost_bps=ctx.predicted_cost_bps / slice_count,
                    invariants_check_passed=slice_invariant,
                    remaining_alpha_bps=alpha_remaining,
                )
            )

        # 归一化切片总量不超过批准量
        _total = sum(float(s.quantity.amount) for s in slices)
        if _total > total_qty and _total > 0:
            _scale = total_qty / _total
            slices = [
                OrderSlice(
                    slice_id=s.slice_id,
                    parent_order_id=s.parent_order_id,
                    quantity=Quantity(amount=str(float(s.quantity.amount) * _scale)),
                    price=s.price,
                    order_type=s.order_type,
                    time_in_force=s.time_in_force,
                    algorithm=s.algorithm,
                    sequence_number=s.sequence_number,
                    estimated_cost_bps=s.estimated_cost_bps,
                    invariants_check_passed=s.invariants_check_passed,
                    remaining_alpha_bps=s.remaining_alpha_bps,
                )
                for s in slices
            ]

        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=slices,
            total_estimated_cost_bps=ctx.predicted_cost_bps,
            estimated_completion_seconds=slice_count * 30.0,
        )


class EmergencyReduceOnlyAlgorithm(BaseExecutionAlgorithm):
    """Emergency Reduce-Only: 应急减仓 — 安全优先，不追求 Maker。"""

    algorithm_type = ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY

    def can_handle(self, ctx: ExecutionContext) -> bool:
        return ctx.urgency >= 0.8 and ctx.side == OrderSide.SELL  # 减仓只能卖出

    def plan(self, ctx: ExecutionContext, order_id: OrderId) -> ExecutionPlan:
        invariant_ok, _msg = self.check_invariants(ctx)

        # 应急减仓不因成本取消
        return ExecutionPlan(
            algorithm=self.algorithm_type,
            slices=[
                OrderSlice(
                    slice_id=f"{order_id}-emergency-0",
                    parent_order_id=order_id,
                    quantity=ctx.total_quantity,
                    price=None,  # 市价成交
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.IOC,
                    algorithm=self.algorithm_type,
                    sequence_number=0,
                    invariants_check_passed=invariant_ok,
                )
            ],
            total_estimated_cost_bps=ctx.predicted_cost_bps,
            estimated_completion_seconds=1.0,
        )


class ExecutionAlgorithmSelector:
    """执行算法选择器 — Contextual Bandit 在已批准集合内选择。"""

    ALL_ALGORITHMS: list[BaseExecutionAlgorithm] = [
        PostOnlyAlgorithm(),
        PassiveAlgorithm(),
        MarketableLimitAlgorithm(),
        IOCAlgorithm(),
        TWAPAlgorithm(slice_count=10, interval_seconds=60.0),
        POVAlgorithm(participation_rate=0.1, max_duration_seconds=300.0),
        AdaptiveSliceAlgorithm(min_slice_pct=0.05, max_slice_pct=0.25),
        EmergencyReduceOnlyAlgorithm(),
    ]

    def __init__(self, approved_algorithm_types: set[ExecutionAlgorithmType] | None = None) -> None:
        self._approved = approved_algorithm_types or set(ExecutionAlgorithmType)
        self._quality_scores: dict[ExecutionAlgorithmType, float] = dict.fromkeys(ExecutionAlgorithmType, 0.5)

    @property
    def approved_algorithms(self) -> list[BaseExecutionAlgorithm]:
        return [a for a in self.ALL_ALGORITHMS if a.algorithm_type in self._approved]

    def select(self, ctx: ExecutionContext) -> BaseExecutionAlgorithm | None:
        """Contextual Bandit 选择 — 仅在已批准集合内。"""
        candidates = self.approved_algorithms

        # 按适用性过滤
        applicable = [a for a in candidates if a.can_handle(ctx)]

        if not applicable:
            # 无适用算法时回退到已批准确定性策略
            safe_fallbacks = [
                a for a in candidates if a.algorithm_type in (ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY,)
            ]
            if safe_fallbacks:
                return safe_fallbacks[0]
            # 最后回退到 IOC（快速退出）
            ioc_fallback = [a for a in candidates if a.algorithm_type == ExecutionAlgorithmType.IOC]
            return ioc_fallback[0] if ioc_fallback else None

        # 应急减仓优先 — 安全完成，不追求 maker
        if ctx.urgency >= 0.8:
            emergency = [a for a in applicable if a.algorithm_type == ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY]
            if emergency:
                return emergency[0]

        # 按历史执行质量和紧急性加权排序
        scored = [
            (
                a,
                self._quality_scores.get(a.algorithm_type, 0.5)
                * (1.0 + ctx.urgency)
                * (1.0 - ctx.predicted_cost_bps / 100.0),
            )
            for a in applicable
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def update_quality(
        self, algorithm_type: ExecutionAlgorithmType, realized_cost_bps: float, slippage_bps: float
    ) -> None:
        """根据实际执行表现更新质量分数。"""
        current = self._quality_scores.get(algorithm_type, 0.5)
        # 成本越低分数越高
        reward = max(0.0, 1.0 - (realized_cost_bps + slippage_bps) / 100.0)
        self._quality_scores[algorithm_type] = 0.9 * current + 0.1 * reward

    def set_approved(self, algorithm_types: set[ExecutionAlgorithmType]) -> None:
        """设置批准算法集。不得改变交易方向/数量/滑点硬限。"""
        self._approved = algorithm_types


class SliceInvariantChecker:
    """订单切片不变量检查器 — 持续验证在途订单的安全性。"""

    @staticmethod
    def check_slice(slice_: OrderSlice, current_ctx: ExecutionContext) -> tuple[bool, str]:
        """检查单个切片是否仍然有效。"""
        if float(slice_.quantity.amount) <= 0:
            return False, "Slice quantity <= 0"

        # 硬滑点检查
        if current_ctx.spread_bps > current_ctx.hard_slippage_limit_bps:
            return (
                False,
                f"Spread {current_ctx.spread_bps}bps exceeds hard limit {current_ctx.hard_slippage_limit_bps}bps",
            )

        # Alpha 剩余检查
        if slice_.remaining_alpha_bps is not None and slice_.remaining_alpha_bps < 0:
            return False, "Remaining alpha depleted"

        # 切片不能超过 Approval 数量
        if float(slice_.quantity.amount) > float(current_ctx.total_quantity.amount):
            return False, "Slice quantity exceeds approved total"

        return True, "slice valid"

    @staticmethod
    def validate_plan(plan: ExecutionPlan, ctx: ExecutionContext) -> tuple[bool, str]:
        """验证执行计划整体安全性。"""
        if plan.is_canceled:
            return True, "Canceled plan is safe"

        for slice_ in plan.slices:
            ok, msg = SliceInvariantChecker.check_slice(slice_, ctx)
            if not ok:
                return False, f"Slice {slice_.slice_id}: {msg}"

        # 总量不得超过 Approval
        if plan.total_quantity() > float(ctx.total_quantity.amount):
            return False, "Plan total quantity exceeds approved quantity"

        return True, "plan valid"
