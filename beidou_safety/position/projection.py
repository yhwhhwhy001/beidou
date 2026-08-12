"""BD-T10: Fill 与 Position 权威链 — 不可变事件溯源。

FillEvent 是仓位的唯一输入源。PositionAggregate 从 fills 计算仓位。
PositionProjection 可从 fills 完全重建，支持确定性重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite

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
        _validate_fill(fill)
        if fill.instrument_id != self.instrument_id:
            raise ValueError(f"Fill instrument does not match aggregate: {fill.instrument_id}")
        if fill.venue_id != self.venue_id:
            raise ValueError(f"Fill venue does not match aggregate: {fill.venue_id}")
        if fill.sequence <= self.fill_sequence:
            raise ValueError("Fill sequence must be strictly increasing")
        if (
            not isfinite(self.quantity)
            or not isfinite(self.avg_entry_price)
            or not isfinite(self.realized_pnl)
            or not isfinite(self.total_fees)
            or self.quantity < 0
            or self.avg_entry_price < 0
            or (self.quantity == 0 and self.side is not PositionSide.FLAT)
            or (self.quantity > 0 and self.side is PositionSide.FLAT)
        ):
            raise ValueError("Position aggregate contains invalid economic state")
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
        return isfinite(reduce_qty) and reduce_qty > 0 and reduce_qty <= self.quantity

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
        self._seen_keys: dict[str, FillEvent] = {}
        self._seen_fill_ids: dict[str, FillEvent] = {}
        self._seen_sequences: dict[int, FillEvent] = {}

    def apply(self, fill: FillEvent) -> bool:
        """应用成交 — 幂等，拒绝重复 trade_id。"""
        _validate_fill(fill)
        key = fill.idempotency_key()
        existing = self._seen_keys.get(key)
        if existing is not None:
            if existing == fill:
                return False
            raise ValueError(f"Conflicting trade identity: {key}")
        existing = self._seen_fill_ids.get(fill.fill_id)
        if existing is not None:
            raise ValueError(f"Conflicting fill identity: {fill.fill_id}")
        existing = self._seen_sequences.get(fill.sequence)
        if existing is not None:
            raise ValueError(f"Conflicting fill sequence: {fill.sequence}")

        self._seen_keys[key] = fill
        self._seen_fill_ids[fill.fill_id] = fill
        self._seen_sequences[fill.sequence] = fill
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
        for sym in sorted(all_symbols):
            position = rebuilt.get(sym)
            sys_qty = 0.0
            if position is not None:
                sys_qty = -position.quantity if position.side is PositionSide.SHORT else position.quantity
            raw_ex_qty = exchange_positions.get(sym, 0.0)
            try:
                ex_qty = float(raw_ex_qty)
            except (TypeError, ValueError):
                diffs.append(f"{sym}: invalid exchange quantity")
                continue
            if not isfinite(ex_qty):
                diffs.append(f"{sym}: invalid exchange quantity")
                continue
            if abs(sys_qty - ex_qty) > 1e-8:
                diffs.append(f"{sym}: system={sys_qty} exchange={raw_ex_qty}")
        return len(diffs) == 0, diffs

    @property
    def fill_count(self) -> int:
        return len(self._fills)


def _validate_fill(fill: FillEvent) -> None:
    """Reject incomplete or non-economic fill facts before projection mutation."""

    if not fill.fill_id.strip() or not fill.trade_id.strip():
        raise ValueError("Fill and trade identities are required")
    if not str(fill.instrument_id).strip() or not str(fill.venue_id).strip():
        raise ValueError("Fill instrument and venue identities are required")
    if fill.side not in {PositionSide.LONG, PositionSide.SHORT}:
        raise ValueError("Fill side must be LONG or SHORT")
    if fill.sequence <= 0:
        raise ValueError("Fill sequence must be positive")
    if fill.timestamp.tzinfo is None:
        raise ValueError("Fill timestamp must be timezone-aware")

    try:
        quantity = float(fill.quantity.amount)
        price = float(fill.price.amount)
        fee = float(fill.fee.amount)
        realized_pnl = None if fill.realized_pnl is None else float(fill.realized_pnl.amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fill contains invalid numeric facts") from exc
    if not isfinite(quantity) or quantity <= 0 or not isfinite(price) or price <= 0:
        raise ValueError("Fill quantity and price must be finite and positive")
    if not isfinite(fee) or fee < 0:
        raise ValueError("Fill fee must be finite and non-negative")
    if realized_pnl is not None and not isfinite(realized_pnl):
        raise ValueError("Fill realized PnL must be finite")
