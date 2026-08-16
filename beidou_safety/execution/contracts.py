"""BD-CV40/41/42/43/44/45: Wave 4 Execution & Protection 合约.

执行计划、订单幂等、仓位聚合、保护、复式账本、执行成本。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

# ============================================================================
# BD-CV40: ExecutionPlan / PlanSlice
# ============================================================================


class PlanStatus(str, Enum):
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PlanSlice:
    """BD-CV40: 单个执行切片。"""

    slice_id: str
    symbol: str
    quantity: str
    price: str
    order_type: str
    algorithm: str


@dataclass(frozen=True)
class ExecutionPlan:
    """BD-CV40: 完整执行计划。

    无算法适用返回 NOT_EXECUTABLE。
    Emergency plan 绝不增加绝对仓位。
    """

    plan_id: str = ""
    slices: list[PlanSlice] = field(default_factory=list)
    status: PlanStatus = PlanStatus.NOT_EXECUTABLE
    algorithm: str = ""
    is_emergency: bool = False
    plan_hash: str = ""

    def is_executable(self) -> bool:
        return self.status != PlanStatus.NOT_EXECUTABLE and len(self.slices) > 0

    def never_increases_absolute_position(self, current_position: float) -> bool:
        """Emergency plan 验证：绝不增加绝对仓位。"""
        if not self.is_emergency:
            return True
        total_qty = sum(abs(float(s.quantity)) for s in self.slices)
        return total_qty <= abs(current_position)


# ============================================================================
# BD-CV41: 订单写入、幂等、UNKNOWN
# ============================================================================


@dataclass(frozen=True)
class OrderIdempotencyKey:
    """BD-CV41: 订单幂等键。

    timeout 后不会产生重复订单。
    kill -9/restart 后 outbox 可继续恢复且不丢幂等状态。
    """

    correlation_id: str
    client_order_id: str
    outbox_id: str

    def compute_hash(self) -> str:
        return hashlib.sha256(f"{self.correlation_id}:{self.client_order_id}:{self.outbox_id}".encode()).hexdigest()


@dataclass(frozen=True)
class OrderWriteReceipt:
    """BD-CV41: 订单写入回执。"""

    order_id: str
    idempotency_key: OrderIdempotencyKey
    status: str  # ACK, REJECTED, UNKNOWN, DUPLICATE
    venue_order_id: str = ""


# ============================================================================
# BD-CV42: Fill/Position Aggregate
# ============================================================================


@dataclass(frozen=True)
class Fill:
    """BD-CV42: 单笔成交。"""

    fill_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    commission: float = 0.0


@dataclass(frozen=True)
class PositionAggregate:
    """BD-CV42: 仓位聚合。

    任意成交序列 replay 最终 PositionAggregate deterministic。
    reduce-only 永不跨零反向开仓。
    """

    symbol: str
    net_position: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    is_reduce_only_compliant: bool = True

    def replay(self, fills: list[Fill]) -> PositionAggregate:
        """确定性重放成交序列。

        reduce-only 永不跨零反向开仓：从空仓建仓（同方向加仓/减仓）
        不受限；仅当成交会使净仓位**穿越零点反向**（多头 SELL 超量、
        空头 BUY 超量）时跳过该成交并置 ``is_reduce_only_compliant=False``。
        M00-F04：原实现对 SELL 侧仅 ``pass`` 空操作（成交仍被应用），
        连跨零也未拦截，且缺失 BUY 侧对称防护，一并修复。逐笔独立判定；
        ``is_reduce_only_compliant=False`` 的聚合（历史事实）不再拦截。
        """
        net = self.net_position
        total_cost = self.avg_entry_price * abs(self.net_position) if self.net_position != 0 else 0.0
        skipped_any = False
        for f in fills:
            if self.is_reduce_only_compliant and (
                (f.side == "SELL" and net > 0 and f.quantity > net)
                or (f.side == "BUY" and net < 0 and f.quantity > abs(net))
            ):
                skipped_any = True
                continue
            net += f.quantity if f.side == "BUY" else -f.quantity
            total_cost += f.price * abs(f.quantity) + f.commission
        return PositionAggregate(
            symbol=self.symbol,
            net_position=net,
            avg_entry_price=total_cost / abs(net) if net != 0 else 0.0,
            fills=self.fills + fills,
            is_reduce_only_compliant=self.is_reduce_only_compliant and not skipped_any,
        )


# ============================================================================
# BD-CV43: ProtectionAggregate
# ============================================================================


@dataclass(frozen=True)
class ProtectionOrder:
    """BD-CV43: 保护单。"""

    order_id: str
    symbol: str
    stop_loss_price: float
    take_profit_price: float = 0.0
    is_active: bool = False


@dataclass(frozen=True)
class ProtectionAggregate:
    """BD-CV43: 仓位级保护聚合。

    任一 nonzero position 的有效 SL 覆盖率=100%。
    多 symbol 精度均符合交易所规则。
    """

    symbol: str
    position_qty: float = 0.0
    stop_loss: ProtectionOrder | None = None
    take_profit: ProtectionOrder | None = None
    coverage_pct: float = 0.0

    def has_full_coverage(self) -> bool:
        """Nonzero position 必须有有效 SL 且覆盖 100%。"""
        if self.position_qty == 0.0:
            return True
        return self.stop_loss is not None and self.stop_loss.is_active and self.coverage_pct >= 100.0


# ============================================================================
# BD-CV44: 复式账本、三方对账
# ============================================================================


class LegType(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


@dataclass(frozen=True)
class LedgerPosting:
    """BD-CV44: 复式记账分录。"""

    account: str
    leg_type: LegType
    amount: float
    currency: str = "USDT"


@dataclass(frozen=True)
class LedgerTransaction:
    """BD-CV44: 账本交易。

    每个 transaction postings 借贷守恒。
    """

    tx_id: str
    postings: list[LedgerPosting] = field(default_factory=list)

    def is_balanced(self) -> bool:
        """借贷守恒验证。"""
        debits = sum(p.amount for p in self.postings if p.leg_type == LegType.DEBIT)
        credits = sum(p.amount for p in self.postings if p.leg_type == LegType.CREDIT)
        return abs(debits - credits) < 0.0001


@dataclass(frozen=True)
class TripleReconciliation:
    """BD-CV44: 三方对账结果。

    三方同源伪造测试必须被检测。
    """

    venue_orders: int = 0
    venue_positions: int = 0
    local_orders: int = 0
    local_positions: int = 0
    ledger_entries: int = 0
    is_matched: bool = False
    mismatches: list[str] = field(default_factory=list)

    def detect_same_source_fraud(self, sources: list[str]) -> bool:
        """三方同源伪造检测。"""
        unique_sources = set(sources)
        return len(unique_sources) >= 3  # 需要三个独立来源


# ============================================================================
# BD-CV45: Realized Execution Cost
# ============================================================================


@dataclass(frozen=True)
class ExecutionCostSnapshot:
    """BD-CV45: 执行成本快照。

    搜索不到 predicted→realized 直接赋值路径。
    无 fill/fee 的样本不会进入 learner。
    """

    symbol: str
    predicted_cost_bps: float = 0.0
    realized_cost_bps: float = 0.0
    has_fill: bool = False
    has_fee: bool = False

    def can_learn(self) -> bool:
        """只有真实 fill + fee 的样本可以进入 learner。"""
        return self.has_fill and self.has_fee
