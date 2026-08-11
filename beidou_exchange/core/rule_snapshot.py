"""BD-CV10: InstrumentRuleSnapshot — 不可变交易所规则快照。

所有交易/保护/组合执行共享同一新鲜交易所规则快照。
规则 UNKNOWN/STALE 时 symbol=NOT_EXECUTABLE。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        return (
            self.tick_size != ""
            and self.step_size != ""
            and self.min_qty != ""
            and self.min_notional != ""
            and self.price_precision > 0
            and self.qty_precision > 0
        )

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
        """计算规则快照 hash。"""
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
            "observed_at": self.observed_at,
            "source": self.source,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    @classmethod
    def unknown(cls, symbol: str = "") -> InstrumentRuleSnapshot:
        """构造 UNKNOWN 规则快照。"""
        return cls(symbol=symbol, position_mode="UNKNOWN")

    @classmethod
    def from_exchange_info(cls, symbol: str, raw: dict[str, Any]) -> InstrumentRuleSnapshot:
        """从交易所 exchangeInfo 原始数据构造。"""
        filters = raw.get("filters", [])
        price_filter = next((f for f in filters if f.get("filterType") == "PRICE_FILTER"), {})
        lot_filter = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), {})
        notional_filter = next((f for f in filters if f.get("filterType") == "MIN_NOTIONAL"), {})

        tick_size = str(price_filter.get("tickSize", ""))
        step_size = str(lot_filter.get("stepSize", ""))
        min_qty = str(lot_filter.get("minQty", ""))
        min_notional = str(notional_filter.get("notional", ""))

        # 从 tick_size 推导精度
        price_precision = 0
        if tick_size and tick_size != "0":
            try:
                tick_val = float(tick_size)
                price_precision = max(0, len(str(tick_val).rstrip("0").split(".")[-1]) if "." in str(tick_val) else 0)
            except ValueError:
                pass

        qty_precision = 0
        if step_size and step_size != "0":
            try:
                step_val = float(step_size)
                qty_precision = max(0, len(str(step_val).rstrip("0").split(".")[-1]) if "." in str(step_val) else 0)
            except ValueError:
                pass

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
