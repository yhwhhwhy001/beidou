"""止盈止损保护引擎 — 动态止损、风险回报止盈、移动止损、保护单生命周期。

止损类型:
  - FIXED_PERCENT: 固定百分比止损
  - ATR_BASED: ATR 波动率止损
  - VOLATILITY_BASED: 历史波动率止损
  - TRAILING: 移动止损(跟踪最高/最低价)
  - SWING_STRUCTURE: 基于支撑/阻力位止损

止盈类型:
  - FIXED_RR: 固定风险回报比
  - MULTI_TARGET: 多目标分批止盈
  - TRAILING_TAKE_PROFIT: 移动止盈

保护单生命周期:
  CREATED → ACTIVE → TRIGGERED → EXECUTED / EXPIRED / CANCELLED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import (
    CorrelationId,
    InstrumentId,
    OrderSide,
    Price,
    Quantity,
    VenueId,
)


# ================================================================
# BD-T11: 保护参数默认值 — 来自版本化策略配置 (v1)。
# 禁止在调用点硬编码策略数值（stop_pct / rr_ratio / multiplier /
# trail_pct / min_trail_distance 等）；修改必须走 BD-T11 策略版本
# 变更流程，并同步更新版本化策略配置。
# ================================================================
DEFAULT_STOP_PCT = 2.0  # BD-T11 v1: 固定百分比/移动止损默认百分比
DEFAULT_RR_RATIO = 2.0  # BD-T11 v1: 默认风险回报比
DEFAULT_MULTIPLIER = 2.0  # BD-T11 v1: ATR/历史波动率止损默认倍数
DEFAULT_TRAIL_PCT = 2.0  # BD-T11 v1: 移动止损默认百分比
DEFAULT_MIN_TRAIL_DISTANCE = 0.5  # BD-T11 v1: 移动止损最小距离
DEFAULT_FALLBACK_STOP_PCT = 5.0  # BD-T11 v1: swing 结构缺数据回退止损百分比


class StopLossType(str, Enum):
    FIXED_PERCENT = "FIXED_PERCENT"
    ATR_BASED = "ATR_BASED"
    VOLATILITY_BASED = "VOLATILITY_BASED"
    TRAILING = "TRAILING"
    SWING_STRUCTURE = "SWING_STRUCTURE"


class TakeProfitType(str, Enum):
    FIXED_RR = "FIXED_RR"
    MULTI_TARGET = "MULTI_TARGET"
    TRAILING_TAKE_PROFIT = "TRAILING_TAKE_PROFIT"


class ProtectionStatus(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(slots=True)
class ProtectionOrder:
    """保护单定义 — 状态随生命周期变更。"""

    protection_id: str
    position_id: str
    instrument_id: InstrumentId
    venue_id: VenueId
    side: OrderSide  # SELL for long positions, BUY for short positions
    trigger_price: Price
    order_price: Price | None  # None = 市价单
    quantity: Quantity
    order_type: str  # STOP_MARKET, STOP_LIMIT, TAKE_PROFIT_MARKET, TAKE_PROFIT_LIMIT
    reduce_only: bool = True
    status: ProtectionStatus = ProtectionStatus.CREATED
    stop_type: StopLossType | None = None
    take_profit_type: TakeProfitType | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_at: datetime | None = None
    correlation_id: CorrelationId | None = None

    def is_stop_loss(self) -> bool:
        return self.stop_type is not None

    def is_take_profit(self) -> bool:
        return self.take_profit_type is not None

    def is_active(self) -> bool:
        return self.status == ProtectionStatus.ACTIVE


@dataclass
class PositionProtection:
    """单个仓位的完整保护方案 — 包含止损和止盈。"""

    position_id: str
    instrument_id: InstrumentId
    venue_id: VenueId
    entry_price: float
    quantity: float
    side: OrderSide  # BUY=long, SELL=short
    stop_loss: ProtectionOrder | None = None
    take_profits: list[ProtectionOrder] = field(default_factory=list)
    trailing_config: dict[str, float] = field(default_factory=dict)
    # 跟踪数据
    highest_price: float | None = None  # long仓用
    lowest_price: float | None = None  # short仓用
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_long(self) -> bool:
        return self.side == OrderSide.BUY

    def is_short(self) -> bool:
        return self.side == OrderSide.SELL

    def update_price_extremes(self, current_price: float) -> None:
        """更新跟踪中的最高/最低价（用于移动止损）。"""
        if self.is_long():
            if self.highest_price is None or current_price > self.highest_price:
                self.highest_price = current_price
        else:
            if self.lowest_price is None or current_price < self.lowest_price:
                self.lowest_price = current_price
        self.last_updated = datetime.now(timezone.utc)

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """未实现盈亏百分比。"""
        if self.entry_price == 0:
            return 0.0
        if self.is_long():
            return (current_price - self.entry_price) / self.entry_price * 100
        else:
            return (self.entry_price - current_price) / self.entry_price * 100


class StopLossCalculator:
    """止损计算器 — 根据类型计算止损价格。"""

    @staticmethod
    def fixed_percent(entry_price: float, side: OrderSide, stop_pct: float) -> float:
        """固定百分比止损。例如 stop_pct=2.0 → 入场价下方2%。"""
        if side == OrderSide.BUY:
            return entry_price * (1 - stop_pct / 100)
        else:
            return entry_price * (1 + stop_pct / 100)

    @staticmethod
    def atr_based(entry_price: float, side: OrderSide, atr: float, multiplier: float = DEFAULT_MULTIPLIER) -> float:
        """ATR 波动率止损。止损距离 = ATR × 倍数。"""
        if side == OrderSide.BUY:
            return entry_price - atr * multiplier
        else:
            return entry_price + atr * multiplier

    @staticmethod
    def volatility_based(
        entry_price: float, side: OrderSide, volatility_pct: float, multiplier: float = DEFAULT_MULTIPLIER
    ) -> float:
        """历史波动率止损。"""
        if side == OrderSide.BUY:
            return entry_price * (1 - volatility_pct * multiplier / 100)
        else:
            return entry_price * (1 + volatility_pct * multiplier / 100)

    @staticmethod
    def swing_structure(
        swing_low: float | None, swing_high: float | None, side: OrderSide, entry_price: float = 0.0
    ) -> float:
        """基于支撑/阻力位止损。缺数据时回退到固定百分比。"""
        if side == OrderSide.BUY and swing_low is not None:
            return swing_low * 0.999  # 略低于前低
        elif side == OrderSide.SELL and swing_high is not None:
            return swing_high * 1.001  # 略高于前高
        # 缺数据时回退：BD-T11 默认止损百分比，避免 0.0 导致的误触发
        if side == OrderSide.BUY:
            return entry_price * (1 - DEFAULT_FALLBACK_STOP_PCT / 100) if entry_price > 0 else 0.0
        else:
            return entry_price * (1 + DEFAULT_FALLBACK_STOP_PCT / 100) if entry_price > 0 else float("inf")

    @staticmethod
    def calculate(
        stop_type: StopLossType,
        entry_price: float,
        side: OrderSide,
        atr: float | None = None,
        volatility_pct: float | None = None,
        stop_pct: float = DEFAULT_STOP_PCT,
        multiplier: float = DEFAULT_MULTIPLIER,
        swing_low: float | None = None,
        swing_high: float | None = None,
    ) -> float:
        """统一止损计算入口。"""
        if stop_type == StopLossType.FIXED_PERCENT:
            return StopLossCalculator.fixed_percent(entry_price, side, stop_pct)
        elif stop_type == StopLossType.ATR_BASED:
            return StopLossCalculator.atr_based(entry_price, side, atr or 0.0, multiplier)
        elif stop_type == StopLossType.VOLATILITY_BASED:
            return StopLossCalculator.volatility_based(entry_price, side, volatility_pct or 1.0, multiplier)
        elif stop_type == StopLossType.SWING_STRUCTURE:
            return StopLossCalculator.swing_structure(swing_low, swing_high, side, entry_price)
        elif stop_type == StopLossType.TRAILING:
            # 移动止损初始值 = 固定百分比
            return StopLossCalculator.fixed_percent(entry_price, side, stop_pct)
        return entry_price * (1 - DEFAULT_FALLBACK_STOP_PCT / 100)  # BD-T11 默认回退止损



class TakeProfitCalculator:
    """止盈计算器 — 基于风险回报比或多目标。"""

    @staticmethod
    def fixed_rr(
        entry_price: float, stop_loss_price: float, side: OrderSide, rr_ratio: float = DEFAULT_RR_RATIO
    ) -> float:
        """基于风险回报比的止盈。风险 = |入场-止损| × RR。"""
        risk = abs(entry_price - stop_loss_price)
        if side == OrderSide.BUY:
            return entry_price + risk * rr_ratio
        else:
            return entry_price - risk * rr_ratio

    @staticmethod
    def multi_target(
        entry_price: float,
        stop_loss_price: float,
        side: OrderSide,
        targets: list[dict[str, float]],
    ) -> list[dict[str, Any]]:
        """多目标分批止盈。

        targets: [{"rr_ratio": 1.0, "close_pct": 30}, {"rr_ratio": 2.0, "close_pct": 40}, ...]
        返回: [{"price": xxx, "close_pct": yy, "rr_ratio": zz}, ...]
        """
        risk = abs(entry_price - stop_loss_price)
        result = []
        for t in targets:
            rr = t.get("rr_ratio", DEFAULT_RR_RATIO)
            close_pct = t.get("close_pct", 50.0)
            price = entry_price + risk * rr if side == OrderSide.BUY else entry_price - risk * rr
            result.append(
                {
                    "price": round(price, 2),
                    "close_pct": close_pct,
                    "rr_ratio": rr,
                    "quantity_pct": close_pct / 100,
                }
            )
        return result

    @staticmethod
    def calculate(
        take_profit_type: TakeProfitType,
        entry_price: float,
        stop_loss_price: float,
        side: OrderSide,
        rr_ratio: float = DEFAULT_RR_RATIO,
        targets: list[dict[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """统一止盈计算入口，返回止盈目标列表。"""
        if take_profit_type == TakeProfitType.FIXED_RR:
            return [
                {
                    "price": TakeProfitCalculator.fixed_rr(entry_price, stop_loss_price, side, rr_ratio),
                    "close_pct": 100.0,
                    "rr_ratio": rr_ratio,
                    "quantity_pct": 1.0,
                }
            ]
        elif take_profit_type == TakeProfitType.MULTI_TARGET:
            return TakeProfitCalculator.multi_target(
                entry_price, stop_loss_price, side, targets or [{"rr_ratio": rr_ratio, "close_pct": 100.0}]
            )
        elif take_profit_type == TakeProfitType.TRAILING_TAKE_PROFIT:
            # 移动止盈：初始目标同 FIXED_RR，后续由交易所 TRAILING_STOP_MARKET 动态跟踪
            return [
                {
                    "price": TakeProfitCalculator.fixed_rr(entry_price, stop_loss_price, side, rr_ratio),
                    "close_pct": 100.0,
                    "rr_ratio": rr_ratio,
                    "quantity_pct": 1.0,
                }
            ]
        return [{"price": entry_price, "close_pct": 0, "rr_ratio": 0, "quantity_pct": 0}]


class ProtectionManager:
    """保护单生命周期管理器 — 统一管理所有仓位的止盈止损。

    1. 开仓时自动创建止损+止盈保护单
    2. 实时监控价格变动，触发止损/止盈
    3. 支持移动止损动态更新
    4. 保护单触发后自动生成 OrderIntent
    """

    def __init__(self) -> None:
        self._protections: dict[str, PositionProtection] = {}
        self._history: list[ProtectionOrder] = []

    # ---- 创建保护 ----

    def create_protection(
        self,
        position_id: str,
        instrument_id: InstrumentId,
        venue_id: VenueId,
        entry_price: float,
        quantity: float,
        side: OrderSide,
        stop_loss_config: dict[str, Any] | None = None,
        take_profit_config: dict[str, Any] | None = None,
        trailing_config: dict[str, float] | None = None,
    ) -> PositionProtection:
        """为一笔新仓位创建完整的保护方案。"""
        pp = PositionProtection(
            position_id=position_id,
            instrument_id=instrument_id,
            venue_id=venue_id,
            entry_price=entry_price,
            quantity=quantity,
            side=side,
            trailing_config=trailing_config or {},
        )
        pp.update_price_extremes(entry_price)

        # 止损
        if stop_loss_config:
            sl_type = StopLossType(stop_loss_config.get("type", "FIXED_PERCENT"))
            stop_price = StopLossCalculator.calculate(
                sl_type,
                entry_price,
                side,
                atr=stop_loss_config.get("atr"),
                volatility_pct=stop_loss_config.get("volatility_pct"),
                stop_pct=stop_loss_config.get("stop_pct", DEFAULT_STOP_PCT),
                multiplier=stop_loss_config.get("multiplier", DEFAULT_MULTIPLIER),
                swing_low=stop_loss_config.get("swing_low"),
                swing_high=stop_loss_config.get("swing_high"),
            )
            sl_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            order_type = "STOP_LIMIT" if stop_loss_config.get("use_limit", False) else "STOP_MARKET"
            # STOP_LIMIT 限价：SELL(SHORT)需低于触发价；BUY(LONG)需高于触发价
            if order_type == "STOP_LIMIT":
                if sl_side == OrderSide.SELL:
                    limit_price = round(stop_price * 0.995, 2)
                else:
                    limit_price = round(stop_price * 1.005, 2)
            else:
                limit_price = None
            pp.stop_loss = ProtectionOrder(
                protection_id=f"sl-{position_id}",
                position_id=position_id,
                instrument_id=instrument_id,
                venue_id=venue_id,
                side=sl_side,
                trigger_price=Price(amount=str(round(stop_price, 2))),
                order_price=Price(amount=str(limit_price)) if limit_price is not None else None,
                quantity=Quantity(amount=str(quantity)),
                order_type=order_type,
                reduce_only=True,
                status=ProtectionStatus.ACTIVE,
                stop_type=sl_type,
                reason=f"Stop Loss: {sl_type.value}",
            )

        # 止盈
        if take_profit_config:
            tp_type = TakeProfitType(take_profit_config.get("type", "FIXED_RR"))
            stop_price_for_rr = (
                float(pp.stop_loss.trigger_price.amount)
                if pp.stop_loss
                else entry_price * (1 - DEFAULT_FALLBACK_STOP_PCT / 100)
            )
            tp_targets = TakeProfitCalculator.calculate(
                tp_type,
                entry_price,
                stop_price_for_rr,
                side,
                rr_ratio=take_profit_config.get("rr_ratio", DEFAULT_RR_RATIO),
                targets=take_profit_config.get("targets"),
            )
            tp_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            for i, target in enumerate(tp_targets):
                qty = quantity * target["quantity_pct"]
                if qty <= 0:
                    continue
                tp_order = ProtectionOrder(
                    protection_id=f"tp-{position_id}-{i}",
                    position_id=position_id,
                    instrument_id=instrument_id,
                    venue_id=venue_id,
                    side=tp_side,
                    trigger_price=Price(amount=str(target["price"])),
                    order_price=None,  # 市价止盈
                    quantity=Quantity(amount=str(round(qty, 4))),
                    order_type="TAKE_PROFIT_MARKET",
                    reduce_only=True,
                    status=ProtectionStatus.ACTIVE,
                    take_profit_type=tp_type,
                    reason=f"Take Profit {i + 1}/{len(tp_targets)}: RR={target['rr_ratio']} "
                    f"close={target['close_pct']}%",
                    metadata={"rr_ratio": target["rr_ratio"], "close_pct": target["close_pct"]},
                )
                pp.take_profits.append(tp_order)

        self._protections[position_id] = pp
        return pp

    # ---- 保护单生命周期 ----

    def cancel_protection(self, position_id: str) -> list[ProtectionOrder]:
        """取消仓位的所有保护单（平仓时调用）。"""
        pp = self._protections.get(position_id)
        if pp is None:
            return []
        cancelled: list[ProtectionOrder] = []
        if pp.stop_loss and pp.stop_loss.is_active():
            pp.stop_loss.status = ProtectionStatus.CANCELLED
            cancelled.append(pp.stop_loss)
            self._history.append(pp.stop_loss)
        for tp in pp.take_profits:
            if tp.is_active():
                tp.status = ProtectionStatus.CANCELLED
                cancelled.append(tp)
                self._history.append(tp)
        return cancelled

    def remove_position(self, position_id: str) -> None:
        """移除仓位及其所有保护单。"""
        self._protections.pop(position_id, None)

    # ---- 查询 ----

    def get_protection(self, position_id: str) -> PositionProtection | None:
        return self._protections.get(position_id)

    def all_positions(self) -> dict[str, PositionProtection]:
        return dict(self._protections)

    def position_count(self) -> int:
        return len(self._protections)
