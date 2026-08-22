"""BD-CV10: InstrumentRuleSnapshot — 不可变交易所规则快照。

所有交易/保护/组合执行共享同一新鲜交易所规则快照。
规则 UNKNOWN/STALE 时 symbol=NOT_EXECUTABLE。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class InstrumentRuleSnapshot:
    """不可变交易对规则快照。

    AC-10-01: 所有可写订单都绑定此快照 hash。
    AC-10-02: 未知规则无法产生可执行 OrderSlice/ProtectionOrder。
    """

    symbol: str = ""
    tick_size: str = ""
    step_size: str = ""
    min_qty: str = ""
    min_notional: str = ""
    price_precision: int = 0
    qty_precision: int = 0
    contract_size: float = 0.0
    position_mode: str = "UNKNOWN"  # HEDGE / ONEWAY / UNKNOWN
    rule_version: int = 0
    observed_at: str = ""
    source: str = "exchange_info"

    @property
    def is_known(self) -> bool:
        """规则是否已知且可用。"""
        try:
            values = tuple(
                Decimal(value) for value in (self.tick_size, self.step_size, self.min_qty, self.min_notional)
            )
        except (InvalidOperation, TypeError, ValueError):
            return False
        return all(value.is_finite() and value > 0 for value in values) and (
            self.price_precision >= 0 and self.qty_precision >= 0
        )

    @staticmethod
    def _precision(value: str) -> int:
        decimal_value = Decimal(value)
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ValueError("venue increment must be finite and positive")
        exponent = int(decimal_value.normalize().as_tuple().exponent)
        return max(0, -exponent)

    def quantize_quantity(self, quantity: str) -> str:
        """Round a positive quantity down to the exact venue step.

        Quantity rounding is deliberately one-way: quantization may reduce an
        approved amount, but must never create additional approved risk.
        """

        value = Decimal(str(quantity))
        step = Decimal(self.step_size)
        if not value.is_finite() or value <= 0 or not step.is_finite() or step <= 0:
            raise ValueError("quantity or step is invalid")
        quantized = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
        if quantized <= 0:
            raise ValueError("quantity rounds to zero")
        return f"{quantized:.{self.qty_precision}f}"

    def quantize_price(self, price: str, *, side: str) -> str:
        """Quantize without violating the approved limit-price direction."""

        value = Decimal(str(price))
        tick = Decimal(self.tick_size)
        if not value.is_finite() or value <= 0 or not tick.is_finite() or tick <= 0:
            raise ValueError("price or tick is invalid")
        side_upper = str(side).upper()
        if side_upper not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        rounding = ROUND_DOWN if side_upper == "BUY" else ROUND_UP
        quantized = (value / tick).to_integral_value(rounding=rounding) * tick
        if quantized <= 0:
            raise ValueError("price rounds to zero")
        return f"{quantized:.{self.price_precision}f}"

    @property
    def is_stale(self, max_age_seconds: float = 3600.0) -> bool:
        """规则是否过期。"""
        if not self.observed_at:
            return True
        try:
            ts = datetime.fromisoformat(self.observed_at)
            now = datetime.now(timezone.utc)
            return (now - ts).total_seconds() > max_age_seconds
        except (ValueError, TypeError):
            return True

    def compute_hash(self) -> str:
        """计算规则快照 hash。

        BD-FIX (refresh-stability): 只对规则内容取 hash —— observed_at 是
        刷新时刻,周期刷新(offline tick 25 分钟)会翻转全部品种的 hash,
        执行器据此把每个品种的首个订单判为 VENUE_RULE_SNAPSHOT_CHANGED,
        且旧实现变更后不回写已记录 hash → 品种被永久拒绝(实测 187 连拒,
        拒绝率随运行时间单调上升)。规则本身不变时 hash 必须稳定。
        """
        data = {
            "symbol": self.symbol,
            "tick_size": self.tick_size,
            "step_size": self.step_size,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
            "price_precision": self.price_precision,
            "qty_precision": self.qty_precision,
            "contract_size": self.contract_size,
            "rule_version": self.rule_version,
            "source": self.source,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def unknown(cls, symbol: str = "") -> InstrumentRuleSnapshot:
        """构造 UNKNOWN 规则快照。"""
        return cls(symbol=symbol, position_mode="UNKNOWN")

    @classmethod
    def from_exchange_info(cls, symbol: str, raw: dict[str, Any]) -> InstrumentRuleSnapshot:
        """从交易所 exchangeInfo 原始数据构造。"""
        filters: list[dict[str, Any]] = raw.get("filters", [])
        price_filter: dict[str, Any] = next((f for f in filters if f.get("filterType") == "PRICE_FILTER"), {})
        lot_filter: dict[str, Any] = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), {})
        notional_filter: dict[str, Any] = next((f for f in filters if f.get("filterType") == "MIN_NOTIONAL"), {})

        tick_size = str(price_filter.get("tickSize", ""))
        step_size = str(lot_filter.get("stepSize", ""))
        min_qty = str(lot_filter.get("minQty", ""))
        min_notional = str(notional_filter.get("notional", ""))

        try:
            price_precision = cls._precision(tick_size)
        except (InvalidOperation, TypeError, ValueError):
            price_precision = -1
        try:
            qty_precision = cls._precision(step_size)
        except (InvalidOperation, TypeError, ValueError):
            qty_precision = -1

        return cls(
            symbol=symbol,
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
            price_precision=price_precision,
            qty_precision=qty_precision,
            contract_size=float(raw.get("contractSize", 0)),
            position_mode=str(raw.get("positionMode", "UNKNOWN")),
            rule_version=int(raw.get("version", 0)),
            observed_at=datetime.now(timezone.utc).isoformat(),
            source="exchange_info",
        )
