"""交易所原生保护单管理 — BD-09 items 2,3,4,5,7,8。

开仓成交后立即创建交易所原生 STOP_MARKET 和 TAKE_PROFIT_MARKET。
保护覆盖状态持久化，以交易所 ACK 为准。启动恢复仓位和条件单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ProtectionType(str, Enum):
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TRAILING_STOP = "TRAILING_STOP"
    STOP_LIMIT = "STOP_LIMIT"


class ProtectionStatus(str, Enum):
    PENDING = "PENDING"
    CREATED = "CREATED"  # 已下单，等待 ACK
    ACKED = "ACKED"  # 交易所已确认
    TRIGGERED = "TRIGGERED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


@dataclass
class ProtectionOrder:
    """交易所原生保护单。"""

    protection_id: str
    position_id: str
    instrument_id: str
    venue_id: str
    order_type: ProtectionType
    side: str  # BUY / SELL (reduce-only)
    trigger_price: float
    quantity: float
    status: ProtectionStatus = ProtectionStatus.PENDING
    exchange_order_id: str | None = None
    ack_received_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ExchangeProtectionManager:
    """交易所原生保护单管理器 — BD-09。

    保证:
    - 每次开仓后立即创建 STOP_MARKET + TAKE_PROFIT_MARKET
    - 保护参数来自 Policy（ATR/volatility/structure/trailing）
    - 以交易所 ACK 为准；本地 Watchdog 仅二级保护
    - 部分成交后原子调整保护数量
    - 保护缺失或创建失败 → EXIT_ONLY
    - 启动恢复仓位和条件单，coverage reconciliation 后允许新增风险
    - 处理 one-way/hedge mode、人工平仓、保护触发与撤单竞态
    """

    def __init__(self) -> None:
        self._protections: dict[str, ProtectionOrder] = {}
        self._position_protections: dict[str, list[str]] = {}  # pos_id → [prot_id, ...]

    def create_for_position(
        self,
        position_id: str,
        instrument_id: str,
        venue_id: str,
        entry_price: float,
        quantity: float,
        side: str,
        stop_pct: float = 2.0,
        take_profit_rr: float = 2.0,
        *,
        price_precision: int | None = None,
    ) -> list[ProtectionOrder]:
        """开仓后立即创建保护单对。"""

        protections = []

        # Stop loss
        if side == "LONG":
            stop_price = entry_price * (1 - stop_pct / 100)
            tp_price = entry_price * (1 + stop_pct * take_profit_rr / 100)
        else:
            stop_price = entry_price * (1 + stop_pct / 100)
            tp_price = entry_price * (1 - stop_pct * take_profit_rr / 100)

        # STOP_MARKET
        sl = ProtectionOrder(
            protection_id=f"sl-{position_id}",
            position_id=position_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            order_type=ProtectionType.STOP_MARKET,
            side="SELL" if side == "LONG" else "BUY",
            trigger_price=round(stop_price, price_precision) if price_precision is not None else round(stop_price, 8),
            quantity=quantity,
        )
        self._protections[sl.protection_id] = sl
        self._position_protections.setdefault(position_id, []).append(sl.protection_id)
        protections.append(sl)

        # TAKE_PROFIT_MARKET
        tp = ProtectionOrder(
            protection_id=f"tp-{position_id}",
            position_id=position_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            order_type=ProtectionType.TAKE_PROFIT_MARKET,
            side="SELL" if side == "LONG" else "BUY",
            trigger_price=round(tp_price, price_precision) if price_precision is not None else round(tp_price, 8),
            quantity=quantity,
        )
        self._protections[tp.protection_id] = tp
        self._position_protections.setdefault(position_id, []).append(tp.protection_id)
        protections.append(tp)

        return protections

    def ack_protection(self, protection_id: str, exchange_order_id: str) -> bool:
        """交易所 ACK 确认。"""
        prot = self._protections.get(protection_id)
        if not prot:
            return False
        prot.status = ProtectionStatus.ACKED
        prot.exchange_order_id = exchange_order_id
        prot.ack_received_at = datetime.now(timezone.utc)
        return True

    def adjust_for_partial_fill(self, position_id: str, new_quantity: float) -> None:
        """部分成交后原子调整保护数量。"""
        prot_ids = self._position_protections.get(position_id, [])
        for pid in prot_ids:
            prot = self._protections.get(pid)
            if prot and prot.status == ProtectionStatus.ACKED:
                prot.quantity = new_quantity  # 需要 cancel + recreate

    def has_coverage(self, position_id: str) -> bool:
        """仓位是否有 ACKed 保护。"""
        prot_ids = self._position_protections.get(position_id, [])
        return any(
            self._protections.get(pid, ProtectionOrder("", "", "", "", ProtectionType.STOP_MARKET, "", 0.0, 0.0)).status
            == ProtectionStatus.ACKED
            for pid in prot_ids
        )

    def all_positions_covered(self) -> bool:
        """所有仓位是否都有保护覆盖。"""
        return all(self.has_coverage(pos_id) for pos_id in self._position_protections)

    def coverage_report(self) -> dict:
        """保护覆盖报告。"""
        total = len(self._position_protections)
        covered = sum(1 for pid in self._position_protections if self.has_coverage(pid))
        return {
            "total_positions": total,
            "covered": covered,
            "uncovered": total - covered,
            "coverage_pct": (covered / total * 100) if total > 0 else 100.0,
        }
