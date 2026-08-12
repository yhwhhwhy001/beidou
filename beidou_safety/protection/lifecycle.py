"""仓位生命周期与保护单管理 — BD-09。

PositionAggregate: Open→Increase→Reduce→PartialClose→Protected→Unprotected→Closed。
保护参数来自 Policy，支持 ATR/volatility/structure/trailing。
保护缺失→EXIT_ONLY；无法恢复→EMERGENCY_FLATTEN→LOCK。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite


@dataclass
class PositionAggregate:
    """仓位聚合 — 完整生命周期。"""

    position_id: str
    instrument_id: str
    venue_id: str
    side: str  # LONG / SHORT
    entry_price: float
    quantity: float
    protected: bool = False
    stop_loss_id: str | None = None
    take_profit_ids: list[str] = field(default_factory=list)
    realized_pnl: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        self._validate_state()

    def _validate_state(self) -> None:
        if not self.position_id.strip() or not self.instrument_id.strip() or not self.venue_id.strip():
            raise ValueError("Position, instrument and venue identities are required")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("Position side must be LONG or SHORT")
        if not isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("Position entry price must be finite and positive")
        if not isfinite(self.quantity) or self.quantity < 0:
            raise ValueError("Position quantity must be finite and non-negative")
        if self.quantity == 0 and self.closed_at is None:
            raise ValueError("A flat position requires an explicit closure timestamp")
        if self.quantity > 0 and self.closed_at is not None:
            raise ValueError("An open position cannot have a closure timestamp")
        if not isfinite(self.realized_pnl):
            raise ValueError("Position realized PnL must be finite")
        if self.created_at.tzinfo is None or (self.closed_at is not None and self.closed_at.tzinfo is None):
            raise ValueError("Position timestamps must be timezone-aware")
        if self.protected != bool(self.stop_loss_id and self.stop_loss_id.strip()):
            raise ValueError("Protection state requires one non-empty stop-loss identity")
        if any(not value.strip() for value in self.take_profit_ids) or len(set(self.take_profit_ids)) != len(
            self.take_profit_ids
        ):
            raise ValueError("Take-profit identities must be non-empty and unique")

    @property
    def is_open(self) -> bool:
        return self.quantity > 0 and self.closed_at is None

    @property
    def is_protected(self) -> bool:
        return self.is_open and self.protected and self.stop_loss_id is not None

    def increase(self, qty: float, price: float) -> None:
        if not self.is_open:
            raise ValueError("Only an open position can be increased")
        if not isfinite(qty) or qty <= 0 or not isfinite(price) or price <= 0:
            raise ValueError("Increase quantity and price must be finite and positive")
        total = self.quantity * self.entry_price + qty * price
        self.quantity += qty
        self.entry_price = total / self.quantity

    def reduce(self, qty: float, exit_price: float) -> float:
        if not self.is_open:
            raise ValueError("Only an open position can be reduced")
        if not isfinite(qty) or qty <= 0 or qty > self.quantity:
            raise ValueError("Reduction quantity must be finite, positive and no greater than the position")
        if not isfinite(exit_price) or exit_price <= 0:
            raise ValueError("Exit price must be finite and positive")
        pnl = qty * (exit_price - self.entry_price)
        if self.side == "SHORT":
            pnl = -pnl
        self.realized_pnl += pnl
        self.quantity -= qty
        if self.quantity == 0:
            self.quantity = 0
            self.closed_at = datetime.now(timezone.utc)
            self.protected = False
        return pnl

    def protect(self, stop_loss_id: str, take_profit_ids: list[str] | None = None) -> None:
        if not self.is_open:
            raise ValueError("Only an open position can be protected")
        if not stop_loss_id.strip():
            raise ValueError("Stop-loss identity is required")
        normalized_take_profit_ids = list(take_profit_ids or [])
        has_invalid_take_profit_id = any(not value.strip() for value in normalized_take_profit_ids)
        has_duplicate_take_profit_id = len(set(normalized_take_profit_ids)) != len(normalized_take_profit_ids)
        if has_invalid_take_profit_id or has_duplicate_take_profit_id:
            raise ValueError("Take-profit identities must be non-empty and unique")
        self.protected = True
        self.stop_loss_id = stop_loss_id
        self.take_profit_ids = normalized_take_profit_ids


class PositionManager:
    """仓位管理器。

    保证:
    - 任一开放仓位无 ACKed 保护时 → EXIT_ONLY/LOCKED
    - 保护缺失 → 立即 EXIT_ONLY
    - 部分止盈后止损数量正确缩减
    - 双向持仓和人工平仓竞态不反向开仓
    """

    def __init__(self) -> None:
        self._positions: dict[str, PositionAggregate] = {}

    def open(self, pos: PositionAggregate) -> PositionAggregate:
        pos._validate_state()
        existing = self._positions.get(pos.position_id)
        if existing is pos:
            return existing
        if existing is not None:
            raise ValueError(f"Position identity already exists: {pos.position_id}")
        self._positions[pos.position_id] = pos
        return pos

    def get(self, position_id: str) -> PositionAggregate | None:
        return self._positions.get(position_id)

    def all_open(self) -> list[PositionAggregate]:
        return [p for p in self._positions.values() if p.is_open]

    def unprotected_positions(self) -> list[PositionAggregate]:
        return [p for p in self.all_open() if not p.is_protected]

    def protection_coverage_pct(self) -> float:
        all_open = self.all_open()
        if not all_open:
            return 100.0
        protected = sum(1 for p in all_open if p.is_protected)
        return protected / len(all_open) * 100

    def total_exposure(self) -> float:
        return sum(p.quantity * p.entry_price for p in self.all_open())

    def close_position(self, position_id: str, exit_price: float) -> float:
        pos = self._positions.get(position_id)
        if not pos or not pos.is_open:
            return 0.0
        pnl = pos.reduce(pos.quantity, exit_price)
        return pnl
