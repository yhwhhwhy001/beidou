"""BD-CV42/43: PositionAggregate + ProtectionAggregate 引擎。

任意成交序列 replay 确定性。
reduce-only 永不跨零反向开仓。
Nonzero position 必须有 venue-acknowledged SL 100% 覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True)
class FillEvent:
    """BD-CV42: 不可变成交事件。"""

    fill_id: str
    symbol: str
    side: str  # BUY/SELL
    quantity: float
    price: float
    commission: float = 0.0
    timestamp: float = 0.0


@dataclass
class PositionAggregate:
    """BD-CV42: 仓位聚合 — 仅由 FillEvent 更新。

    本地 position 只能由 FillEvent 更新。
    exchange position 与 local 差异 → reconciliation。
    """

    symbol: str = ""
    net_position: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fills: list[FillEvent] = field(default_factory=list)
    version: int = 0

    def apply_fill(self, fill: FillEvent) -> PositionAggregate:
        """BD-CV42: 应用单笔成交（原始事实入账，不做跨零改写）。

        注意: 本类不做 reduce-only 跨零防护（成交事实必须完整入账，
        不得丢弃）；契约版 PositionAggregate.replay 的跨零防护见
        beidou_safety/execution/contracts.py（M00-F04）。仓位级
        reduce-only 校验策略由 M12 评估。
        """
        if not fill.fill_id.strip():
            raise ValueError("Fill identity is required")
        if not fill.symbol.strip():
            raise ValueError("Fill symbol is required")
        if fill.symbol != self.symbol and self.symbol:
            raise ValueError(f"Symbol mismatch: {fill.symbol} != {self.symbol}")
        if fill.side not in {"BUY", "SELL"}:
            raise ValueError(f"Invalid fill side: {fill.side}")
        if not isfinite(fill.quantity) or not isfinite(fill.price) or fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("Fill quantity and price must be positive")
        if not isfinite(fill.commission) or fill.commission < 0:
            raise ValueError("Fill commission must be finite and non-negative")
        if not isfinite(fill.timestamp):
            raise ValueError("Fill timestamp must be finite")
        if (
            not isfinite(self.net_position)
            or not isfinite(self.avg_entry_price)
            or not isfinite(self.realized_pnl)
            or self.avg_entry_price < 0
        ):
            raise ValueError("Position state must contain finite economic facts")

        for existing in self.fills:
            if existing.fill_id != fill.fill_id:
                continue
            if existing == fill:
                return self
            raise ValueError(f"Conflicting fill identity: {fill.fill_id}")

        # Reduce-only guard: 不跨零反向开仓
        new_position = self.net_position
        if fill.side == "BUY":
            new_position += fill.quantity
        else:
            new_position -= fill.quantity

        # Update avg entry
        same_direction = (self.net_position > 0 and fill.side == "BUY") or (
            self.net_position < 0 and fill.side == "SELL"
        )
        if self.net_position == 0:
            new_avg = fill.price
        elif same_direction:
            total_cost = self.avg_entry_price * abs(self.net_position) + fill.price * abs(fill.quantity)
            new_avg = total_cost / abs(new_position)
        elif new_position == 0:
            new_avg = 0.0
        elif (self.net_position > 0) == (new_position > 0):
            new_avg = self.avg_entry_price
        else:
            new_avg = fill.price

        # Realized PnL
        new_realized = self.realized_pnl
        if self.net_position > 0 and fill.side == "SELL":
            new_realized += (fill.price - self.avg_entry_price) * min(abs(fill.quantity), abs(self.net_position))
        elif self.net_position < 0 and fill.side == "BUY":
            new_realized += (self.avg_entry_price - fill.price) * min(abs(fill.quantity), abs(self.net_position))

        return PositionAggregate(
            symbol=fill.symbol or self.symbol,
            net_position=new_position,
            avg_entry_price=new_avg,
            realized_pnl=new_realized,
            fills=[*self.fills, fill],
            version=self.version + 1,
        )

    def replay(self, fills: list[FillEvent]) -> PositionAggregate:
        """BD-CV42 AC-42-01: 任意成交序列 replay 确定性。"""
        current = self
        for f in fills:
            current = current.apply_fill(f)
        return current

    def is_reduce_only_compliant(self, new_fill: FillEvent) -> bool:
        """BD-CV42 AC-42-02: reduce-only 永不跨零反向开仓。"""
        if (
            not new_fill.symbol.strip()
            or (self.symbol and new_fill.symbol != self.symbol)
            or not isfinite(new_fill.quantity)
            or new_fill.quantity <= 0
        ):
            return False
        if new_fill.side == "SELL":
            return self.net_position > 0 and new_fill.quantity <= self.net_position
        if new_fill.side == "BUY":
            return self.net_position < 0 and new_fill.quantity <= abs(self.net_position)
        return False


@dataclass
class ProtectionOrder:
    """BD-CV43: 保护单。"""

    order_id: str
    symbol: str
    position_key: str
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    kind: str = "SL"  # SL/TP
    status: str = "UNKNOWN"  # ACTIVE/PENDING/UNKNOWN
    venue_order_id: str = ""
    is_active: bool = False
    rule_snapshot_id: str = ""


@dataclass
class ProtectionAggregate:
    """BD-CV43: 仓位级保护聚合。

    precision/step/tick 来自 InstrumentRuleSnapshot。
    SL replacement: create-new→ACK→cancel-old。
    """

    symbol: str = ""
    position_qty: float = 0.0
    stop_loss: ProtectionOrder | None = None
    take_profit: ProtectionOrder | None = None
    covered_qty: float = 0.0
    coverage_pct: float = 0.0
    rule_snapshot_id: str = ""

    def has_full_coverage(self) -> bool:
        """BD-CV43 AC-43-01: nonzero position 有效 SL 覆盖率=100%。"""
        if self.position_qty == 0.0:
            return True
        return self.compute_coverage() >= 100.0

    def compute_coverage(self, rule_step_size: float = 0.001) -> float:
        """BD-CV43: 基于 InstrumentRuleSnapshot 的精度计算覆盖率。"""
        if self.position_qty == 0.0:
            return 100.0
        if self.stop_loss is None or not self.stop_loss.is_active:
            return 0.0
        required = abs(self.position_qty)
        effective_covered = min(abs(self.covered_qty), required)
        if required - effective_covered <= max(0.0, rule_step_size):
            effective_covered = required
        self.coverage_pct = (effective_covered / required) * 100.0
        return self.coverage_pct

    def replace_sl_atomic(self, new_sl: ProtectionOrder, rule_snapshot_id: str = "") -> str:
        """BD-CV43 AC-43-03: create-new→ACK→cancel-old 无保护空窗。"""
        venue_acknowledged = new_sl.status == "ACTIVE" and new_sl.is_active and bool(new_sl.venue_order_id)
        if not venue_acknowledged:
            return "WAIT_FOR_ACK"
        if self.stop_loss is not None and self.stop_loss.is_active:
            old_id = self.stop_loss.venue_order_id
            self.stop_loss = new_sl
            new_sl.rule_snapshot_id = rule_snapshot_id
            return f"CANCEL:{old_id}"
        self.stop_loss = new_sl
        new_sl.rule_snapshot_id = rule_snapshot_id
        return "NEW_SL"
