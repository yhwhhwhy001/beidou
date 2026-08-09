"""BD-T10: Fill 与 Position 权威链 — 不可变事件溯源。

FillEvent 是仓位的唯一输入源。PositionAggregate 从 fills 计算仓位。
PositionProjection 可从 fills 完全重建，支持确定性重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import (
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    Price,
    Quantity,
    VenueId,
)


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True, slots=True)
class FillEvent:
    """BD-T10: 不可变成交事件 — 仓位的唯一输入源。

    trade_id + venue 组成全局唯一幂等键。
    """

    fill_id: str
    trade_id: str
    venue_id: VenueId
    instrument_id: InstrumentId
    side: PositionSide
    quantity: Quantity
    price: Price
    fee: MonetaryValue = field(default_factory=lambda: MonetaryValue(amount="0"))
    realized_pnl: MonetaryValue | None = None
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0

    def idempotency_key(self) -> str:
        return f"{self.trade_id}:{self.venue_id}"


@dataclass
class PositionAggregate:
    """BD-T10: 仓位聚合 — 从 FillEvent 序列计算。

    支持 add/reduce/flatten/reverse/partial/fees/realized PnL。
    reduce-only/close-position 绝不允许增加绝对风险。
    """

    instrument_id: InstrumentId
    venue_id: VenueId
    side: PositionSide = PositionSide.FLAT
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    fill_sequence: int = 0

    def apply_fill(self, fill: FillEvent) -> None:
        """应用成交事件更新仓位。"""
        qty = float(fill.quantity.amount)
        price = float(fill.price.amount)
        fee = float(fill.fee.amount)

        if self.quantity == 0:
            # 新开仓
            self.side = PositionSide.LONG if fill.side == PositionSide.LONG else PositionSide.SHORT
            self.quantity = qty
            self.avg_entry_price = price
        elif fill.side == self.side:
            # 加仓
            total_cost = self.quantity * self.avg_entry_price + qty * price
            self.quantity += qty
            self.avg_entry_price = total_cost / self.quantity if self.quantity > 0 else 0
        else:
            # 减仓/平仓
            if qty >= self.quantity:
                # 完全平仓或反向
                pnl = self.quantity * (price - self.avg_entry_price)
                if self.side == PositionSide.SHORT:
                    pnl = -pnl
                self.realized_pnl += pnl
                self.quantity = max(0, qty - self.quantity)
                if self.quantity > 0:
                    self.side = fill.side
                    self.avg_entry_price = price
                else:
                    self.side = PositionSide.FLAT
                    self.avg_entry_price = 0
            else:
                # 部分减仓
                pnl = qty * (price - self.avg_entry_price)
                if self.side == PositionSide.SHORT:
                    pnl = -pnl
                self.realized_pnl += pnl
                self.quantity -= qty

        self.total_fees += fee
        self.fill_sequence = fill.sequence

    def is_reduce_only_safe(self, reduce_qty: float) -> bool:
        """BD-T10: reduce-only 绝不允许增加绝对风险。"""
        return reduce_qty <= self.quantity

    @property
    def notional(self) -> float:
        return self.quantity * self.avg_entry_price if self.quantity > 0 else 0.0

    @property
    def unrealized_pnl(self) -> float:
        return 0.0  # 需要当前市价计算；由调用方提供


class PositionProjection:
    """BD-T10: 仓位投影 — 可从 fills 完全重建。

    崩溃恢复: replay fills → rebuild position → reconcile against exchange。
    """

    def __init__(self) -> None:
        self._fills: list[FillEvent] = []
        self._seen_keys: set[str] = set()

    def apply(self, fill: FillEvent) -> bool:
        """应用成交 — 幂等，拒绝重复 trade_id。"""
        key = fill.idempotency_key()
        if key in self._seen_keys:
            return False
        self._seen_keys.add(key)
        self._fills.append(fill)
        return True

    def rebuild(self) -> dict[str, PositionAggregate]:
        """从 fills 重建所有仓位。"""
        positions: dict[str, PositionAggregate] = {}
        for fill in sorted(self._fills, key=lambda f: f.sequence):
            key = f"{fill.instrument_id}:{fill.venue_id}"
            if key not in positions:
                positions[key] = PositionAggregate(
                    instrument_id=fill.instrument_id,
                    venue_id=fill.venue_id,
                )
            positions[key].apply_fill(fill)
        return positions

    def reconcile_against(self, exchange_positions: dict[str, float]) -> tuple[bool, list[str]]:
        """对账系统重建仓位与交易所仓位。"""
        rebuilt = self.rebuild()
        diffs: list[str] = []
        all_symbols = set(rebuilt.keys()) | set(exchange_positions.keys())
        for sym in all_symbols:
            sys_qty = rebuilt[sym].quantity if sym in rebuilt else 0.0
            ex_qty = exchange_positions.get(sym, 0.0)
            if abs(sys_qty - ex_qty) > 1e-8:
                diffs.append(f"{sym}: system={sys_qty} exchange={ex_qty}")
        return len(diffs) == 0, diffs

    @property
    def fill_count(self) -> int:
        return len(self._fills)
