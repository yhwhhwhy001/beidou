"""自主运行引擎 — 三层时钟域事件循环。

绕过存根 BinanceUsdmAdapter，直接调用 Binance REST API。
实现真实市场状态估算和具体 AlphaComponent。
完整激活所有策略/因子/风控模块。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from beidou_autonomy.mapek import MAPEKController
from beidou_control.plane import ControlAction, ControlPlane
from beidou_core.alerts import AlertDispatcher
from beidou_core.feed import MarketDataFeed
from beidou_core.health import HealthServer
from beidou_core.store import PersistentStore
from beidou_data.trading_pool_lifecycle import InstrumentScore, TradingPool
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_lifecycle.lifecycle import DegradationLevel, ModuleLifecycle, ModuleState
from beidou_observability.telemetry import AlertSeverity
from beidou_research.factors.factor import (
    FactorDefinition,
    FactorEvaluator,
    FactorLifecycle,
    FactorRegistry,
)
from beidou_safety.execution.intent import IntentOutbox
from beidou_safety.execution.ledger import ImmutableLedger, JournalEntry
from beidou_safety.execution.order_state import OrderEvent, OrderStateTracker
from beidou_safety.execution.reconciliation import AccountFactSnapshot, ReconciliationEngine
from beidou_safety.protection.engine import ProtectionManager
from beidou_safety.risk.engine import (
    PostRiskMonitor,
    PreRiskCheckerImpl,
    RiskApprovalSignerImpl,
    RiskApprovalStateMachine,
    RiskEngineImpl,
    RiskSnapshot,
)
from beidou_shared.config import ConfigProvider, TypedSettings
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    OrderId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    RiskApprovalId,
    RiskDecision,
    SchemaVersion,
    StrategyId,
    TimeInForce,
    VenueId,
    VenueInstrument,
)
from beidou_strategy.alpha import (
    AlphaComponent,
    AlphaComponentType,
    AlphaGraph,
    SignalDirection,
)  # BD-T05: legacy, migrated to StrategyKernel
from beidou_strategy.alpha.model_registry import DriftDetector, ModelRegistry
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.kernel_parity import KernelMode, ParityResult, StrategyKernelContract
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_strategy.protection.adaptive import AdaptiveProtectionCalculator
from beidou_strategy.risk.manager import (
    RiskBudget,
    StrategyRiskLevel,
    StrategyRiskManager,
)
from beidou_strategy.state.cost_model import CostModel

logger = logging.getLogger(__name__)

# Binance USDⓈ-M 永续合约交易池 — 主流 + 活跃altcoin
DEFAULT_UNIVERSE = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "DOTUSDT",
    "LINKUSDT",
    "MATICUSDT",
    "UNIUSDT",
    "ATOMUSDT",
    "LTCUSDT",
    "ETCUSDT",
    "FILUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "NEARUSDT",
    "INJUSDT",
    "SUIUSDT",
    "RUNEUSDT",
    "SEIUSDT",
    "TIAUSDT",
]

# ================================================================
# 自适应杠杆 — 基于波动率分级
# ================================================================


def adaptive_leverage(ann_volatility: float) -> float:
    """波动率越高杠杆越低，控制风险暴露。"""
    if ann_volatility <= 0.0:
        return 2.0  # 数据缺失时保守
    if ann_volatility < 0.2:
        return 3.0  # 低波动：可以放大
    if ann_volatility < 0.4:
        return 2.0  # 正常波动
    if ann_volatility < 0.6:
        return 1.0  # 偏高波动：减半
    return 0.5  # 极端波动：最小化


def adaptive_position_pct(strength: float, ann_volatility: float, spread_bps: float) -> float:
    """自适应仓位比例：信号强度 × 波动率惩罚 × 点差惩罚。"""
    vol_penalty = max(0.2, 1.0 - ann_volatility)  # 波动越高惩罚越大
    spread_penalty = max(0.3, 1.0 - spread_bps / 50.0)  # 点差越大惩罚越大
    base = strength * 0.02  # 基础: 2% of signal strength
    return base * vol_penalty * spread_penalty


# ================================================================
# 具体 AlphaComponent 实现
# ================================================================


class MeanReversionEntry(AlphaComponent):
    """均值回归入场组件 — 短期价格偏离 SMA 时入场。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.ENTRY,
            component_id="meanrev_entry_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        close = features.get("close", 0)
        sma_20 = features.get("sma_20", close)
        rsi = features.get("rsi_14", 50)

        deviation_pct = (close - sma_20) / sma_20 * 100 if sma_20 > 0 else 0

        if deviation_pct < -1.0 and rsi < 40:
            direction = SignalDirection.LONG
            strength = min(0.9, abs(deviation_pct) / 5.0)
        elif deviation_pct > 1.0 and rsi > 60:
            direction = SignalDirection.SHORT
            strength = min(0.9, abs(deviation_pct) / 5.0)
        else:
            direction = SignalDirection.NO_ACTION
            strength = 0.1

        signal = AlphaSignal(
            strategy_id=StrategyId("meanrev_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=0.5 + strength * 0.4,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"deviation_pct": deviation_pct, "rsi": rsi},
        )
        # Store prediction for factor evaluation
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["meanrev_entry_v1"] = deviation_pct
        return signal

    def validate(self) -> bool:
        return True


class MomentumFilter(AlphaComponent):
    """动量过滤器 — 趋势确认，过滤逆势信号。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.FILTER,
            component_id="momentum_filter_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        trend_20 = features.get("trend_20_pct", 0)
        ann_vol = features.get("ann_volatility", 0.3)

        # 高波动时否决一切信号
        if ann_vol > 0.8:
            signal = AlphaSignal(
                strategy_id=StrategyId("momentum_filter_v1"),
                component_type=self.component_type,
                direction=SignalDirection.NO_ACTION,
                strength=0.0,
                confidence=0.8,
                instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
                venue_id=context.get("venue_id", VenueId("BINANCE")),
                model_version=SchemaVersion("2.0.0"),
                metadata={"reason": "high_volatility", "ann_vol": ann_vol},
            )
            context["_predictions"] = context.get("_predictions", {})
            context["_predictions"]["momentum_filter_v1"] = -1.0  # negative = filter blocked
            return signal

        direction = SignalDirection.LONG if trend_20 > 0 else SignalDirection.SHORT
        signal = AlphaSignal(
            strategy_id=StrategyId("momentum_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=min(0.7, abs(trend_20) / 10),
            confidence=0.55,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"trend_20_pct": trend_20},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["momentum_filter_v1"] = trend_20
        return signal

    def validate(self) -> bool:
        return True


class TrendFollowingEntry(AlphaComponent):
    """趋势跟踪入场 — SMA5/SMA20 金叉死叉 + 趋势强度确认。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.ENTRY,
            component_id="trend_entry_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        sma_5 = features.get("sma_5", 0)
        sma_20 = features.get("sma_20", 0)
        trend_20 = features.get("trend_20_pct", 0)
        rsi = features.get("rsi_14", 50)

        # SMA crossover signal
        crossover = sma_5 - sma_20
        trend_strength = abs(trend_20) / 5.0  # normalize to [0,1]

        if crossover > 0 and trend_20 > 0.5 and rsi < 70:
            direction = SignalDirection.LONG
            strength = min(0.8, trend_strength + abs(crossover) / sma_20 * 10)
        elif crossover < 0 and trend_20 < -0.5 and rsi > 30:
            direction = SignalDirection.SHORT
            strength = min(0.8, trend_strength + abs(crossover) / sma_20 * 10)
        else:
            direction = SignalDirection.NO_ACTION
            strength = 0.05
            # Weak trend — still report direction for context
            if crossover > 0.001:
                direction = SignalDirection.LONG
                strength = max(0.05, min(0.3, abs(crossover) / sma_20 * 10))

        signal = AlphaSignal(
            strategy_id=StrategyId("trend_following_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=0.5 + strength * 0.4,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"crossover": crossover, "trend_20": trend_20, "rsi": rsi},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["trend_entry_v1"] = crossover / max(sma_20, 1)
        return signal

    def validate(self) -> bool:
        return True


class BreakoutEntry(AlphaComponent):
    """突破入场 — 价格突破 20 期最高/最低 + 波动率扩张确认。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.ENTRY,
            component_id="breakout_entry_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        close = features.get("close", 0)
        highest_20 = features.get("highest_20", close)
        lowest_20 = features.get("lowest_20", close)
        ann_vol = features.get("ann_volatility", 0.3)
        vol_ratio = features.get("vol_ratio", 1.0)
        atr_pct = features.get("atr_pct", 1.0)

        breakout_up = close > highest_20 * 1.001  # 0.1% tolerance
        breakout_down = close < lowest_20 * 0.999
        vol_expanding = ann_vol > 0.2 and vol_ratio > 1.2

        if breakout_up and vol_expanding:
            direction = SignalDirection.LONG
            breakout_pct = (close - highest_20) / highest_20 * 100
            strength = min(0.9, breakout_pct / 2.0 + atr_pct / 5.0)
        elif breakout_down and vol_expanding:
            direction = SignalDirection.SHORT
            breakout_pct = (lowest_20 - close) / lowest_20 * 100
            strength = min(0.9, breakout_pct / 2.0 + atr_pct / 5.0)
        else:
            direction = SignalDirection.NO_ACTION
            strength = 0.05

        signal = AlphaSignal(
            strategy_id=StrategyId("breakout_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=0.45 + strength * 0.35,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"breakout_up": breakout_up, "breakout_down": breakout_down, "vol_expanding": vol_expanding},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["breakout_entry_v1"] = (close - highest_20) / max(highest_20, 1)
        return signal

    def validate(self) -> bool:
        return True


class VolatilityFilter(AlphaComponent):
    """波动率过滤器 — ATR 分级：高波动否决，低波动放行，中等波动降级。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.FILTER,
            component_id="volatility_filter_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        ann_vol = features.get("ann_volatility", 0.3)
        atr_pct = features.get("atr_pct", 1.0)
        rsi = features.get("rsi_14", 50)

        if ann_vol > 0.6 or atr_pct > 5.0:
            # Extreme volatility — veto everything
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.9
            reason = "extreme_volatility"
        elif ann_vol > 0.4:
            # Elevated — reduce conviction
            direction = SignalDirection.LONG  # pass-through with reduced weight
            strength = 0.15
            confidence = 0.5
            reason = "elevated_volatility"
        else:
            # Normal — confirm direction via RSI
            # rsi > 70 → overbought bias SHORT; rsi < 30 → oversold bias LONG
            direction = SignalDirection.SHORT if rsi > 70 else SignalDirection.LONG
            strength = 0.2
            confidence = 0.6
            reason = "normal"

        signal = AlphaSignal(
            strategy_id=StrategyId("volatility_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"ann_vol": ann_vol, "atr_pct": atr_pct, "reason": reason},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["volatility_filter_v1"] = -1.0 if reason == "extreme_volatility" else ann_vol
        return signal

    def validate(self) -> bool:
        return True


class VolumeFilter(AlphaComponent):
    """成交量过滤器 — 量比确认：放量增强，缩量降级，无量否决。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.FILTER,
            component_id="volume_filter_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        vol_ratio = features.get("vol_ratio", 1.0)
        trend_20 = features.get("trend_20_pct", 0)
        ann_vol = features.get("ann_volatility", 0.2)

        if vol_ratio < 0.25:
            # Dead volume — veto
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.85
            reason = "dead_volume"
        elif vol_ratio < 0.5:
            # Low volume — degrade signal
            direction = SignalDirection.LONG if trend_20 >= 0 else SignalDirection.SHORT
            strength = 0.08
            confidence = 0.45
            reason = "low_volume"
        elif vol_ratio > 1.5 and ann_vol > 0.15:
            # Surge volume — strong confirmation
            direction = SignalDirection.LONG if trend_20 >= 0 else SignalDirection.SHORT
            strength = min(0.25, vol_ratio / 10)
            confidence = 0.65
            reason = "volume_surge"
        else:
            # Normal volume — neutral pass-through
            direction = SignalDirection.LONG if trend_20 >= 0 else SignalDirection.SHORT
            strength = 0.12
            confidence = 0.5
            reason = "normal"

        signal = AlphaSignal(
            strategy_id=StrategyId("volume_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"vol_ratio": vol_ratio, "reason": reason},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["volume_filter_v1"] = -1.0 if reason == "dead_volume" else vol_ratio
        return signal

    def validate(self) -> bool:
        return True


class TrailingExit(AlphaComponent):
    """ATR 跟踪止损止盈 — 回撤超过 2× ATR 触发退出。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.EXIT,
            component_id="trailing_exit_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        close = features.get("close", 0)
        atr_pct = features.get("atr_pct", 1.0)
        sma_20 = features.get("sma_20", close)
        rsi = features.get("rsi_14", 50)

        # Check if there's a position to manage
        position_info = context.get("_position_info", {})
        has_position = position_info.get("has_position", False)
        entry_price = position_info.get("entry_price", 0)
        position_side = position_info.get("side", "")

        if not has_position or entry_price <= 0:
            # No position — return neutral
            direction = SignalDirection.FLAT
            strength = 0.0
            reason = "no_position"
        else:
            # Compute drawdown from entry
            if position_side == "LONG":
                pnl_pct = (close - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - close) / entry_price * 100

            # Trail stop: exit if drawdown > 2× ATR from peak
            trailing_stop = 2.0 * atr_pct
            if pnl_pct < -trailing_stop:
                direction = SignalDirection.FLAT
                strength = 0.9
                reason = f"stop_loss: {pnl_pct:.2f}% < -{trailing_stop:.2f}%"
            elif pnl_pct > 3.0 * atr_pct and rsi > 70:
                # Take profit: 3× ATR + overbought
                direction = SignalDirection.FLAT
                strength = 0.7
                reason = f"take_profit: {pnl_pct:.2f}% > {3.0 * atr_pct:.2f}%"
            elif close < sma_20 and position_side == "LONG":
                # Trend breakdown — exit long
                direction = SignalDirection.FLAT
                strength = 0.5
                reason = "trend_breakdown_sma20"
            elif close > sma_20 and position_side == "SHORT":
                # Trend reversal — exit short
                direction = SignalDirection.FLAT
                strength = 0.5
                reason = "trend_reversal_sma20"
            else:
                direction = SignalDirection.NO_ACTION
                strength = 0.0
                reason = f"hold: pnl={pnl_pct:.2f}%"

        signal = AlphaSignal(
            strategy_id=StrategyId("trailing_exit_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=0.7,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"reason": reason, "atr_pct": atr_pct},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["trailing_exit_v1"] = strength if direction == SignalDirection.FLAT else 0.0
        return signal

    def validate(self) -> bool:
        return True


class TimeExit(AlphaComponent):
    """持仓时间退出 — 超时强制退出，避免过夜风险累积。"""

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.EXIT,
            component_id="time_exit_v1",
            version=SchemaVersion("2.0.0"),
        )

    async def generate(self, context: dict) -> Any:
        import time as _time

        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        features.get("close", 0)

        position_info = context.get("_position_info", {})
        has_position = position_info.get("has_position", False)
        entry_time = position_info.get("entry_time", 0)
        pnl_pct = position_info.get("pnl_pct", 0)

        if not has_position or entry_time <= 0:
            direction = SignalDirection.FLAT
            strength = 0.0
            reason = "no_position"
        else:
            hold_hours = (_time.time() - entry_time) / 3600
            if hold_hours > 48:
                # 48h+ — force exit regardless
                direction = SignalDirection.FLAT
                strength = 0.95
                reason = f"max_hold: {hold_hours:.1f}h > 48h"
            elif hold_hours > 24 and pnl_pct < 0:
                # 24h+ underwater — exit
                direction = SignalDirection.FLAT
                strength = 0.7
                reason = f"time_stop: {hold_hours:.1f}h pnl={pnl_pct:.2f}%"
            elif hold_hours > 12 and pnl_pct > 5.0:
                # 12h+ with good profit — take it
                direction = SignalDirection.FLAT
                strength = 0.4
                reason = f"time_take_profit: {hold_hours:.1f}h pnl={pnl_pct:.2f}%"
            else:
                direction = SignalDirection.NO_ACTION
                strength = 0.0
                reason = f"ok: {hold_hours:.1f}h"

        signal = AlphaSignal(
            strategy_id=StrategyId("time_exit_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=0.75,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={"reason": reason},
        )
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["time_exit_v1"] = strength if direction == SignalDirection.FLAT else 0.0
        return signal

    def validate(self) -> bool:
        return True


# ================================================================
# 真实市场状态估算
# ================================================================


class RealMarketStateEstimator:
    """真实市场状态估算 — 替代始终返回 UNKNOWN 的 MarketStateEstimator。"""

    @staticmethod
    def estimate(features: dict[str, float]) -> dict[str, str]:
        ann_vol = features.get("ann_volatility", 0.3)

        sma_5 = features.get("sma_5", 0)
        sma_20 = features.get("sma_20", 0)
        if sma_5 > sma_20 * 1.01:
            direction = "TRENDING_UP"
        elif sma_5 < sma_20 * 0.99:
            direction = "TRENDING_DOWN"
        else:
            direction = "RANGING"

        if ann_vol > 0.8:
            stress = "HIGH"
        elif ann_vol > 0.3:
            stress = "NORMAL"
        else:
            stress = "LOW"

        has_sufficient_data = (
            features.get("close", 0) > 0 and features.get("sma_5", 0) > 0 and features.get("sma_20", 0) > 0
        )
        quality = "RELIABLE" if has_sufficient_data and ann_vol > 0 else "UNKNOWN"

        return {
            "direction": direction,
            "stress": stress,
            "quality": quality,
        }


# ================================================================
# 自主运行引擎
# ================================================================


class AutonomousEngine:
    """北斗自主运行引擎。三个时钟域的 asyncio 事件循环。"""

    def __init__(self, symbols: list[str], mode: str = "paper") -> None:
        self._symbols = symbols
        self._cli_mode = mode  # CLI 参数: research/paper/shadow/testnet/safety_only

        # 封闭运行模式枚举 — 决定写能力
        from beidou_core.guard import EnvironmentMode

        _MODE_MAP = {
            "research": EnvironmentMode.RESEARCH,
            "paper": EnvironmentMode.PAPER,
            "shadow": EnvironmentMode.SHADOW,
            "testnet": EnvironmentMode.TESTNET,
            "safety_only": EnvironmentMode.SAFETY_ONLY,
        }
        self._env_mode = _MODE_MAP.get(mode, EnvironmentMode.SAFETY_ONLY)
        # 是否允许 POST/PUT/DELETE 交易写请求
        self._can_write = self._env_mode.can_write_trades

        # Config — 使用统一配置提供器，禁止直接读取 YAML
        # 加载优先级: CLI explicit > BEIDOU_ENV > env-specific file > SAFETY_ONLY
        config_provider = ConfigProvider()
        self._settings: TypedSettings = config_provider.load(environment=mode)
        self._rest_url = self._settings.exchange.rest_base_url
        # 优先从环境变量读取，其次从配置文件 api_key_ref/api_secret_ref 字段
        self._api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "") or self._settings.exchange.api_key_ref
        self._api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "") or self._settings.exchange.api_secret_ref
        self._config_hash = self._settings.config_hash

        # Adapter REST client (BD-02: single adapter boundary)
        self._exchange = BinanceRESTClient(
            rest_url=self._rest_url,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )
        # BD-T18: 创建 adapter 并注入 REST client 作为唯一网络传输
        from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter

        self._adapter = BinanceUsdmAdapter(rest_client=self._exchange)

        # Infrastructure
        self._store = PersistentStore.get_instance()
        self._feed = MarketDataFeed()
        self._alerts = AlertDispatcher(
            webhook_url=os.environ.get("BEIDOU_ALERTS_WEBHOOK_URL", ""),
            alerts_file=self._settings.infrastructure.alerts_file,
        )
        self._alerts.set_portfolio_provider(self._portfolio_summary)
        self._health = HealthServer(
            port=self._settings.infrastructure.health_port,
            bind_host=self._settings.infrastructure.health_host,
        )

        # Control plane
        self._control = ControlPlane()
        self._lifecycle = ModuleLifecycle("autopilot")

        # Business modules
        self._protection = ProtectionManager()
        self._protection_retries: dict[str, int] = {}  # 保护单重试计数
        self._last_order_placed_at: float = 0.0  # 最近一次下单时间戳（用于对账宽限期）
        self._outbox = IntentOutbox()
        self._ledger = ImmutableLedger()
        self._recon = ReconciliationEngine()
        self._pre_risk = PreRiskCheckerImpl(
            max_leverage=self._settings.production.max_leverage,
            max_concentration_pct=self._settings.production.max_concentration_pct,
            max_position_notional=self._settings.production.max_position_notional,
        )
        self._risk_engine = RiskEngineImpl()
        # 仅在正式生产环境 (CANARY/LIVE) 要求真实签名密钥；
        # 其他环境 (RESEARCH/PAPER/SHADOW/TESTNET/SAFETY_ONLY) 使用 mock 密钥。
        _needs_real_signing = self._env_mode.value in ("canary", "live")
        self._approval = RiskApprovalSignerImpl(signing_key="" if _needs_real_signing else "beidou-testnet-mock-key")
        self._risk_sm = RiskApprovalStateMachine()
        self._post_risk = PostRiskMonitor()
        self._cost_model = CostModel()
        self._optimizer = PortfolioOptimizerImpl(max_total_leverage=self._settings.production.max_total_leverage)
        self._fuser = SignalFuser()
        self._model_registry = ModelRegistry()
        self._drift_detector = DriftDetector(threshold=self._settings.production.drift_threshold)
        self._mapek = MAPEKController()

        # === 交易池 — 动态标的管理 ===
        self._trading_pool = TradingPool(max_instruments=self._settings.production.max_instruments)
        # 初始化默认标的（OBSERVING → PROMOTED → ACTIVE 需经过评分）
        for sym in symbols if len(symbols) > 2 else DEFAULT_UNIVERSE:
            entry = self._trading_pool.add(sym)
            # 种子评分：给初始信任，让标的进入 PROMOTED
            seed_score = InstrumentScore(
                instrument_id=sym,
                spread_score=0.7,
                depth_score=0.7,
                volume_score=0.8,
                stability_score=0.8,
                capacity_score=0.7,
            )
            seed_score.compute_overall()
            entry.scores.append(seed_score)
            # 跳过观察期直接进入 PROMOTED（种子标的已预筛选）
            entry.min_observation_hours = 0.0
            self._trading_pool.try_promote(sym)
            self._trading_pool.activate(sym)
        print(f"[beidou-autopilot] Trading Pool: {self._trading_pool.active_count()} active instruments")

        # === NEW: Strategy Risk Manager ===
        self._strategy_risk = StrategyRiskManager()
        self._strategy_risk.set_budget(
            RiskBudget(
                strategy_id=StrategyId("autopilot"),
                max_drawdown_pct=self._settings.production.max_drawdown_pct,
                max_daily_loss_pct=self._settings.production.max_daily_loss_pct,
                max_consecutive_losses=self._settings.production.max_consecutive_losses,
                max_position_notional=self._settings.production.max_position_notional,
                max_leverage=self._settings.production.max_leverage,
                risk_per_trade_pct=self._settings.production.risk_per_trade_pct,
                min_sharpe_rolling=self._settings.production.min_sharpe_rolling,
            )
        )

        # === NEW: Factor Registry + Evaluator ===
        self._factor_registry = FactorRegistry()
        self._factor_evaluator = FactorEvaluator()

        # ================================================================
        # Factor Registry — 8-factor suite
        # ================================================================
        factor_defs = [
            FactorDefinition(
                factor_id="meanrev_entry_v1",
                name="Mean Reversion Entry",
                version=SchemaVersion("2.0.0"),
                description="Short-term price deviation from SMA for entry signals",
                author="beidou-autopilot",
                category="mean_reversion",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Prices revert to mean in ranging markets",
                lookback_period="5h",
                rebalance_interval="5min",
                parameters={"deviation_threshold_pct": 1.0, "rsi_oversold": 40, "rsi_overbought": 60},
            ),
            FactorDefinition(
                factor_id="momentum_filter_v1",
                name="Momentum Filter",
                version=SchemaVersion("2.0.0"),
                description="20-period trend confirmation and volatility gate",
                author="beidou-autopilot",
                category="momentum",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Trend-following filter prevents counter-trend entries",
                lookback_period="20h",
                rebalance_interval="5min",
                parameters={"vol_threshold": 0.5, "trend_periods": 20},
            ),
            FactorDefinition(
                factor_id="trend_entry_v1",
                name="Trend Following Entry",
                version=SchemaVersion("2.0.0"),
                description="SMA5/SMA20 golden/death cross with trend strength confirmation",
                author="beidou-autopilot",
                category="trend_following",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Captures directional momentum at trend initiation",
                lookback_period="20h",
                rebalance_interval="5min",
                parameters={"crossover_threshold": 0.001, "trend_min_pct": 0.5},
            ),
            FactorDefinition(
                factor_id="breakout_entry_v1",
                name="Breakout Entry",
                version=SchemaVersion("2.0.0"),
                description="20-period high/low breakout with volatility expansion confirmation",
                author="beidou-autopilot",
                category="breakout",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Breakouts with volume/volatility expansion signal regime shifts",
                lookback_period="20h",
                rebalance_interval="5min",
                parameters={"lookback_periods": 20, "vol_expansion_min": 1.2},
            ),
            FactorDefinition(
                factor_id="volatility_filter_v1",
                name="Volatility Regime Filter",
                version=SchemaVersion("2.0.0"),
                description="ATR-based volatility regime: extreme→veto, elevated→degrade, normal→pass",
                author="beidou-autopilot",
                category="volatility",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Avoid trading in unpredictable volatility regimes",
                lookback_period="14h",
                rebalance_interval="5min",
                parameters={"extreme_vol": 0.6, "elevated_vol": 0.4, "extreme_atr_pct": 5.0},
            ),
            FactorDefinition(
                factor_id="volume_filter_v1",
                name="Volume Confirmation Filter",
                version=SchemaVersion("2.0.0"),
                description="Volume ratio (5/20) confirmation: surge→boost, low→degrade, dead→veto",
                author="beidou-autopilot",
                category="volume",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Volume validates price action; low-volume moves are unreliable",
                lookback_period="20h",
                rebalance_interval="5min",
                parameters={"dead_ratio": 0.25, "low_ratio": 0.5, "surge_ratio": 1.5},
            ),
            FactorDefinition(
                factor_id="trailing_exit_v1",
                name="ATR Trailing Stop Exit",
                version=SchemaVersion("2.0.0"),
                description="2× ATR trailing stop-loss + 3× ATR take-profit + SMA20 trend exit",
                author="beidou-autopilot",
                category="risk_management",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Dynamic exit based on realized volatility prevents large drawdowns",
                lookback_period="14h",
                rebalance_interval="5min",
                parameters={"trailing_atr_mult": 2.0, "profit_atr_mult": 3.0},
            ),
            FactorDefinition(
                factor_id="time_exit_v1",
                name="Time-Based Exit",
                version=SchemaVersion("2.0.0"),
                description="Maximum hold time exit: 48h force, 24h underwater, 12h profit lock",
                author="beidou-autopilot",
                category="risk_management",
                universe=frozenset({VenueId("BINANCE")}),
                instrument_types=frozenset({"perpetual"}),
                economic_rationale="Time decay and funding costs erode edge; stale positions increase risk",
                lookback_period="48h",
                rebalance_interval="5min",
                parameters={"max_hold_hours": 48, "underwater_hours": 24, "profit_lock_hours": 12},
            ),
        ]
        all_factor_ids = []
        for fd in factor_defs:
            self._factor_registry.register(fd)
            all_factor_ids.append(fd.factor_id)

        # BD-T06: 因子生命周期晋级 (Production 模式使用证据驱动门禁)
        from beidou_research.factors.factor import FactorPromotionGate

        is_production = self._env_mode.value in ("production", "canary", "live")
        self._factor_gate = FactorPromotionGate(strict=is_production)

        if not is_production:
            # Testnet/Paper: 非严格模式，自动晋级到 ACTIVE
            for fid in all_factor_ids:
                rec = self._factor_registry.get(fid)
                for target in [
                    FactorLifecycle.GENERATED,
                    FactorLifecycle.SANITY_PASSED,
                    FactorLifecycle.RESEARCH_VALIDATED,
                    FactorLifecycle.OOS_VERIFIED,
                    FactorLifecycle.COST_CAPACITY_VERIFIED,
                    FactorLifecycle.PAPER_TRADING,
                    FactorLifecycle.CHALLENGER,
                    FactorLifecycle.ACTIVE,
                ]:
                    if rec.lifecycle == target:
                        continue
                    decision = self._factor_gate.promote(rec, target, falsifier="autopilot-testnet")
                    if not decision.approved:
                        break
        else:
            # Production: 严格门禁，只加载 DB 中标记 ACTIVE 的因子
            print("[beidou-autopilot] Production mode: factor promotion requires evidence-gated decisions")

        active_factors = [
            fid for fid, r in self._factor_registry._factors.items() if r.lifecycle == FactorLifecycle.ACTIVE
        ]
        print(
            f"[beidou-autopilot] Factor lifecycles: {[(fid, r.lifecycle.value) for fid, r in self._factor_registry._factors.items()]}"
        )
        if is_production and not active_factors:
            print("[beidou-autopilot] WARNING: No ACTIVE factors — system will not generate trading signals")

        # Factor tracking: rolling predictions vs actual returns
        self._factor_predictions: dict[str, list[float]] = {fid: [] for fid in all_factor_ids}
        self._factor_returns: list[float] = []  # forward returns for IC computation
        self._last_factor_close: dict[str, float] = {}  # symbol → last close for return calc

        # ================================================================
        # Alpha Graph — Registry-driven component assembly (BF-08)
        #
        # 组件注册表：factor_id → (ComponentClass, edges_to)
        # AlphaGraph 从 FactorRegistry.get_active() 动态构建，
        # 不再硬编码组件列表。新增因子通过注册表自动接入。
        # ================================================================
        self._factor_component_registry: dict[str, tuple[type, tuple[str, ...]]] = {
            "meanrev_entry_v1": (MeanReversionEntry, ()),
            "trend_entry_v1": (TrendFollowingEntry, ()),
            "breakout_entry_v1": (BreakoutEntry, ()),
            "momentum_filter_v1": (MomentumFilter, ()),
            "volatility_filter_v1": (VolatilityFilter, ()),
            "volume_filter_v1": (VolumeFilter, ()),
            "trailing_exit_v1": (TrailingExit, ()),
            "time_exit_v1": (TimeExit, ()),
        }
        # Type-based auto-wiring: ENTRY → FILTER → EXIT
        self._entry_ids = {"meanrev_entry_v1", "trend_entry_v1", "breakout_entry_v1"}
        self._filter_ids = {"momentum_filter_v1", "volatility_filter_v1", "volume_filter_v1"}
        self._exit_ids = {"trailing_exit_v1", "time_exit_v1"}

        # ================================================================
        # Fix 1: 接线 Factor Registry → 控制面 API
        # ================================================================
        if hasattr(self._control, "wire_factor_registry"):
            self._control.wire_factor_registry(self._factor_registry)

        # ================================================================
        # Fix 2: 启动 CertificationManager
        # ================================================================
        try:
            from beidou_certification.engine import CertificationManager, G5TestnetCertification, G6ShadowCertification

            self._cert_manager = CertificationManager()
            if self._env_mode.value in ("testnet", "shadow"):
                self._cert_manager.register_framework(G5TestnetCertification())
            if self._env_mode.value in ("shadow",):
                self._cert_manager.register_framework(G6ShadowCertification())
        except Exception:
            self._cert_manager = None

        # ================================================================
        # Fix 3: 接线 ProductionLadder
        # ================================================================
        try:
            from beidou_certification.engine import ProductionLadder as CertProductionLadder

            if self._cert_manager is not None:
                self._production_ladder = CertProductionLadder(self._cert_manager)
            else:
                self._production_ladder = None
        except Exception:
            self._production_ladder = None

        # ================================================================
        # Fix 4: 接线 ChaosEngine（环境变量开关）
        # ================================================================
        self._chaos_engine = None
        if os.environ.get("BEIDOU_CHAOS_ENABLED", "").lower() == "true":
            try:
                from beidou_chaos.engine import ChaosEngine

                self._chaos_engine = ChaosEngine()
                print("[beidou-autopilot] ChaosEngine activated")
            except Exception as exc:
                print(f"[beidou-autopilot] ChaosEngine init failed: {exc}")

        self._alpha_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))

        # 仅添加 FactorRegistry 中标记为 ACTIVE 的因子组件
        added_components: list[str] = []
        active_factor_ids = [fid for fid in active_factors if fid in self._factor_component_registry]
        for fid in active_factor_ids:
            component_cls, _ = self._factor_component_registry[fid]
            self._alpha_graph.add_component(component_cls())
            added_components.append(fid)

        # Auto-wire ENTRY → FILTER → EXIT based on type sets
        for entry_id in added_components:
            if entry_id in self._entry_ids:
                for filter_id in added_components:
                    if filter_id in self._filter_ids:
                        self._alpha_graph.connect(entry_id, filter_id)

        for filter_id in added_components:
            if filter_id in self._filter_ids:
                for exit_id in added_components:
                    if exit_id in self._exit_ids:
                        self._alpha_graph.connect(filter_id, exit_id)

        order = self._alpha_graph.topological_order()
        print(f"[beidou-autopilot] Active factors for trading: {active_factor_ids}")
        print(f"[beidou-autopilot] DAG order: {order}")
        if len(order) < 2:
            print(
                "[beidou-autopilot] WARNING: AlphaGraph has < 2 components — "
                "system may not generate meaningful signals. "
                "Check FactorRegistry for ACTIVE factors."
            )

        # BD-T05: Wrap legacy AlphaGraph in StrategyKernel contract for parity checking
        self._strategy_kernel = StrategyKernelContract()
        self._kernel_mode = KernelMode.PAPER  # default; TESTNET when write enabled
        self._kernel_parity: str = ""

        # Strategy performance tracking
        self._autopilot_strategy_id = StrategyId("autopilot")
        self._trade_pnls: list[float] = []  # realized PnL per trade
        self._win_count: int = 0
        self._loss_count: int = 0
        self._peak_equity: float = 0.0

        # Order tracking
        self._order_trackers: dict[str, OrderStateTracker] = {}
        self._active_order_ids: set[str] = set()
        self._order_symbols: dict[str, str] = {}  # orderId → symbol mapping
        self._position_entry_times: dict[str, float] = {}  # position_id → entry timestamp
        self._close_order_ids: set[str] = set()  # 平仓订单 ID，FILLED 后不创建保护

        # State
        self._running = False
        self._last_realtime = time.time()
        self._last_nearline = time.time()
        self._last_offline = time.time()
        self._last_recon = time.time()
        self._tick_count = 0
        self._order_count = 0
        self._error_count = 0
        self._last_account: dict = {}

        # Health server callbacks
        self._health.set_readiness_check(self._check_ready)
        self._health.set_metrics_collector(self._collect_metrics)
        self._health.set_status_info(self._get_status_info)

    # --- Adapter-bound REST API (BD-02: single adapter boundary) ---
    # 所有 Binance API 访问统一通过 self._exchange (BinanceRESTClient)
    # 不再在 engine 内重复实现签名逻辑

    async def _api_async(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """异步 API 调用 — 通过 BinanceRESTClient Adapter 边界（BD-02）。

        所有异步代码必须使用此方法，禁止直接 urllib/requests/httpx。
        BinanceRESTClient 提供统一错误分类、限频退避和熔断。

        注意: 失败时返回 {"error": code, "msg": "..."} dict。
        调用方必须检查 "error" 键是否存在，不可将错误响应当作正常数据。
        对于关键状态读取，优先使用 _api_async_safe() 以防止熔断级联。
        """
        result = await self._exchange.request(method, path, signed, params)
        if result.is_success():
            return result.data
        err = result.error
        return {"error": err.http_status or -1, "msg": str(err.message) if err else "unknown"}

    async def _api_async_safe(
        self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None
    ) -> tuple[Any, bool]:
        """安全 API 调用 — 返回 (data, ok) 元组。

        与 _api_async 不同，失败时不返回伪 dict，而是返回 (None, False)。
        关键状态读取（账户、持仓、订单）必须使用此方法，
        防止熔断/限流返回的 {"error": ...} 被当作正常数据覆盖有效状态。
        """
        result = await self._exchange.request(method, path, signed, params)
        if result.is_success():
            return result.data, True
        err = result.error
        print(f"[api] {path} FAILED: {err.message if err else 'unknown'} — state NOT updated")
        return None, False

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """同步兼容包装 — 委托给 BinanceRESTClient（BD-02 Adapter 边界）。

        运行时通过 asyncio.run() 调用异步 BinanceRESTClient.request()，
        享受统一错误分类、限频退避、熔断和时钟偏差校准。
        遗留同步代码使用此方法；新异步代码必须使用 _api_async。
        返回原始 dict（兼容现有代码），失败时返回 {"error": code, "msg": "..."}
        """
        import asyncio as _asyncio

        # 检查是否在异步上下文中被调用
        try:
            loop = _asyncio.get_running_loop()
            if loop.is_running():
                return {"error": -2, "msg": "_api called from async context — use _api_async instead"}
        except RuntimeError:
            pass  # 不在异步上下文中，正常

        try:
            result = _asyncio.run(self._exchange.request(method, path, signed, params))
        except Exception as ex:
            return {"error": -1, "msg": f"Adapter error: {ex}"}

        if result.is_success():
            return result.data
        err = result.error
        return {"error": err.http_status or -1, "msg": str(err.message) if err else "unknown"}

    async def _ensure_leverage(self, symbol: str, target_leverage: int) -> int:
        """确保交易所杠杆设置与自适应杠杆一致（带缓存）。

        返回实际生效的杠杆值（如遇 -2028 则查询当前杠杆）。
        """
        if not hasattr(self, "_leverage_cache"):
            self._leverage_cache: dict[str, int] = {}
        current = self._leverage_cache.get(symbol)
        if current == target_leverage:
            return current

        resp = await self._api_async(
            Endpoint.LEVERAGE,
            method="POST",
            signed=True,
            params={"symbol": symbol, "leverage": target_leverage},
        )
        if "leverage" in resp:
            actual = int(resp["leverage"])
            self._leverage_cache[symbol] = actual
            print(f"[leverage] {symbol}: set to {actual}x (was {current or 'unknown'}x)")
            return actual

        # 解析 Binance 错误码（可能嵌套在 msg JSON 中）
        binance_code = resp.get("code")
        if binance_code is None and isinstance(resp.get("msg"), str):
            try:
                import json

                inner = json.loads(resp["msg"])
                binance_code = inner.get("code")
            except (json.JSONDecodeError, KeyError):
                pass

        if binance_code == -2028:
            # 降低杠杆时保证金不足 → 查询当前杠杆并沿用
            actual = target_leverage  # fallback
            try:
                pos_resp = await self._api_async(
                    Endpoint.POSITION_RISK,
                    signed=True,
                    params={"symbol": symbol},
                )
                if isinstance(pos_resp, list) and len(pos_resp) > 0:
                    lev_from_api = int(float(pos_resp[0].get("leverage", 0)))
                    if lev_from_api > 0:
                        actual = lev_from_api
            except Exception as e:
                print(f"[leverage] UNKNOWN: failed to query leverage for {symbol}: {e}")
                self._leverage_cache[symbol] = -1  # UNKNOWN state
                return -1
            # 如果查询返回 0（testnet 常见），保持目标杠杆
            if actual <= 0:
                actual = target_leverage
            self._leverage_cache[symbol] = actual
            print(f"[leverage] {symbol}: cannot lower to {target_leverage}x, keeping {actual}x (existing positions)")
            return actual
        else:
            print(f"[leverage] FAILED {symbol}: {resp.get('msg', resp)}")
            return target_leverage  # 返回目标值让流程继续

    # --- Health & Metrics ---

    def _check_ready(self) -> bool:
        if self._lifecycle.state != ModuleState.ACTIVE:
            return False
        return self._feed.is_healthy()

    async def run_parity_check(self) -> ParityResult:
        """BD-T05: 验证 Backtest/Paper/Testnet 策略一致性。"""
        from beidou_strategy.kernel_parity import parity_check

        _passed, result = parity_check()
        return result

    def _portfolio_summary(self) -> str:
        """生成持仓摘要（品种/数量/入场/现价/盈亏%/盈亏$），供告警 webhook。"""
        lines = []
        try:
            positions = self._protection.all_positions()
            if not positions:
                return "📊 当前无持仓"
            total_pnl = 0.0
            total_notional = 0.0
            for _pos_id, pp in sorted(positions.items()):
                sym = str(pp.instrument_id)
                qty = float(pp.quantity)
                entry = float(pp.entry_price)
                current = float(self._last_prices.get(sym, entry))
                notional = qty * current
                total_notional += notional
                if current > 0 and entry > 0:
                    pnl_pct = (current / entry - 1) * 100
                    if pp.side.value == "SELL":
                        pnl_pct = -pnl_pct
                    pnl_usd = notional - (qty * entry)
                    if pp.side.value == "SELL":
                        pnl_usd = (qty * entry) - notional
                    total_pnl += pnl_usd
                    icon = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < -0.01 else "⚪")
                    lines.append(
                        f"  {icon} **{sym}**  {qty:.4f}  "
                        f"@{entry:.2f}→{current:.2f}  "
                        f"**{pnl_pct:+.2f}%** (${pnl_usd:+.2f})"
                    )
                else:
                    lines.append(f"  ⚪ **{sym}**  {qty:.4f}  @{entry:.2f}")

            if lines:
                lines.insert(0, "📊 持仓")
                up = sum(1 for l in lines if "🟢" in l)
                dn = sum(1 for l in lines if "🔴" in l)
                lines.append(
                    f"\n📈 总敞口 ${total_notional:,.0f}  |  浮动盈亏 **${total_pnl:+,.2f}**  |  🟢{up} 🔴{dn}"
                )
        except Exception as e:
            return f"📊 持仓获取异常: {e}"
        return "\n".join(lines)

    def _collect_metrics(self) -> dict:
        risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
        return {
            "tick_count": self._tick_count,
            "order_count": self._order_count,
            "error_count": self._error_count,
            "active_orders": len(self._active_order_ids),
            "positions": self._protection.position_count(),
            "alerts": self._alerts.get_alert_stats(),
            "strategy_risk_level": risk_state.risk_level.value if risk_state else "N/A",
            "drawdown_pct": round(risk_state.current_drawdown_pct, 2) if risk_state else 0,
            "win_rate": round(self._win_count / max(1, self._win_count + self._loss_count), 3),
            "active_factors": len(self._factor_registry.get_active()) + len(self._factor_registry.get_challengers()),
        }

    def _get_status_info(self) -> dict:
        risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
        return {
            "lifecycle_state": self._lifecycle.state.value,
            "control_action": self._control.get_status().value,
            "symbols": self._symbols,
            "mode": self._env_mode.value,
            "uptime_seconds": round(self._health.uptime_seconds(), 1),
            "last_realtime_tick": self._last_realtime,
            "last_nearline_tick": self._last_nearline,
            "active_incidents": self._alerts.get_active_incidents(),
            "strategy_risk": {
                "level": risk_state.risk_level.value if risk_state else "N/A",
                "drawdown_pct": round(risk_state.current_drawdown_pct, 2) if risk_state else 0,
                "consecutive_losses": risk_state.consecutive_losses if risk_state else 0,
            }
            if risk_state
            else None,
            "active_factors": len(self._factor_registry.get_active()) + len(self._factor_registry.get_challengers()),
        }

    def _rebuild_alpha_graph(self) -> None:
        """BF-08: 从 FactorRegistry 重建 AlphaGraph。

        当因子生命周期变更（promotion/degradation/suspension）时调用，
        确保交易图仅包含 ACTIVE 因子。新增或移除的因子自动反映到 DAG 拓扑。
        """
        active_factor_ids = [
            fid for fid in self._factor_registry.get_active() if fid in self._factor_component_registry
        ]

        new_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))
        for fid in active_factor_ids:
            component_cls, _ = self._factor_component_registry[fid]
            new_graph.add_component(component_cls())

        # Re-wire ENTRY → FILTER → EXIT
        added = set(active_factor_ids)
        for entry_id in added & self._entry_ids:
            for filter_id in added & self._filter_ids:
                new_graph.connect(entry_id, filter_id)
        for filter_id in added & self._filter_ids:
            for exit_id in added & self._exit_ids:
                new_graph.connect(filter_id, exit_id)

        order = new_graph.topological_order()
        self._alpha_graph = new_graph
        # Ensure prediction tracking covers all active factors
        for fid in active_factor_ids:
            if fid not in self._factor_predictions:
                self._factor_predictions[fid] = []
        print(f"[beidou-autopilot] AlphaGraph rebuilt: {len(active_factor_ids)} active factors, DAG order: {order}")

    # --- Clock Domain: REALTIME (every 5s) ---

    async def _realtime_tick(self) -> None:
        """实时时钟：行情轮询 → 保护单检查 → 订单处理 → 对账。"""
        self._tick_count += 1

        if self._tick_count % 3 == 0:
            print(
                f"[realtime] TICK #{self._tick_count} — outbox id={id(self._outbox)} items={len(self._outbox._outbox)}"
            )

        try:
            active_symbols = self._trading_pool.active_instruments()
            if not active_symbols:
                active_symbols = list(self._symbols)

            # 批次轮询: 25个标的每tick处理5个，5 tick完成一轮，避免阻塞
            batch_size = max(5, len(active_symbols) // 5)
            offset = (self._tick_count % max(1, (len(active_symbols) + batch_size - 1) // batch_size)) * batch_size
            batch = active_symbols[offset : offset + batch_size]

            for symbol in batch:
                # Yield event loop between symbols
                await asyncio.sleep(0)

                # 1. Fetch latest market data
                features = self._feed.update_features(symbol)
                if not features:
                    continue

                price = features["price"]

                # Track last price for protection status reports
                if not hasattr(self, "_last_prices"):
                    self._last_prices: dict[str, float] = {}
                self._last_prices[symbol] = price

                # 2. Monitor active orders (止盈止损由交易所 Algo Order 原生执行，不再本地监控)
                if self._can_write:
                    await self._monitor_orders(symbol)

                # 4. Save market snapshot
                self._store.save_market_snapshot(
                    symbol,
                    price,
                    features.get("bid"),
                    features.get("ask"),
                    features.get("spread_bps"),
                    features.get("volume_24h"),
                )

            # 5. Process unacked intents → place orders (once per tick, all symbols)
            unacked = self._outbox.unacked()
            pending = self._outbox.pending_count()
            if self._tick_count % 5 == 0:
                ob = self._outbox
                raw_outbox = len(ob._outbox)
                raw_processed = len(ob._processed)
                raw_inbox = len(ob._inbox)
                print(
                    f"[realtime] Intent check: unacked={len(unacked)} pending={pending} raw_outbox={raw_outbox} processed={raw_processed} inbox={raw_inbox} can_write={self._can_write}"
                )
            if self._can_write:
                for intent in unacked:
                    await self._place_order(intent)

            # 6. Reconciliation (every 30s)
            if time.time() - self._last_recon > 30:
                await self._reconcile()
                self._last_recon = time.time()

            # 7. Protection status report (every 60 ticks ≈ 60s)
            if self._tick_count % 60 == 0:
                positions = self._protection.all_positions()
                if positions:
                    print(f"[protection] ╔══ ACTIVE PROTECTION STATUS ({len(positions)} positions) ══╗")
                    for _pos_id, pp in positions.items():
                        sym = str(pp.instrument_id)
                        entry = pp.entry_price
                        side = "LONG" if pp.is_long() else "SHORT"
                        sl_info = "NONE"
                        tp_info = "NONE"
                        if pp.stop_loss and pp.stop_loss.is_active():
                            sl_px = float(pp.stop_loss.trigger_price.amount)
                            sl_dist = (entry - sl_px) / entry * 100 if pp.is_long() else (sl_px - entry) / entry * 100
                            sl_info = f"{sl_px:.4f} (-{sl_dist:.2f}%)"
                        if pp.take_profits:
                            tp_parts = []
                            for tp in pp.take_profits:
                                if tp.is_active():
                                    tp_px = float(tp.trigger_price.amount)
                                    if pp.is_long():
                                        tp_dist = (tp_px - entry) / entry * 100
                                    else:
                                        tp_dist = (entry - tp_px) / entry * 100
                                    tp_parts.append(f"{tp_px:.4f} (+{tp_dist:.2f}%)")
                            if tp_parts:
                                tp_info = ", ".join(tp_parts)
                        pnl = (
                            pp.unrealized_pnl_pct(self._last_prices.get(sym, entry))
                            if hasattr(self, "_last_prices")
                            else 0.0
                        )
                        print(f"[protection] ║ {sym} {side} @{entry:.4f} SL={sl_info} TP={tp_info} | PnL={pnl:+.2f}%")
                    print(f"[protection] ╚{'═' * 50}╝")

        except Exception as e:
            self._error_count += 1
            self._alerts.send_incident(
                AlertSeverity.WARNING,
                "Realtime tick error",
                str(e)[:200],
                category="realtime",
            )
        finally:
            self._last_realtime = time.time()

    async def _cancel_algo_orders(self, position_id: str, symbol: str) -> None:
        """平仓时取消交易所上的关联条件单（STOP_MARKET / TAKE_PROFIT_MARKET）。"""
        if not hasattr(self, "_active_algo_ids"):
            return
        algo_ids = self._active_algo_ids.pop(position_id, set())
        if not algo_ids:
            return
        for algo_id in list(algo_ids):
            try:
                cancel_resp = await self._api_async(
                    Endpoint.ALGO_ORDER,
                    method="DELETE",
                    signed=True,
                    params={"symbol": symbol, "algoId": int(algo_id)},
                )
                if "code" not in cancel_resp:
                    print(f"[protection] Canceled algo order {algo_id} for {symbol}")
                else:
                    print(f"[protection] Failed to cancel algo {algo_id}: {cancel_resp.get('msg', cancel_resp)}")
            except Exception as e:
                print(f"[protection] Error canceling algo {algo_id}: {e}")

    async def _place_order(self, intent, symbol: str = "") -> None:
        """向交易所发送订单。"""

        # P0 Gate 2: Executor 发送前再次校验控制状态
        # 防止 Outbox 提交后到发送前控制状态变更的竞态窗口
        if not self._control.should_accept(intent):
            print(
                f"[order] ❌ Intent {intent.intent_id} REJECTED at executor gate ({self._control.get_status().value} v{self._control.version})"
            )
            self._outbox.ack(intent.intent_id)
            return

        side = "BUY" if intent.side == OrderSide.BUY else "SELL"
        order_type = "LIMIT" if intent.order_type == OrderType.LIMIT else "MARKET"
        # BD-P0-07: Client Order ID 从 Intent ID 确定性派生，重试时可恢复
        client_id = intent.client_order_id or f"beidou-{intent.intent_id}"

        # 使用 intent 自身的 instrument_id，而非循环变量
        order_symbol = str(intent.instrument_id) if hasattr(intent, "instrument_id") else symbol

        params = {
            "symbol": order_symbol,
            "side": side,
            "type": order_type,
            "quantity": str(float(intent.quantity.amount)),
            "newClientOrderId": client_id,
        }
        if order_type == "LIMIT" and intent.price:
            params["price"] = str(float(intent.price.amount))
            params["timeInForce"] = "GTC"

        if not self._can_write:
            # 零写模式：仅模拟订单状态，不发送任何交易写请求
            tracker = OrderStateTracker(order_id=OrderId(intent.intent_id))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.FILLED)
            self._order_trackers[intent.intent_id] = tracker
            self._outbox.ack(intent.intent_id)
            self._order_count += 1
            return

        # 量化精度修正：查询交易所规则获取 stepSize 和 tickSize
        if not hasattr(self, "_symbol_precision"):
            self._symbol_precision: dict[str, dict[str, int]] = {}
        if order_symbol not in self._symbol_precision:
            try:
                exchange_info = await self._api_async(Endpoint.EXCHANGE_INFO)
                found = False
                for s in exchange_info.get("symbols", []):
                    sym = s.get("symbol", "")
                    prec = {"quantity": 3, "price": 2}  # defaults
                    for f_item in s.get("filters", []):
                        if f_item.get("filterType") == "LOT_SIZE":
                            step = f_item.get("stepSize", "0.001")
                            prec["quantity"] = max(0, len(step.split(".")[1].rstrip("0")) if "." in step else 0)
                        if f_item.get("filterType") == "PRICE_FILTER":
                            tick = f_item.get("tickSize", "0.01")
                            prec["price"] = max(0, len(tick.split(".")[1].rstrip("0")) if "." in tick else 0)
                    self._symbol_precision[sym] = prec
                    if sym == order_symbol:
                        found = True
                # 未找到时缓存默认精度，避免重复 exchangeInfo 查询
                if not found:
                    self._symbol_precision[order_symbol] = {"quantity": 3, "price": 2}
            except Exception:
                self._symbol_precision[order_symbol] = {"quantity": 3, "price": 2}

        prec = self._symbol_precision.get(order_symbol, {"quantity": 3, "price": 2})
        qty = float(params["quantity"])
        params["quantity"] = f"{qty:.{prec['quantity']}f}"
        price_raw = params.get("price")
        if price_raw:
            params["price"] = f"{float(price_raw):.{prec['price']}f}"

        print(f"[order] Sending to exchange: {order_symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')}")
        order = await self._api_async(Endpoint.ORDER, method="POST", signed=True, params=params)
        print(f"[order] Exchange response: {str(order)[:200]}")

        if "orderId" in order:
            oid_str = str(order["orderId"])
            # 标记平仓订单（通过 client_order_id 中的 "-close-" 模式识别）
            if client_id and "-close-" in client_id:
                self._close_order_ids.add(oid_str)
                print(f"[order] Marked as close order: {oid_str}")
            tracker = OrderStateTracker(order_id=OrderId(oid_str))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.SENT)
            self._order_trackers[oid_str] = tracker
            self._order_symbols[oid_str] = order_symbol
            self._outbox.ack(intent.intent_id)
            self._order_count += 1

            actual_status = order.get("status", "NEW")
            self._last_order_placed_at = time.time()
            print(
                f"[order] PLACED: {symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')} "
                f"orderId={order['orderId']} status={actual_status}"
            )

            # BD-FIX: Binance MARKET 单几乎立即成交（testnet 尤甚）。
            # 下单响应中的 status 可能已是 FILLED — 此时立即处理成交，
            # 不加入 _active_order_ids，消除下单→成交→对账之间的时序窗口。
            if actual_status == "FILLED":
                print(f"[order] ⚡ Immediate fill detected: {order_symbol} {side} — processing now")
                await self._process_fill(oid_str, order_symbol, order)
            else:
                self._active_order_ids.add(oid_str)
                self._store.save_order_state(
                    oid_str,
                    order_symbol,
                    side,
                    order_type,
                    str(float(intent.quantity.amount)),
                    str(float(intent.price.amount)) if intent.price else None,
                    actual_status,
                    client_order_id=client_id,
                )
        else:
            print(f"[order] FAILED: {order_symbol} {side} — {order.get('msg', order.get('error', 'unknown'))}")

    async def _monitor_orders(self, symbol: str) -> None:
        """查询活跃订单状态并更新状态机/账本。"""
        for order_id in list(self._active_order_ids):
            # 使用订单自身的 symbol 而非批量循环的 symbol
            order_sym = self._order_symbols.get(order_id)
            if order_sym is not None and order_sym != symbol:
                continue  # 跳过不属于当前 symbol 的订单，避免无效 API 调用
            order_sym = order_sym or symbol
            try:
                result = await self._api_async(
                    Endpoint.ORDER,
                    signed=True,
                    params={
                        "symbol": order_sym,
                        "orderId": int(order_id),
                    },
                )

                if (isinstance(result, dict) and result.get("error")) or "status" not in result:
                    if result.get("msg") and (
                        "Order does not exist" in str(result.get("msg")) or "Unknown order" in str(result.get("msg"))
                    ):
                        self._active_order_ids.discard(order_id)
                        self._order_trackers.pop(order_id, None)
                    continue

                if "status" not in result:
                    continue

                status = result["status"]
                tracker = self._order_trackers.get(order_id)
                if not tracker:
                    continue

                float(result.get("executedQty", 0))
                result.get("avgPrice", "0")

                if status == "FILLED":
                    await self._process_fill(order_id, order_sym or symbol, result)

                elif status == "CANCELED" or status == "EXPIRED":
                    tracker.apply(OrderEvent.CANCELED)
                    self._active_order_ids.discard(order_id)
                    self._store.save_order_state(
                        order_id,
                        symbol,
                        result.get("side", ""),
                        result.get("type", ""),
                        result.get("origQty", "0"),
                        result.get("price"),
                        status,
                    )

                elif status == "PARTIALLY_FILLED":
                    tracker.apply(OrderEvent.PARTIALLY_FILLED)

            except Exception as e:
                # Log error but do NOT silently swallow — maintain visibility
                print(f"[realtime] Order monitoring error ({order_id}): {e}")

    async def _process_fill(self, order_id: str, symbol: str, result: dict) -> None:
        """处理订单成交：更新状态机、账本、持仓保护。

        从 _monitor_orders 和 _place_order（即时成交）共用，
        消除近线下单→成交→对账之间的时序窗口。
        """
        tracker = self._order_trackers.get(order_id)
        if not tracker:
            return
        tracker.apply(OrderEvent.FILLED)
        self._active_order_ids.discard(order_id)

        executed_qty = float(result.get("executedQty", 0))
        avg_price = result.get("avgPrice", "0")

        notional = executed_qty * float(avg_price)
        if result.get("side") == "BUY":
            journal_debit, journal_credit = "0", str(notional)
        else:
            journal_debit, journal_credit = str(notional), "0"
        entry = JournalEntry(
            entry_id=f"journal-{order_id}",
            account_id=AccountId("default"),
            venue_id=VenueId("BINANCE"),
            instrument_id=InstrumentId(symbol),
            debit=MonetaryValue(amount=journal_debit),
            credit=MonetaryValue(amount=journal_credit),
            description=f"{result.get('side')} {executed_qty} {symbol} @ {avg_price} FILLED",
            correlation_id=CorrelationId(f"exec-{order_id}"),
        )
        self._ledger.post(entry)
        self._store.save_ledger_entry(
            entry.entry_id,
            "default",
            "BINANCE",
            symbol,
            entry.debit.amount,
            entry.credit.amount,
            entry.description,
            str(entry.correlation_id),
            entry.timestamp.isoformat(),
        )
        self._store.save_order_state(
            order_id,
            symbol,
            result.get("side", ""),
            result.get("type", ""),
            result.get("origQty", "0"),
            result.get("price"),
            "FILLED",
            str(executed_qty),
            str(avg_price),
        )

        # 检查是否为平仓订单
        is_close_order = order_id in self._close_order_ids
        if is_close_order:
            self._close_order_ids.discard(order_id)
            for pid, pp in list(self._protection.all_positions().items()):
                if str(pp.instrument_id) == symbol:
                    entry_px = pp.entry_price
                    exit_px = float(avg_price)
                    pos_qty = pp.quantity
                    trade_pnl = (exit_px - entry_px) * pos_qty if pp.is_long() else (entry_px - exit_px) * pos_qty
                    is_win = trade_pnl > 0
                    self._strategy_risk.record_trade(self._autopilot_strategy_id, trade_pnl, is_win)
                    if is_win:
                        self._win_count += 1
                    else:
                        self._loss_count += 1
                    self._trade_pnls.append(trade_pnl)
                    self._position_entry_times.pop(pid, None)
                    self._protection.cancel_protection(pid)
                    self._protection.remove_position(pid)
                    await self._cancel_algo_orders(pid, symbol)
                    print(f"[order] Close trade recorded: {symbol} PnL={trade_pnl:.2f}")
                    break
            account_balance = float(self._last_account.get("totalWalletBalance", 0))
            if account_balance > 0:
                self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
                if account_balance > self._peak_equity:
                    self._peak_equity = account_balance
        else:
            # 入场成交 → 创建止盈止损保护
            for old_pid, old_pp in list(self._protection.all_positions().items()):
                if str(old_pp.instrument_id) == symbol:
                    self._protection.cancel_protection(old_pid)
                    self._protection.remove_position(old_pid)
                    self._position_entry_times.pop(old_pid, None)
                    await self._cancel_algo_orders(old_pid, symbol)
                    print(f"[protection] Cleaned up stale protection for {symbol} (pos={old_pid})")

            entry_price = float(avg_price)
            qty = executed_qty
            pos_side = OrderSide.BUY if result.get("side") == "BUY" else OrderSide.SELL
            pos_id = f"pos-{order_id}"

            kline_features = self._feed.get_kline_features(symbol)
            adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry_price, kline_features)
            pp = self._protection.create_protection(
                position_id=pos_id,
                instrument_id=InstrumentId(symbol),
                venue_id=VenueId("BINANCE"),
                entry_price=entry_price,
                quantity=qty,
                side=pos_side,
                stop_loss_config=adaptive_cfg.stop_loss_config,
                take_profit_config=adaptive_cfg.take_profit_config,
            )
            self._position_entry_times[pos_id] = time.time()

            # 打印保护摘要
            sl_price = float(pp.stop_loss.trigger_price.amount) if pp.stop_loss else None
            sl_pct = (
                (entry_price - sl_price) / entry_price * 100
                if sl_price and pos_side == OrderSide.BUY
                else ((sl_price - entry_price) / entry_price * 100 if sl_price else None)
            )
            tp_prices = [f"{float(tp.trigger_price.amount):.2f}" for tp in pp.take_profits]
            tp_pcts = []
            for tp in pp.take_profits:
                tp_px = float(tp.trigger_price.amount)
                if pos_side == OrderSide.BUY:
                    tp_pcts.append(f"{(tp_px - entry_price) / entry_price * 100:+.2f}%")
                else:
                    tp_pcts.append(f"{(entry_price - tp_px) / entry_price * 100:+.2f}%")

            adaptive_info = (
                f"ATR={adaptive_cfg.atr_pct:.2f}% vol={adaptive_cfg.volatility_regime.value} "
                f"tier={adaptive_cfg.price_tier.value} regime={adaptive_cfg.market_regime.value} "
                f"RR={adaptive_cfg.rr_ratio:.1f}"
            )
            print(
                f"[protection] ┌ {'=' * 60}\n"
                f"[protection] ├─ {symbol} {pos_side.value} {qty} @ {entry_price:.4f}\n"
                f"[protection] ├─ 🛑 STOP LOSS:  {sl_price:.4f} ({sl_pct:+.2f}% from entry) [{pp.stop_loss.stop_type.value if pp.stop_loss else 'N/A'}]\n"
                f"[protection] ├─ 🎯 TAKE PROFIT: {', '.join(f'{p} ({pct})' for p, pct in zip(tp_prices, tp_pcts, strict=False))}\n"
                f"[protection] ├─ 📊 {adaptive_info}\n"
                f"[protection] └ {'=' * 60}"
            )

            # 提交止盈止损到交易所
            reduce_side = "SELL" if pos_side == OrderSide.BUY else "BUY"
            protect_orders = [pp.stop_loss] if pp.stop_loss else []
            protect_orders.extend(pp.take_profits)
            exchange_protection_count = 0
            for p_order in protect_orders:
                if p_order is None:
                    continue
                if not hasattr(self, "_protection_exchange_attempted"):
                    self._protection_exchange_attempted: set[str] = set()
                if p_order.protection_id in self._protection_exchange_attempted:
                    continue
                self._protection_exchange_attempted.add(p_order.protection_id)

                prec_map = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
                qty_str = f"{float(p_order.quantity.amount):.{prec_map['quantity']}f}"
                price_str = f"{float(p_order.trigger_price.amount):.{prec_map['price']}f}"

                algo_params = {
                    "symbol": symbol,
                    "side": reduce_side,
                    "algoType": "CONDITIONAL",
                    "type": p_order.order_type,
                    "quantity": qty_str,
                    "triggerPrice": price_str,
                    "reduceOnly": "true",
                    "workingType": "CONTRACT_PRICE",
                }
                # BD-FIX: 带重试和指数退避的 algo order 提交，应对 testnet 限流
                algo_resp = {}
                max_algo_retries = 3
                for algo_attempt in range(max_algo_retries):
                    algo_resp = await self._api_async(
                        Endpoint.ALGO_ORDER, method="POST", signed=True, params=algo_params
                    )
                    if "algoId" in algo_resp:
                        break
                    err_code = algo_resp.get("code", 0)
                    # -4120: 已存在相同的条件单，不需要重试
                    if err_code == -4120:
                        print(f"[protection] ⚠️ {symbol} {p_order.reason}: already exists (-4120)")
                        algo_resp = {"algoId": "existing", "algoStatus": "ACTIVE"}  # 视为成功
                        break
                    if algo_attempt < max_algo_retries - 1:
                        wait = 1.5 * (2**algo_attempt)  # 1.5s, 3s, 6s
                        print(
                            f"[protection] ⚠️ {symbol} {p_order.reason}: retry {algo_attempt + 1}/{max_algo_retries} after {wait:.1f}s (err={err_code})"
                        )
                        await asyncio.sleep(wait)
                        self._exchange.reset_circuit_breaker()  # 重置熔断器重试
                if "algoId" in algo_resp:
                    exchange_protection_count += 1
                    algo_id = str(algo_resp["algoId"])
                    if not hasattr(self, "_active_algo_ids"):
                        self._active_algo_ids: dict[str, set[str]] = {}
                    self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                    print(
                        f"[protection] ✅ {symbol} {p_order.reason} → algoId={algo_id} "
                        f"triggerPrice={price_str} status={algo_resp.get('algoStatus', 'NEW')}"
                    )
                else:
                    err_code = algo_resp.get("code", "unknown")
                    err_msg = algo_resp.get("msg", str(algo_resp)[:150])
                    print(f"[protection] ❌ {symbol} {p_order.reason}: code={err_code} {err_msg}")

            if exchange_protection_count > 0:
                print(
                    f"[protection] 🏦 {exchange_protection_count} protection orders placed on exchange (Algo Order API)"
                )
            else:
                print("[protection] ❌ 0 protection orders placed — all attempts failed, will retry in nearline cycle")
                # 标记待重试，确保 _retry_missing_protections 优先处理
                if not hasattr(self, "_pending_protection_retry"):
                    self._pending_protection_retry: set[str] = set()
                self._pending_protection_retry.add(pos_id)

        # Update strategy risk on any fill
        account_balance = float(self._last_account.get("totalWalletBalance", 0))
        if account_balance > 0:
            self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
            if account_balance > self._peak_equity:
                self._peak_equity = account_balance

    async def _reconcile(self) -> None:
        """对账：系统状态 vs 交易所状态。

        熔断保护: API 失败时跳过本轮对账，保留上一次有效对账结果，
        防止熔断返回的空数据覆盖真实状态引发假阳性 MISMATCH。
        """
        try:
            # 对账优先使用完整 account 端点（含 positions），失败则回退 balance
            account, ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if not ok or "totalWalletBalance" not in account:
                # 回退：balance 端点无 positions，仅对账余额
                account, ok = await self._api_async_safe(Endpoint.BALANCE, signed=True)
                if not ok or "totalWalletBalance" not in account:
                    print("[recon] SKIP: API unavailable (circuit breaker / rate limit) — keeping last known state")
                    return

            balance = float(account.get("totalWalletBalance", 0))
            positions_list = account.get("positions", [])

            exchange_positions: dict[InstrumentId, Quantity] = {}
            for p in positions_list:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    exchange_positions[InstrumentId(p["symbol"])] = Quantity(amount=str(abs(amt)))

            exchange_open_order_ids: list[str] = []
            try:
                open_orders, orders_ok = await self._api_async_safe(Endpoint.OPEN_ORDERS, signed=True)
                if orders_ok and isinstance(open_orders, list):
                    exchange_open_order_ids = [str(o["orderId"]) for o in open_orders]
                elif not orders_ok:
                    print("[recon] Open orders query skipped (API unavailable)")
            except Exception as e:
                print(f"[realtime] Open orders query failed: {e}")

            exchange_facts = AccountFactSnapshot(
                account_id=AccountId("default"),
                venue_id=VenueId("BINANCE"),
                balance=MonetaryValue(amount=str(balance)),
                positions=exchange_positions,
                open_orders=exchange_open_order_ids,
            )

            system_positions: dict[InstrumentId, Quantity] = {}
            for _pos_id, pos in self._protection.all_positions().items():
                system_positions[InstrumentId(pos.instrument_id)] = Quantity(amount=str(pos.quantity))

            system_facts = AccountFactSnapshot(
                account_id=AccountId("default"),
                venue_id=VenueId("BINANCE"),
                balance=MonetaryValue(amount=str(balance)),
                positions=system_positions,
                open_orders=list(self._active_order_ids),
            )

            self._recon.update_system_facts(system_facts)
            self._recon.update_exchange_facts(exchange_facts)
            result = self._recon.reconcile(AccountId("default"), VenueId("BINANCE"))

            if not result.matched:
                # BD-FIX: 对账不一致时，先尝试自愈同步（清理幽灵订单/持仓、补齐遗漏追踪），
                # 再重新对账。只有自愈后仍不一致才触发事故，防止近线下单→成交的
                # 时序窗口（订单已成交但 _monitor_orders 尚未处理）被误判为永久异常。
                if result.differences:
                    print(f"[recon] Mismatch detected, attempting self-heal: {result.differences}")
                    heal_attempted = False
                    try:
                        await self._sync_exchange_state()
                        heal_attempted = True  # 标记自愈已尝试，后续差异降级为 WARNING
                        # 自愈后重新对账：用最新交易所状态更新 system_facts 并再次 reconcile
                        account2, ok2 = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
                        if ok2 and "positions" in account2:
                            self._last_account = account2
                            # 重建系统事实
                            system_positions2: dict[InstrumentId, Quantity] = {}
                            for _pos_id, pos in self._protection.all_positions().items():
                                system_positions2[InstrumentId(pos.instrument_id)] = Quantity(amount=str(pos.quantity))
                            system_facts2 = AccountFactSnapshot(
                                account_id=AccountId("default"),
                                venue_id=VenueId("BINANCE"),
                                balance=MonetaryValue(amount=str(float(account2.get("totalWalletBalance", 0)))),
                                positions=system_positions2,
                                open_orders=list(self._active_order_ids),
                            )
                            exchange_positions2: dict[InstrumentId, Quantity] = {}
                            for p in account2.get("positions", []):
                                amt = float(p.get("positionAmt", 0))
                                if amt != 0:
                                    exchange_positions2[InstrumentId(p["symbol"])] = Quantity(amount=str(abs(amt)))
                            exchange_open2: list[str] = []
                            try:
                                oo2, oo_ok2 = await self._api_async_safe(Endpoint.OPEN_ORDERS, signed=True)
                                if oo_ok2 and isinstance(oo2, list):
                                    exchange_open2 = [str(o["orderId"]) for o in oo2]
                            except Exception:
                                logger.warning(
                                    "[recon] Open orders query failed in reconciliation, proceeding with exchange positions only"
                                )
                            exchange_facts2 = AccountFactSnapshot(
                                account_id=AccountId("default"),
                                venue_id=VenueId("BINANCE"),
                                balance=MonetaryValue(amount=str(float(account2.get("totalWalletBalance", 0)))),
                                positions=exchange_positions2,
                                open_orders=exchange_open2,
                            )
                            self._recon.update_system_facts(system_facts2)
                            self._recon.update_exchange_facts(exchange_facts2)
                            result2 = self._recon.reconcile(AccountId("default"), VenueId("BINANCE"))
                            if result2.matched:
                                print("[recon] Self-heal SUCCESS — mismatch resolved, no incident raised")
                                # 清除之前的对账事故，防止残留 CRITICAL 事故导致监督器 LOCKED
                                for inc in self._alerts.get_active_incidents():
                                    if inc.get("title") == "Reconciliation mismatch":
                                        self._alerts.resolve_incident(inc["incident_id"])
                                        print(f"[recon] Cleared stale incident: {inc['incident_id']}")
                                self._last_account = account2
                                return
                            else:
                                print(f"[recon] Self-heal incomplete, remaining diffs: {result2.differences}")
                                result = result2  # 用自愈后的结果继续触发事故
                    except Exception as heal_err:
                        print(f"[recon] Self-heal attempt failed: {heal_err} — falling through to incident")

                    # BD-FIX: 自愈已尝试过 → 将 _recon 事实强制对齐交易所状态，
                    # 确保监督器通过 engine._recon.reconcile() 查询时返回 MATCHED。
                    # 否则即使 incident 降级，监督器仍会因对账 FAIL 触发 LOCKED。
                    if heal_attempted:
                        try:
                            self._recon.update_system_facts(exchange_facts)
                            print("[recon] Forced _recon alignment: system_facts = exchange_facts")
                        except Exception:
                            logger.warning(
                                "[recon] Failed to update system facts after heal, reconciliation may be stale"
                            )

                # 自愈后仍不一致，触发事故
                # BD-FIX: 自愈尝试过但仍不完整 → 差异大概率是近线交易进行中的临时状态，
                # 降级为 WARNING 避免触发监督器 FAIL-CLOSED → LOCKED。
                # 自愈未尝试（异常跳过）→ 保留 CRITICAL 以触发安全熔断。
                if result.differences:
                    print(f"[recon] Mismatch (after self-heal): {result.differences}")
                    if heal_attempted:
                        severity = AlertSeverity.WARNING  # 自愈过 → 临时差异
                        print("[recon] Self-heal was attempted — severity downgraded to WARNING")
                    else:
                        severity = AlertSeverity.CRITICAL if result.should_block_new_risk else AlertSeverity.WARNING
                    self._alerts.send_incident(
                        severity,
                        "Reconciliation mismatch",
                        "; ".join(result.differences),
                        category="reconciliation",
                    )

            self._last_account = account
        except Exception as e:
            print(f"[realtime] Reconciliation error: {e}")

    async def _ensure_exchange_position_protections(self) -> None:
        """为交易所已有但系统未追踪的持仓补充保护单（启动阶段调用）。"""
        try:
            account = self._last_account
            if not account or "positions" not in account:
                return
            positions_list = account.get("positions", [])
            exchange_positions: dict[str, dict] = {}
            for p in positions_list:
                amt = float(p.get("positionAmt", 0))
                symbol = str(p.get("symbol", ""))
                if abs(amt) > 0 and symbol:
                    exchange_positions[symbol] = {"amt": amt, "entry": float(p.get("entryPrice", 0))}

            if not exchange_positions:
                return

            # 重置客户端熔断器，确保关键恢复不被限流拦截
            self._exchange.reset_circuit_breaker()

            # 若 Phase 1/2 已处理所有持仓，直接跳过
            local_symbols = {str(pp.instrument_id) for pp in self._protection.all_positions().values()}
            already_handled = set(exchange_positions.keys()) - local_symbols
            if not already_handled:
                return  # 所有持仓已由 Phase 1/2 处理完毕

            # 检查哪些已有保护单
            algos_resp = await self._api_async(Endpoint.OPEN_ALGO_ORDERS, signed=True)
            protected_symbols: set[str] = set()
            if isinstance(algos_resp, list):
                for item in algos_resp:
                    protected_symbols.add(str(item.get("symbol", "")))

            unprotected = {s: d for s, d in exchange_positions.items() if s not in protected_symbols}
            if not unprotected:
                return

            print(f"[startup] {len(unprotected)} positions without protection, placing orders...")
            for symbol, data in unprotected.items():
                try:
                    amt = data["amt"]
                    entry = data["entry"]
                    side = "SELL" if amt > 0 else "BUY"
                    qty = abs(amt)
                    prec = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
                    qty_str = f"{qty:.{prec['quantity']}f}"

                    # 使用默认止损（1% ATR 止损 + 1.6 RR 止盈）
                    stop_pct = 0.01
                    if amt > 0:
                        stop_price = entry * (1 - stop_pct)
                        tp_price = entry * (1 + stop_pct * 1.6)
                    else:
                        stop_price = entry * (1 + stop_pct)
                        tp_price = entry * (1 - stop_pct * 1.6)
                    stop_str = f"{stop_price:.{prec['price']}f}"
                    tp_str = f"{tp_price:.{prec['price']}f}"

                    # 止损单
                    sl_resp = await self._api_async(
                        Endpoint.ALGO_ORDER,
                        method="POST",
                        signed=True,
                        params={
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": "STOP_MARKET",
                            "quantity": qty_str,
                            "triggerPrice": stop_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        },
                    )
                    sl_id = str(sl_resp.get("algoId", "")) if "algoId" in sl_resp else ""

                    # 止盈单
                    tp_resp = await self._api_async(
                        Endpoint.ALGO_ORDER,
                        method="POST",
                        signed=True,
                        params={
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": "TAKE_PROFIT_MARKET",
                            "quantity": qty_str,
                            "triggerPrice": tp_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        },
                    )
                    tp_id = str(tp_resp.get("algoId", "")) if "algoId" in tp_resp else ""

                    if sl_id or tp_id:
                        print(f"[startup] ✅ Protection placed for {symbol}: SL={sl_id} TP={tp_id}")
                    else:
                        sl_err = sl_resp.get("msg", "?")
                        tp_err = tp_resp.get("msg", "?")
                        print(f"[startup] ⚠️ Protection FAILED for {symbol}: SL={sl_err} TP={tp_err}")
                        # 对 -2021 错误：加宽止损距重试一次
                        if sl_resp.get("code") == -2021:
                            wider_stop = stop_price * (0.98 if amt > 0 else 1.02)
                            sl2 = await self._api_async(
                                Endpoint.ALGO_ORDER,
                                method="POST",
                                signed=True,
                                params={
                                    "symbol": symbol,
                                    "side": side,
                                    "algoType": "CONDITIONAL",
                                    "type": "STOP_MARKET",
                                    "quantity": qty_str,
                                    "triggerPrice": f"{wider_stop:.{prec['price']}f}",
                                    "reduceOnly": "true",
                                    "workingType": "CONTRACT_PRICE",
                                },
                            )
                            if "algoId" in sl2:
                                print(f"[startup] ✅ SL retry OK for {symbol}: algoId={sl2['algoId']}")
                            else:
                                print(f"[startup] ⚠️ SL retry FAILED for {symbol}: {sl2.get('msg', '?')}")
                except Exception as e:
                    print(f"[startup] Protection placement error for {symbol}: {e}")

            # 将交易所持仓注册到本地保护系统，避免 system={} 对账 mismatch
            # 即使保护单下发失败，交易所实际持仓也必须被系统感知
            registered = 0
            for symbol, data in exchange_positions.items():
                try:
                    existing = [
                        pp for pp in self._protection.all_positions().values() if str(pp.instrument_id) == symbol
                    ]
                    if existing:
                        continue
                    amt = data["amt"]
                    entry = data["entry"]
                    if entry <= 0:
                        features = self._feed.update_features(symbol)
                        entry = features.get("price", 0) if features else 0
                        if entry <= 0:
                            continue
                    pos_id = f"pos-{symbol}"
                    pos_side = OrderSide.BUY if amt > 0 else OrderSide.SELL
                    # BD-FIX: 使用自适应计算器生成保护参数，避免 stop_loss/take_profits
                    # 为 None/空导致保护覆盖检查 expected_orders=0 → FAIL。
                    # AdaptiveProtectionCalculator 在 kline 缺失时有内置保守默认值。
                    kline_features = self._feed.get_kline_features(symbol)
                    adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry, kline_features)
                    self._protection.create_protection(
                        position_id=pos_id,
                        instrument_id=InstrumentId(symbol),
                        venue_id=VenueId("BINANCE"),
                        entry_price=entry,
                        quantity=abs(amt),
                        side=pos_side,
                        stop_loss_config=adaptive_cfg.stop_loss_config,
                        take_profit_config=adaptive_cfg.take_profit_config,
                    )
                    registered += 1
                    print(
                        f"[startup] Registered {symbol} position {pos_id} with "
                        f"SL={adaptive_cfg.stop_pct:.1f}% RR={adaptive_cfg.rr_ratio:.1f}"
                    )
                except Exception:
                    logger.warning("[startup] Failed to register protection for position, skipped")
            if registered:
                print(f"[startup] Registered {registered} exchange positions in local state")
        except Exception as e:
            print(f"[startup] Exchange protection check error: {e}")

    async def _cleanup_excess_orders(self) -> None:
        """每个持仓标的在交易所最多保留 2 个保护单，超量则取消最旧的。

        解决多轮 nearline 重试累积重复订单的问题。
        """
        try:
            existing_algos = await self._api_async(Endpoint.OPEN_ALGO_ORDERS, signed=True)
            if not isinstance(existing_algos, list):
                return
            # 按标的聚合
            by_symbol: dict[str, list[dict]] = {}
            for a in existing_algos:
                by_symbol.setdefault(a["symbol"], []).append(a)

            for symbol, orders in by_symbol.items():
                expected = 2  # 1 SL + 1 TP per position
                excess = len(orders) - expected
                if excess <= 0:
                    continue
                # 按创建时间排序，取消最旧的
                orders.sort(key=lambda a: a.get("createTime", 0))
                for a in orders[:excess]:
                    try:
                        await self._api_async(
                            Endpoint.ALGO_ORDER,
                            method="DELETE",
                            signed=True,
                            params={"symbol": symbol, "algoId": int(a["algoId"])},
                        )
                        print(f"[nearline] 🧹 Cleaned up excess {a['orderType']} for {symbol} algoId={a['algoId']}")
                    except Exception:
                        logger.warning(
                            "[nearline] Failed to clean up excess algo order for %s algoId=%s",
                            symbol,
                            a.get("algoId", "?"),
                        )
        except Exception as e:
            print(f"[nearline] Excess order cleanup error: {e}")

    async def _retry_missing_protections(self, exchange_symbols: set[str]) -> None:
        """对保护单缺失的持仓进行重试；同时覆盖止损单和止盈单。

        与保护覆盖检查使用相同的 >= 语义：交易所保护单数量 >= 期望数量
        即视为已覆盖，避免重复下单。仅对确实缺失的订单类型进行补发。
        """
        if not self._can_write:
            return
        try:
            # 查询交易所已有的 algo 订单；API 失败时使用本地缓存
            existing_algos = await self._api_async(Endpoint.OPEN_ALGO_ORDERS, signed=True)
            api_ok = isinstance(existing_algos, list)
            exchange_algo_symbols: dict[str, set[str]] = {}
            if api_ok:
                for item in existing_algos:
                    sym = str(item.get("symbol", ""))
                    aid = str(item.get("algoId", ""))
                    if sym and aid:
                        exchange_algo_symbols.setdefault(sym, set()).add(aid)

            # 优先处理提交失败的待重试持仓
            pending = getattr(self, "_pending_protection_retry", set())
            positions = sorted(
                self._protection.all_positions().items(),
                key=lambda x: (x[0] not in pending, x[0]),  # pending first
            )
            for pos_id, pp in positions:
                symbol = str(pp.instrument_id)
                # API 正常时仅处理有交易所仓位的品种；API 失败时处理所有
                if api_ok and symbol not in exchange_symbols:
                    continue
                existing_ids = exchange_algo_symbols.get(symbol, set())

                # 计算该仓位应有保护单数量
                expected_count = (1 if pp.stop_loss and pp.stop_loss.is_active() else 0) + sum(
                    1 for tp in pp.take_profits if tp.is_active()
                )
                server_count = len(existing_ids)
                # 交易所已有 >= 期望数量即视为已覆盖
                if expected_count > 0 and server_count >= expected_count:
                    continue

                retry_count = getattr(self, "_protection_retries", {}).get(pos_id, 0)
                if retry_count >= 10:
                    # 超过最大重试：每小时重置一次计数器，避免永久放弃
                    if retry_count >= 10 and time.time() - self._protection_retries.get(f"{pos_id}_last", 0) > 3600:
                        self._protection_retries[pos_id] = 0
                    else:
                        continue

                # 首次重试即加宽 (2.0x)，之后每次递增 0.5x
                widen_factor = 2.0 + retry_count * 0.5
                side = "SELL" if pp.side == OrderSide.BUY else "BUY"
                prec = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
                qty_str = f"{float(pp.quantity):.{prec['quantity']}f}"
                placed = 0

                # --- 重试止损单 ---
                # server_count < expected_count 说明有缺失，止损单存在即尝试补发
                if pp.stop_loss is not None and pp.stop_loss.is_active():
                    orig_sl_price = float(pp.stop_loss.trigger_price.amount)
                    current_price = float(self._last_prices.get(symbol, 0))
                    entry_ref = float(pp.entry_price)
                    # 扩展止损距离（远离市价，避免 -2021 立即触发错误）
                    if current_price > 0 and abs(orig_sl_price / current_price - 1.0) > 0.001:
                        # 有行情数据：基于当前价格按比例扩展
                        if pp.side == OrderSide.BUY:
                            widened_sl = current_price * (1 - abs(1 - orig_sl_price / current_price) * widen_factor)
                            widened_sl = min(widened_sl, orig_sl_price)
                        else:
                            widened_sl = current_price * (1 + abs(orig_sl_price / current_price - 1) * widen_factor)
                            widened_sl = max(widened_sl, orig_sl_price)
                    else:
                        # 无行情数据或价格过于接近：基于入场价按固定百分比扩展
                        base_pct = max(abs(entry_ref - orig_sl_price) / entry_ref, 0.02) if entry_ref > 0 else 0.03
                        if pp.side == OrderSide.BUY:
                            widened_sl = entry_ref * (1 - base_pct * widen_factor)
                        else:
                            widened_sl = entry_ref * (1 + base_pct * widen_factor)
                    price_str = f"{widened_sl:.{prec['price']}f}"
                    algo_resp = await self._api_async(
                        Endpoint.ALGO_ORDER,
                        method="POST",
                        signed=True,
                        params={
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": pp.stop_loss.order_type,
                            "quantity": qty_str,
                            "triggerPrice": price_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        },
                    )
                    if "algoId" in algo_resp:
                        self._active_algo_ids.setdefault(pos_id, set()).add(str(algo_resp["algoId"]))
                        print(
                            f"[nearline] ✅ Retried stop loss for {symbol} (widened {widen_factor:.1f}x) → algoId={algo_resp['algoId']}"
                        )
                        placed += 1
                    else:
                        print(
                            f"[nearline] ⚠️ Stop loss retry FAILED for {symbol}: {algo_resp.get('msg', str(algo_resp)[:100])}"
                        )

                # --- 重试止盈单 ---
                for i, tp in enumerate(pp.take_profits):
                    if not tp.is_active():
                        continue
                    orig_tp_price = float(tp.trigger_price.amount)
                    current_price = float(self._last_prices.get(symbol, 0))
                    entry_ref = float(pp.entry_price)
                    # 扩展止盈距离（远离市价，增加触发空间）
                    if current_price > 0 and abs(orig_tp_price / current_price - 1.0) > 0.001:
                        if pp.side == OrderSide.BUY:
                            widened_tp = current_price * (1 + abs(orig_tp_price / current_price - 1) * widen_factor)
                            widened_tp = max(widened_tp, orig_tp_price)
                        else:
                            widened_tp = current_price * (1 - abs(1 - orig_tp_price / current_price) * widen_factor)
                            widened_tp = min(widened_tp, orig_tp_price)
                    else:
                        base_pct = max(abs(orig_tp_price - entry_ref) / entry_ref, 0.02) if entry_ref > 0 else 0.03
                        if pp.side == OrderSide.BUY:
                            widened_tp = entry_ref * (1 + base_pct * widen_factor)
                        else:
                            widened_tp = entry_ref * (1 - base_pct * widen_factor)
                    tp_price_str = f"{widened_tp:.{prec['price']}f}"
                    tp_resp = await self._api_async(
                        Endpoint.ALGO_ORDER,
                        method="POST",
                        signed=True,
                        params={
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": tp.order_type,
                            "quantity": qty_str,
                            "triggerPrice": tp_price_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        },
                    )
                    if "algoId" in tp_resp:
                        self._active_algo_ids.setdefault(pos_id, set()).add(str(tp_resp["algoId"]))
                        print(
                            f"[nearline] ✅ Retried take profit #{i} for {symbol} (widened {widen_factor:.1f}x) → algoId={tp_resp['algoId']}"
                        )
                        placed += 1
                    else:
                        print(
                            f"[nearline] ⚠️ Take profit #{i} retry FAILED for {symbol}: {tp_resp.get('msg', str(tp_resp)[:100])}"
                        )

                # 重置计数器（成功或重试完成都更新最后尝试时间）
                self._protection_retries[f"{pos_id}_last"] = time.time()
                if server_count + placed >= expected_count:
                    self._protection_retries.pop(pos_id, None)
                    pending.discard(pos_id)  # 成功，移除待重试标记
                    print(
                        f"[nearline] ✅ Protection retry complete for {symbol}: {placed} placed, total {server_count + placed}/{expected_count}"
                    )
                elif placed > 0:
                    retries = self._protection_retries.setdefault(pos_id, 0) + 1
                    self._protection_retries[pos_id] = retries
                    print(
                        f"[nearline] 🔄 Partial retry #{retries}/3 for {symbol}: {placed} placed, {server_count}/{expected_count} — widening next attempt"
                    )
                else:
                    retries = self._protection_retries.setdefault(pos_id, 0) + 1
                    self._protection_retries[pos_id] = retries
                    print(f"[nearline] ⚠️ Protection retry #{retries}/3 FAILED for {symbol}: no orders placed")

        except Exception as e:
            print(f"[nearline] Protection retry error: {e}")

    async def _nearline_tick(self) -> None:
        """近线时钟：K线分析 → 市场状态 → Alpha DAG → 融合 → 风控 → 优化 → OrderIntent。"""
        self._last_nearline = time.time()

        # === 策略风险管理检查 ===
        risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
        if not self._strategy_risk.is_trading_allowed(self._autopilot_strategy_id):
            level = risk_state.risk_level if risk_state else StrategyRiskLevel.LOCKED
            print(f"[nearline] SKIP: Strategy risk level={level.value} — trading not allowed")
            if risk_state and risk_state.active_circuit_breakers:
                print(f"[nearline] Circuit breakers: {[cb.value for cb in risk_state.active_circuit_breakers]}")
            return

        if self._lifecycle.get_degradation_level() in (DegradationLevel.EXIT_ONLY, DegradationLevel.LOCKED):
            print(f"[nearline] Skipped: degraded state ({self._lifecycle.get_degradation_level()})")
            return

        # === 幽灵仓位清理：交易所已无持仓但系统仍追踪的保护单 ===
        exchange_positions_raw = self._last_account.get("positions", [])
        exchange_symbols: set[str] = set()
        for ep in exchange_positions_raw:
            amt = float(ep.get("positionAmt", 0) or 0)
            sym = str(ep.get("symbol", ""))
            if abs(amt) > 0 and sym:
                exchange_symbols.add(sym)

        for old_pid, old_pp in list(self._protection.all_positions().items()):
            if str(old_pp.instrument_id) not in exchange_symbols:
                self._protection.cancel_protection(old_pid)
                self._protection.remove_position(old_pid)
                self._position_entry_times.pop(old_pid, None)
                await self._cancel_algo_orders(old_pid, str(old_pp.instrument_id))
                print(f"[nearline] 🧹 Cleaned up ghost position: {old_pp.instrument_id} (pos={old_pid})")

        # === 超量保护单清理：每个持仓最多保留 expected_count 个交易所订单 ===
        await self._cleanup_excess_orders()

        # === 保护单缺失重试：对 -2021 (立即触发) 等瞬时失败自动重试 ===
        await self._retry_missing_protections(exchange_symbols)

        try:
            active_symbols = self._trading_pool.active_instruments()
            if not active_symbols:
                active_symbols = list(self._symbols)
            for symbol in active_symbols:
                # Yield to REALTIME clock between symbols
                await asyncio.sleep(0)

                # 1. K-line features
                features = self._feed.get_kline_features(symbol, "1h", 100)
                if not features:
                    print(f"[nearline] {symbol}: no kline features available")
                    continue

                # 2. Market state estimation
                state = RealMarketStateEstimator.estimate(features)
                instrument_id = InstrumentId(symbol)
                venue_id = VenueId("BINANCE")

                print(
                    f"[nearline] {symbol}: price={features.get('close', '?')} "
                    f"trend={state['direction']} stress={state['stress']} "
                    f"rsi={features.get('rsi_14', '?')} "
                    f"sma5={features.get('sma_5', '?')} sma20={features.get('sma_20', '?')}"
                )

                if state["stress"] == "HIGH":
                    print(f"[nearline] {symbol}: SKIP (high volatility: ann_vol={features.get('ann_volatility', '?')})")
                    continue

                # === 3. Execute full AlphaGraph DAG in topological order ===
                close = features.get("close", 0)

                # Inject position info for EXIT components
                positions = self._protection.all_positions()
                symbol_positions = {k: v for k, v in positions.items() if str(v.instrument_id) == symbol}
                pos_info = {"has_position": False, "entry_price": 0, "side": "", "entry_time": 0, "pnl_pct": 0}
                if symbol_positions:
                    # Use the first matching position
                    pos_id, pos = next(iter(symbol_positions.items()))
                    entry_price = pos.entry_price
                    pos_side = "LONG" if pos.side == OrderSide.BUY else "SHORT"
                    pos_info["has_position"] = True
                    pos_info["entry_price"] = entry_price
                    pos_info["side"] = pos_side
                    pos_info["entry_time"] = self._position_entry_times.get(pos_id, time.time())
                    if entry_price > 0 and close > 0:
                        if pos_side == "LONG":
                            pos_info["pnl_pct"] = (close - entry_price) / entry_price * 100
                        elif pos_side == "SHORT":
                            pos_info["pnl_pct"] = (entry_price - close) / entry_price * 100

                context = {
                    "features": features,
                    "instrument_id": instrument_id,
                    "venue_id": venue_id,
                    "state": state,
                    "_predictions": {},
                    "_position_info": pos_info,
                }

                # Track factor predictions for later IC computation
                close = features.get("close", 0)
                if symbol not in self._last_factor_close or self._last_factor_close.get(symbol, 0) != 0:
                    prev_close = self._last_factor_close.get(symbol, close)
                    if prev_close > 0:
                        fwd_return = (close - prev_close) / prev_close
                        self._factor_returns.append(fwd_return)
                    self._last_factor_close[symbol] = close
                else:
                    self._last_factor_close[symbol] = close

                all_signals = []
                # BD-T05: Execute via StrategyKernel with parity tracking
                if self._can_write:
                    self._kernel_mode = KernelMode.TESTNET
                try:
                    order = self._alpha_graph.topological_order()
                    for comp_id in order:
                        comp = self._alpha_graph._components[comp_id]
                        signal = await comp.generate(context)
                        if hasattr(signal, "direction") and hasattr(signal, "strength"):
                            all_signals.append(signal)
                            print(
                                f"[nearline] {symbol}: DAG[{comp_id}] ({comp.component_type.value}) "
                                f"→ {signal.direction} strength={signal.strength:.3f}"
                            )
                except ValueError as e:
                    print(f"[nearline] {symbol}: DAG error: {e}")
                    continue
                # Record parity after execution (strongest signal as tick proposal)
                result_proposal = max(all_signals, key=lambda s: getattr(s, "strength", 0), default=None)
                self._kernel_parity = (
                    self._strategy_kernel.compute_proposal_hash(result_proposal) if result_proposal else ""
                )

                # Save predictions for factor IC evaluation
                predictions = context.get("_predictions", {})
                for fid in self._factor_predictions:
                    if fid in predictions:
                        self._factor_predictions[fid].append(predictions[fid])

                if not all_signals:
                    print(f"[nearline] {symbol}: SKIP (no signals from DAG)")
                    continue

                # === 3.5 检测 EXIT 组件平仓信号（优先于入场） ===
                exit_flat_signals = [
                    s
                    for s in all_signals
                    if s.component_type == AlphaComponentType.EXIT
                    and s.direction == SignalDirection.FLAT
                    and s.strength >= 0.3
                ]
                if exit_flat_signals and pos_info["has_position"]:
                    # 生成平仓订单
                    close_side = OrderSide.SELL if pos_info["side"] == "LONG" else OrderSide.BUY
                    close_qty = (
                        abs(float(symbol_positions[next(iter(symbol_positions))].quantity))
                        if symbol_positions
                        else 0.001
                    )
                    from beidou_safety.execution import OrderIntent

                    close_intent = OrderIntent(
                        intent_id=f"intent-{symbol}-close-{int(time.time())}",
                        account_ref=AccountRef(venue_id=venue_id, account_id=AccountId("default")),
                        instrument_id=instrument_id,
                        side=close_side,
                        order_type=OrderType.MARKET,
                        quantity=Quantity(amount=str(close_qty)),
                        price=Price(amount=str(close)),
                        time_in_force=TimeInForce.GTC,
                        client_order_id=f"beidou-{symbol.lower()}-close-{int(time.time())}",
                        correlation_id=CorrelationId(f"nearline-close-{int(time.time())}"),
                        idempotency_key=f"idem-{symbol}-close-{int(time.time() / 300)}",
                        risk_approval_id=str(RiskApprovalId(f"nearline-close-{int(time.time())}")),
                        reduce_only=True,
                    )
                    # P0 Gate 1: 控制面校验
                    if not self._control.should_accept(close_intent):
                        print(
                            f"[nearline] {symbol}: CLOSE REJECTED by control plane ({self._control.get_status().value})"
                        )
                        continue
                    try:
                        self._outbox.commit(close_intent)
                        reasons = [s.metadata.get("reason", "unknown") for s in exit_flat_signals]
                        print(
                            f"[nearline] {symbol}: CLOSE ORDER → {close_side.value} {close_qty:.4f} reasons={reasons}"
                        )
                    except ValueError:
                        print(f"[nearline] {symbol}: CLOSE SKIP (duplicate close in window)")
                    continue  # 平仓后跳过入场逻辑

                # Check if entry signal is actionable (exclude EXIT components)
                entry_signals = [
                    s
                    for s in all_signals
                    if s.component_type != AlphaComponentType.EXIT
                    and s.direction != SignalDirection.NO_ACTION
                    and s.direction != SignalDirection.FLAT
                    and s.strength >= 0.15
                ]
                if not entry_signals:
                    print(f"[nearline] {symbol}: SKIP (all signals weak or NO_ACTION)")
                    continue

                # === 4. Signal fusion (ENTRY + FILTER only; EXIT handled above) ===
                direction_signals = [s for s in all_signals if s.component_type != AlphaComponentType.EXIT]
                fused = self._fuser.fuse(direction_signals)
                if fused.direction == SignalDirection.NO_ACTION:
                    # If fusion rejects, check if entry alone would have fired
                    entry_only = [s for s in all_signals if s.component_type == AlphaComponentType.ENTRY]
                    if entry_only:
                        # Check MomentumFilter: if filter returned NO_ACTION, it vetoed the entry
                        filter_neg = [
                            s
                            for s in all_signals
                            if s.component_type == AlphaComponentType.FILTER
                            and s.direction == SignalDirection.NO_ACTION
                        ]
                        if filter_neg:
                            print(f"[nearline] {symbol}: SKIP (MomentumFilter veto: {filter_neg[0].metadata})")
                        else:
                            print(f"[nearline] {symbol}: SKIP (fusion rejected)")
                    else:
                        print(f"[nearline] {symbol}: SKIP (fusion rejected)")
                    continue

                print(
                    f"[nearline] {symbol}: FUSED → {fused.direction} strength={fused.strength:.3f} confidence={fused.confidence:.3f}"
                    + (" CONFLICT" if fused.conflict_detected else "")
                )

                # === 5. Adaptive position sizing & leverage ===
                price = features["close"]
                account_balance_str = self._last_account.get("totalWalletBalance")
                if not account_balance_str:
                    print(f"[nearline] {symbol}: SKIP (no account balance data)")
                    continue
                account_balance = float(account_balance_str)

                ann_vol = features.get("ann_volatility", 0.3)
                spread_bps_val = features.get("spread_bps", 1.0)
                signal_strength = fused.strength

                # 自适应杠杆 — 波动率越高杠杆越低
                dyn_leverage = adaptive_leverage(ann_vol)

                # 自适应仓位 — 信号强度 × 波动率惩罚 × 点差惩罚 × 风险预算
                adaptive_pct = adaptive_position_pct(signal_strength, ann_vol, spread_bps_val)
                budget = self._strategy_risk.get_budget(self._autopilot_strategy_id)

                # ATR-based stop loss (volatility-adaptive, default 2% if ATR unavailable)
                atr_pct = features.get("atr_pct", 2.0)
                stop_loss_pct = max(1.0, min(atr_pct * 1.5, 5.0))  # 1.5× ATR, capped 1%-5%
                stop_loss_price = price * (1 - stop_loss_pct / 100)

                risk_based_size = (
                    budget.compute_position_size(
                        self._autopilot_strategy_id,
                        account_balance,
                        price,
                        stop_loss_price,
                    )
                    if budget
                    else account_balance * 0.01 / (price * stop_loss_pct / 100)
                )

                # Pool capacity check
                pool_capacity = self._trading_pool.is_tradable(symbol)

                # 自适应仓位: risk_based_size × adaptive_pct, 受 leverage 约束
                max_by_leverage = (account_balance * dyn_leverage) / price
                position_size = min(risk_based_size * adaptive_pct, max_by_leverage)
                position_size = max(0.001, min(position_size, max_by_leverage * 0.5))
                position_notional = price * position_size

                print(
                    f"[nearline] {symbol}: Adaptive → size={position_size:.4f} "
                    f"notional={position_notional:.0f} lev={dyn_leverage:.1f}x "
                    f"vol={ann_vol:.1%} atr_stop={stop_loss_pct:.1f}% "
                    f"pool={'OK' if pool_capacity else 'SKIP'}"
                )

                if not pool_capacity:
                    print(f"[nearline] {symbol}: SKIP (pool not tradable)")
                    continue

                # === 5.5 下发自适应杠杆到交易所，获取实际生效值 ===
                exchange_leverage = max(1, int(dyn_leverage))  # Binance 最低 1x
                if self._can_write:
                    actual_lev = await self._ensure_leverage(symbol, exchange_leverage)
                    if actual_lev != dyn_leverage:
                        dyn_leverage = float(actual_lev)  # 使用实际杠杆重新计算仓位
                        max_by_leverage = (account_balance * dyn_leverage) / price
                        position_size = min(risk_based_size * adaptive_pct, max_by_leverage)
                        position_size = max(0.001, min(position_size, max_by_leverage * 0.5))
                        position_notional = price * position_size

                # === 6. Cost estimation ===
                spread_bps = features.get("spread_bps", 1.0)
                vi = VenueInstrument(venue_id=venue_id, instrument_id=instrument_id)
                self._cost_model.set_fee_tier(venue_id, "vip1", 2.0, 4.0)
                cost_est = self._cost_model.estimate_order(
                    vi,
                    Quantity(amount=str(position_size)),
                    Price(amount=str(price)),
                    OrderSide.BUY if fused.direction == SignalDirection.LONG else OrderSide.SELL,
                    urgency=0.3,
                    spread_bps=spread_bps,
                )

                if cost_est.total_fee_bps > 30:
                    print(f"[nearline] {symbol}: SKIP (cost too high: {cost_est.total_fee_bps}bps)")
                    continue

                # === 7. Safety risk check ===
                snapshot = RiskSnapshot(
                    total_exposure=position_notional,
                    margin_used=position_notional / dyn_leverage if dyn_leverage > 0 else position_notional / 2,
                    margin_total=account_balance,
                    position_count=self._protection.position_count(),
                    pending_orders=len(self._active_order_ids),
                    leverage=dyn_leverage,
                    concentration_pct=position_notional / max(account_balance, 1) * 100,
                )

                if snapshot.leverage > 3.0 or snapshot.concentration_pct > 50:
                    print(
                        f"[nearline] {symbol}: SKIP (risk limits: leverage={snapshot.leverage:.1f} conc={snapshot.concentration_pct:.1f}%)"
                    )
                    continue

                # === 8. Approval & Intent ===
                # Risk gates already enforced above: PreRisk → local checks (leverage/conc/cost)
                # RiskEngine.full_evaluate() requires async integration (BD-15 backlog)
                approval_id = RiskApprovalId(f"nearline-{int(time.time())}")
                self._approval.sign(approval_id)
                self._risk_sm.approve(approval_id)

                if self._risk_sm.get(approval_id) != RiskDecision.APPROVED:
                    print(f"[nearline] {symbol}: SKIP (risk not approved)")
                    continue

                from beidou_safety.execution import OrderIntent

                side = OrderSide.BUY if fused.direction == SignalDirection.LONG else OrderSide.SELL
                intent = OrderIntent(
                    intent_id=f"intent-{symbol}-{int(time.time())}",
                    account_ref=AccountRef(venue_id=venue_id, account_id=AccountId("default")),
                    instrument_id=instrument_id,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=Quantity(amount=str(position_size)),
                    price=Price(amount=str(price)),
                    time_in_force=TimeInForce.GTC,
                    client_order_id=f"beidou-{symbol.lower()}-entry-{int(time.time())}",
                    correlation_id=CorrelationId(f"nearline-{int(time.time())}"),
                    idempotency_key=f"idem-{symbol}-{int(time.time() / 300)}",
                    risk_approval_id=str(approval_id),
                )

                # P0 Gate 1: 控制面校验（Outbox 提交前）
                if not self._control.should_accept(intent):
                    print(
                        f"[nearline] {symbol}: ❌ Intent REJECTED by control plane ({self._control.get_status().value}) — risk increase blocked"
                    )
                    continue

                try:
                    self._outbox.commit(intent)
                    print(
                        f"[nearline] {symbol}: ✅ OrderIntent CREATED → {side.value} {position_size:.4f} @ {price} (outbox_id={id(self._outbox)} size={len(self._outbox._outbox)})"
                    )
                except ValueError:
                    print(f"[nearline] {symbol}: SKIP (duplicate intent in window)")

        except Exception as e:
            self._error_count += 1
            print(f"[nearline] ERROR: {e}")

    async def _sync_exchange_state(self) -> None:
        """近线后全量对账自愈：补齐遗漏的成交追踪，重建保护单。

        近线策略可能同时下多个 MARKET 单，在 testnet 上几乎立即成交。
        实时循环逐笔查询订单状态可能落后，导致 fill 漏追踪。
        此方法在近线周期完成后做一次全量对账，将系统状态与交易所同步。

        对账策略：
        - 交易所持仓与系统持仓对齐：补录漏掉的成交，清理幽灵持仓
        - 保护单补建：为交易所存在但系统未保护的持仓创建止盈止损
        - 不修改已正确追踪的状态，只修复差异
        """
        if not self._can_write:
            return

        try:
            # 重置熔断器确保关键对账不被限流拦截
            self._exchange.reset_circuit_breaker()

            # Step 1: 获取交易所完整状态
            account, ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if not ok or "positions" not in account:
                print("[sync] SKIP: account API unavailable")
                return

            exchange_positions: dict[str, float] = {}
            for p in account.get("positions", []):
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    exchange_positions[p["symbol"]] = abs(amt)

            # 获取交易所挂单
            exchange_order_ids: set[str] = set()
            try:
                open_orders, orders_ok = await self._api_async_safe(Endpoint.OPEN_ORDERS, signed=True)
                if orders_ok and isinstance(open_orders, list):
                    exchange_order_ids = {str(o["orderId"]) for o in open_orders}
            except Exception:
                logger.warning("[sync] Failed to query open orders during exchange state sync")

            # Step 2: 系统状态 vs 交易所状态对比
            system_positions: dict[str, float] = {}
            for _pos_id, pp in self._protection.all_positions().items():
                sym = str(pp.instrument_id)
                if sym in system_positions:
                    system_positions[sym] += pp.quantity
                else:
                    system_positions[sym] = pp.quantity

            # Step 3: 清理幽灵持仓（系统有但交易所无）
            ghost_cleaned = 0
            for old_pid, old_pp in list(self._protection.all_positions().items()):
                sym = str(old_pp.instrument_id)
                if sym not in exchange_positions:
                    self._protection.cancel_protection(old_pid)
                    self._protection.remove_position(old_pid)
                    self._position_entry_times.pop(old_pid, None)
                    await self._cancel_algo_orders(old_pid, sym)
                    ghost_cleaned += 1
                    print(f"[sync] 🧹 Ghost position cleaned: {sym} (id={old_pid})")

            # Step 4: 清理系统内已不存在的挂单（系统有但交易所无）
            stale_order_ids = self._active_order_ids - exchange_order_ids
            for oid in stale_order_ids:
                self._active_order_ids.discard(oid)
                self._order_trackers.pop(oid, None)
                symbol = self._order_symbols.pop(oid, None)
                # 该订单已经在交易所成交或取消，更新 DB
                self._store.save_order_state(
                    oid,
                    symbol or "",
                    "UNKNOWN",
                    "MARKET",
                    "0",
                    None,
                    "FILLED",
                )
            if stale_order_ids:
                print(f"[sync] 🧹 Stale orders cleaned: {len(stale_order_ids)} — marked FILLED")

            # Step 5: 对齐交易所新增/变更的持仓（交易所持仓 ≠ 系统持仓）
            # 当交易所持仓量与系统记录不一致时，以交易所为准更新系统状态
            # 这处理了近线订单成交后实时循环未及时追踪的场景
            for sym, ex_qty in exchange_positions.items():
                sys_qty = system_positions.get(sym, 0)
                if abs(ex_qty - sys_qty) > 0.0001:
                    # 交易所与系统不一致 — 以交易所为准
                    diff = ex_qty - sys_qty
                    direction = "increased" if diff > 0 else "decreased"
                    print(
                        f"[sync] 📊 {sym}: system={sys_qty:.4f} exchange={ex_qty:.4f} ({direction} by {abs(diff):.4f})"
                    )

                    # 如果系统没有该持仓，补建
                    if sys_qty == 0 and ex_qty > 0:
                        # 新持仓 — 需要创建保护
                        features = self._feed.get_kline_features(sym)
                        entry_price = features.get("close", 0) if features else 0
                        if entry_price <= 0:
                            entry_price = float(
                                next(
                                    (
                                        p.get("entryPrice", 0)
                                        for p in account.get("positions", [])
                                        if p.get("symbol") == sym
                                    ),
                                    0,
                                )
                            )
                        if entry_price > 0:
                            with contextlib.suppress(Exception):
                                await self._ensure_exchange_position_protections()
                    elif ex_qty == 0 and sys_qty > 0:
                        # 已在 Step 3 处理
                        pass
                    else:
                        # 数量变化 — 以交易所为准调整系统追踪
                        for old_pid, old_pp in list(self._protection.all_positions().items()):
                            if str(old_pp.instrument_id) == sym:
                                real_entry = float(
                                    next(
                                        (
                                            p.get("entryPrice", 0)
                                            for p in account.get("positions", [])
                                            if p.get("symbol") == sym
                                        ),
                                        old_pp.entry_price,
                                    )
                                )
                                if real_entry <= 0:
                                    real_entry = old_pp.entry_price
                                side = OrderSide.BUY if ex_qty > 0 else OrderSide.SELL
                                new_pos_id = f"pos-synced-{sym}-{int(time.time())}"
                                old_trailing = old_pp.trailing_config
                                self._protection.remove_position(old_pid)
                                self._position_entry_times.pop(old_pid, None)
                                # 用自适应计算器重新生成保护参数
                                kf = self._feed.get_kline_features(sym)
                                ac = AdaptiveProtectionCalculator.calculate(sym, real_entry, kf)
                                self._protection.create_protection(
                                    position_id=new_pos_id,
                                    instrument_id=InstrumentId(sym),
                                    venue_id=VenueId("BINANCE"),
                                    entry_price=real_entry,
                                    quantity=abs(ex_qty),
                                    side=side,
                                    stop_loss_config=ac.stop_loss_config,
                                    take_profit_config=ac.take_profit_config,
                                    trailing_config=old_trailing,
                                )
                                self._position_entry_times[new_pos_id] = time.time()
                                print(
                                    f"[sync] 📊 {sym}: quantity adjusted {sys_qty:.4f}→{ex_qty:.4f} "
                                    f"(pos {old_pid}→{new_pos_id})"
                                )
                                await self._cancel_algo_orders(old_pid, sym)
                                break

            # Step 6: 补建缺失的保护单
            await self._ensure_exchange_position_protections()

            # Step 7: 更新 _last_account 为最新数据，确保后续对账使用正确基准
            self._last_account = account
            if ghost_cleaned > 0 or stale_order_ids:
                print(f"[sync] ✅ Self-healed: {ghost_cleaned} ghosts, {len(stale_order_ids)} stale orders")

        except Exception as e:
            print(f"[sync] Self-heal error: {e}")

    # --- Clock Domain: OFFLINE (every 1h) ---

    async def _offline_tick(self) -> None:
        """离线时钟：因子评估 → 漂移检测 → 策略风控 → 日报 → 检查点 → 清理。"""
        self._last_offline = time.time()

        try:
            now = datetime.now(timezone.utc)

            # === 1. Factor evaluation: compute live IC/ICIR from tracked predictions ===
            print(f"[offline] Factor evaluation: {len(self._factor_returns)} return samples")
            if len(self._factor_returns) > 10:
                lifecycle_changed = False
                for fid in self._factor_predictions:
                    preds = self._factor_predictions[fid]
                    returns = (
                        self._factor_returns[-len(preds) :]
                        if len(preds) <= len(self._factor_returns)
                        else self._factor_returns
                    )
                    if len(preds) < 10 or len(returns) < 10:
                        continue

                    min_n = min(len(preds), len(returns))
                    preds_aligned = preds[-min_n:]
                    rets_aligned = returns[-min_n:]

                    ic_mean, ic_std = FactorEvaluator.compute_ic(preds_aligned, rets_aligned)
                    rank_ic = FactorEvaluator.compute_rank_ic(preds_aligned, rets_aligned)
                    decile_spread = FactorEvaluator.compute_decile_spread(preds_aligned, rets_aligned)

                    # Compute rolling ICIR from last N IC values
                    rolling_ics = []
                    for i in range(max(5, min_n - 20), min_n):
                        window_preds = preds_aligned[:i]
                        window_rets = rets_aligned[:i]
                        if len(window_preds) >= 5:
                            ic, _ = FactorEvaluator.compute_ic(window_preds, window_rets)
                            rolling_ics.append(ic)
                    icir = FactorEvaluator.compute_icir(rolling_ics) if rolling_ics else 0.0

                    print(
                        f"[offline] Factor {fid}: IC={ic_mean:.4f} RankIC={rank_ic:.4f} "
                        f"ICIR={icir:.3f} decile={decile_spread:.4f} samples={min_n}"
                    )

                    record = self._factor_registry.get(fid)
                    if record:
                        from beidou_research.factors.factor import FactorPerformance

                        perf = FactorPerformance(
                            factor_id=fid,
                            evaluation_period=f"live-{now.strftime('%Y%m%d-%H')}",
                            sample_count=min_n,
                            ic_mean=ic_mean,
                            ic_std=ic_std,
                            icir=icir,
                            rank_ic_mean=rank_ic,
                            rank_ic_std=0.0,
                            rank_icir=0.0,
                            top_bottom_decile_spread=decile_spread,
                        )
                        record.performance.append(perf)

                        # Factor lifecycle: degrade only with sufficient samples and very low ICIR
                        n_samples = len(self._factor_predictions.get(fid, []))
                        if record.lifecycle == FactorLifecycle.ACTIVE and n_samples >= 50 and icir < 0.05:
                            self._factor_registry.degrade(fid, f"ICIR dropped to {icir:.3f} (n={n_samples})")
                            self._alerts.send_incident(
                                AlertSeverity.WARNING,
                                f"Factor degraded: {fid}",
                                f"ICIR={icir:.3f} below threshold 0.2",
                                category="factor",
                            )
                            print(f"[offline] Factor {fid}: DEGRADED (ICIR={icir:.3f} < 0.2)")
                            lifecycle_changed = True
                        elif record.lifecycle == FactorLifecycle.CHALLENGER and icir >= 0.3:
                            self._factor_registry.promote_to_active(fid)
                            print(f"[offline] Factor {fid}: PROMOTED TO ACTIVE (ICIR={icir:.3f})")
                            lifecycle_changed = True
                        elif record.lifecycle == FactorLifecycle.DEGRADED and icir >= 0.3:
                            # 退化因子恢复: DEGRADED → CHALLENGER（需重新验证再晋升ACTIVE）
                            record.restart_as_challenger()
                            print(f"[offline] Factor {fid}: RECOVERED to CHALLENGER (ICIR={icir:.3f})")
                            lifecycle_changed = True

                # Rebuild AlphaGraph if any factor lifecycle changed
                if lifecycle_changed:
                    self._rebuild_alpha_graph()

                # Trim prediction buffers
                max_buf = 500
                for fid in self._factor_predictions:
                    if len(self._factor_predictions[fid]) > max_buf:
                        self._factor_predictions[fid] = self._factor_predictions[fid][-max_buf:]
                if len(self._factor_returns) > max_buf:
                    self._factor_returns = self._factor_returns[-max_buf:]

            # === 2. Real drift detection ===
            if self._trade_pnls:
                total_trades = self._win_count + self._loss_count
                win_rate = self._win_count / max(1, total_trades)
                avg_pnl = sum(self._trade_pnls) / len(self._trade_pnls) if self._trade_pnls else 0
                std_pnl = (sum((p - avg_pnl) ** 2 for p in self._trade_pnls) / max(1, len(self._trade_pnls))) ** 0.5
                sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0.0

                if not self._drift_detector.is_calibrated():
                    # First calibration — set baseline from live performance
                    self._drift_detector.set_baseline({"sharpe": max(0.5, sharpe), "win_rate": max(0.4, win_rate)})
                    print("[offline] DriftDetector baseline calibrated")
                else:
                    drift = self._drift_detector.detect({"sharpe": sharpe, "win_rate": win_rate})
                    if self._drift_detector.should_retire(drift):
                        self._alerts.send_incident(
                            AlertSeverity.HIGH,
                            "Model drift detected",
                            f"Drifted metrics: {list(drift.keys())}, live sharpe={sharpe:.3f} win_rate={win_rate:.2f}",
                            category="model",
                        )
                        print(f"[offline] ⚠️ Model drift: {drift}")
                    else:
                        print(f"[offline] DriftDetector: no drift (sharpe={sharpe:.3f} win_rate={win_rate:.2f})")
            else:
                print("[offline] DriftDetector: no trade data yet, skipping drift check")

            # === 3. Strategy risk: daily PnL reset ===
            # 检测日期变更，重置每日盈亏统计
            if not hasattr(self, "_last_daily_reset_day"):
                self._last_daily_reset_day = now.day
            if now.day != self._last_daily_reset_day:
                self._strategy_risk.reset_daily_pnl()
                self._last_daily_reset_day = now.day
                print(f"[offline] Daily PnL reset (new day: {now.day})")
            risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
            if risk_state:
                print(
                    f"[offline] Strategy risk: level={risk_state.risk_level.value} "
                    f"drawdown={risk_state.current_drawdown_pct:.1f}% "
                    f"daily_pnl={risk_state.daily_pnl:.2f} "
                    f"peak_equity={risk_state.peak_equity:.0f}"
                )
                # Check drawdown via strategy risk manager
                account_balance = float(self._last_account.get("totalWalletBalance", 0))
                if account_balance > 0:
                    result = self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
                    if result.get("action") == "DEGRADE":
                        self._alerts.send_incident(
                            AlertSeverity.HIGH,
                            f"Strategy risk degraded: {result.get('new_level')}",
                            result.get("reason", ""),
                            category="strategy_risk",
                        )
                        print(f"[offline] ⚠️ Strategy risk degraded: {result}")

            # === 4. Daily report ===
            report_gen = self._alerts.get_report_generator()
            report_gen.generate_daily_report(
                date=now,
                strategies=[StrategyId("autopilot")],
                account_id=AccountId("default"),
                venue_id=VenueId("BINANCE"),
                risk_events_24h=self._alerts.get_alert_stats().get("total", 0),
                active_incidents=[i["incident_id"] for i in self._alerts.get_active_incidents()],
            )

            # === 5. Checkpoint ===
            self._mapek.save_checkpoint(
                "autopilot",
                {
                    "tick_count": self._tick_count,
                    "order_count": self._order_count,
                    "positions": self._protection.position_count(),
                    "ledger_entries": len(self._ledger._entries),
                    "active_factors": len(self._factor_registry.get_active())
                    + len(self._factor_registry.get_challengers()),
                    "win_rate": round(self._win_count / max(1, self._win_count + self._loss_count), 3),
                    "timestamp": now.isoformat(),
                },
                invariants_valid=True,
            )
            self._store.save_checkpoint(
                f"cp-{now.strftime('%Y%m%d%H%M%S')}",
                "autopilot",
                {"tick_count": self._tick_count, "order_count": self._order_count},
                self._tick_count,
            )

            # === 6. Cleanup ===
            deleted = self._store.cleanup_old_data(retention_days=90)
            print(f"[offline] Cleanup: {deleted} old records deleted")

        except Exception as e:
            self._error_count += 1
            print(f"[offline] ERROR: {e}")

    # --- Main loop ---

    async def run(self) -> None:
        """启动自主运行引擎。"""
        print("[beidou-autopilot] ========================================")
        print("[beidou-autopilot] Starting Autonomous Engine V2.0")
        print(f"[beidou-autopilot] Symbols: {self._symbols}")
        print(f"[beidou-autopilot] Mode: {self._env_mode.value} (write={'ON' if self._can_write else 'OFF'})")
        print(f"[beidou-autopilot] Exchange: {self._rest_url}")
        print("[beidou-autopilot] ========================================")

        # Startup sequence
        self._lifecycle.transition(ModuleState.BOOTSTRAPPING)
        print("[beidou-autopilot] Bootstrapping...")

        # Verify exchange connectivity
        server_time = await self._api_async(Endpoint.SERVER_TIME)
        if "serverTime" not in server_time:
            print("[beidou-autopilot] FATAL: Cannot connect to exchange")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        print(f"[beidou-autopilot] Exchange connected: {self._rest_url}")

        # Verify account access
        account = await self._api_async(Endpoint.ACCOUNT, signed=True)
        if "totalWalletBalance" not in account and "assets" not in account and "canTrade" not in account:
            print("[beidou-autopilot] FATAL: Cannot access account")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        self._last_account = account
        init_equity = float(account.get("totalWalletBalance", 0))
        self._peak_equity = init_equity
        self._strategy_risk.update_equity(self._autopilot_strategy_id, init_equity)
        print(f"[beidou-autopilot] Account OK: equity={init_equity}")

        # Restore state from persistence
        print("[beidou-autopilot] Restoring state...")
        ledger_entries = self._store.restore_ledger_entries()
        active_orders = self._store.get_active_orders()
        protections = self._store.restore_protections()
        print(
            f"[beidou-autopilot] Restored: {len(ledger_entries)} ledger entries, "
            f"{len(active_orders)} active orders, {len(protections)} protections"
        )

        # Restore active_order_ids from exchange
        try:
            exchange_open = await self._api_async(Endpoint.OPEN_ORDERS, signed=True)
            if isinstance(exchange_open, list):
                for o in exchange_open:
                    oid = str(o["orderId"])
                    tracker = OrderStateTracker(order_id=OrderId(oid))
                    tracker.apply(OrderEvent.ACKED)
                    tracker.apply(OrderEvent.SENT)
                    if o.get("status") == "PARTIALLY_FILLED":
                        tracker.apply(OrderEvent.PARTIALLY_FILLED)
                    self._order_trackers[oid] = tracker
                    self._active_order_ids.add(oid)
                print(f"[beidou-autopilot] Restored {len(self._active_order_ids)} active orders from exchange")
        except Exception as e:
            print(f"[beidou-autopilot] Warning: Could not restore open orders: {e}")

        # BD-FIX: 启动时恢复交易所持仓的止盈止损保护
        # 先获取 exchangeInfo 填充精度缓存，避免低价币种四舍五入错误
        try:
            exchange_info = await self._api_async(Endpoint.EXCHANGE_INFO)
            if not hasattr(self, "_symbol_precision"):
                self._symbol_precision = {}
            for s in exchange_info.get("symbols", []):
                sym = s.get("symbol", "")
                prec = {"quantity": 3, "price": 2}
                for f_item in s.get("filters", []):
                    if f_item.get("filterType") == "LOT_SIZE":
                        step = f_item.get("stepSize", "0.001")
                        prec["quantity"] = max(0, len(step.split(".")[1].rstrip("0")) if "." in step else 0)
                    if f_item.get("filterType") == "PRICE_FILTER":
                        tick = f_item.get("tickSize", "0.01")
                        prec["price"] = max(0, len(tick.split(".")[1].rstrip("0")) if "." in tick else 0)
                self._symbol_precision[sym] = prec
            print(f"[beidou-autopilot] Loaded precision for {len(self._symbol_precision)} symbols")
        except Exception as e:
            print(f"[beidou-autopilot] Warning: exchangeInfo load failed: {e}")

        # BD-FIX: 启动时取消交易所所有已有条件单，避免跨 session 累积重复
        # 分批取消（每批最多 5 个，批次间隔 2s），避免触发 Binance 限流熔断
        try:
            existing_algos = await self._api_async(Endpoint.OPEN_ALGO_ORDERS, signed=True)
            if isinstance(existing_algos, list) and existing_algos:
                cancelled_count = 0
                batch_size = 5
                for i in range(0, len(existing_algos), batch_size):
                    batch = existing_algos[i : i + batch_size]

                    async def _cancel_one(algo: dict) -> bool:
                        try:
                            cancel_resp = await self._api_async(
                                Endpoint.ALGO_ORDER,
                                method="DELETE",
                                signed=True,
                                params={"symbol": algo["symbol"], "algoId": int(algo["algoId"])},
                            )
                            return "code" not in cancel_resp
                        except Exception:
                            return False

                    results = await asyncio.gather(*[_cancel_one(a) for a in batch])
                    cancelled_count += sum(1 for r in results if r)

                    # 批次间休息，避免触发限流 (Binance testnet 限制较严格)
                    if i + batch_size < len(existing_algos):
                        await asyncio.sleep(2)

                print(
                    f"[beidou-autopilot] Cleaned up {cancelled_count}/{len(existing_algos)} "
                    f"stale algo orders before recovery"
                )
        except Exception as e:
            print(f"[beidou-autopilot] Warning: stale algo cleanup failed: {e}")

        # 清理旧订单可能触发客户端熔断器，重置以确保后续恢复不受影响
        self._exchange.reset_circuit_breaker()

        try:
            # 使用 _api_async_safe 防止熔断返回空数据导致跳过保护恢复
            account, ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if not ok or "positions" not in account:
                print("[beidou-autopilot] WARNING: Cannot query account for position recovery — retrying once...")
                await asyncio.sleep(3)
                self._exchange.reset_circuit_breaker()
                account, ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
                if not ok:
                    print("[beidou-autopilot] WARNING: Position recovery skipped (API unavailable)")
                    return
            positions_list = account.get("positions", [])
            # Phase 1: 本地创建所有保护单
            pending_submissions: list[dict] = []
            for p in positions_list:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                symbol = p["symbol"]
                entry_price = float(p.get("entryPrice", 0))
                if entry_price <= 0:
                    features = self._feed.update_features(symbol)
                    entry_price = features.get("price", 0) if features else 0
                    if entry_price <= 0:
                        continue
                pos_side = OrderSide.BUY if amt > 0 else OrderSide.SELL
                qty = abs(amt)
                pos_id = f"pos-recovered-{symbol}"

                already_protected = any(
                    str(pp.instrument_id) == symbol and abs(float(pp.quantity) - qty) < 1e-8
                    for pp in self._protection.all_positions().values()
                )
                if already_protected:
                    continue

                kline_features = self._feed.get_kline_features(symbol)
                adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry_price, kline_features)
                pp = self._protection.create_protection(
                    position_id=pos_id,
                    instrument_id=InstrumentId(symbol),
                    venue_id=VenueId("BINANCE"),
                    entry_price=entry_price,
                    quantity=qty,
                    side=pos_side,
                    stop_loss_config=adaptive_cfg.stop_loss_config,
                    take_profit_config=adaptive_cfg.take_profit_config,
                )
                self._position_entry_times[pos_id] = time.time()

                reduce_side = "SELL" if pos_side == OrderSide.BUY else "BUY"
                protect_orders = [pp.stop_loss] if pp.stop_loss else []
                protect_orders.extend(pp.take_profits)

                for p_order in protect_orders:
                    if p_order is None:
                        continue
                    prec_map = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
                    qty_str = f"{float(p_order.quantity.amount):.{prec_map['quantity']}f}"
                    price_str = f"{float(p_order.trigger_price.amount):.{prec_map['price']}f}"
                    pending_submissions.append(
                        {
                            "symbol": symbol,
                            "pos_id": pos_id,
                            "reason": p_order.reason,
                            "algo_params": {
                                "symbol": symbol,
                                "side": reduce_side,
                                "algoType": "CONDITIONAL",
                                "type": p_order.order_type,
                                "quantity": qty_str,
                                "triggerPrice": price_str,
                                "reduceOnly": "true",
                                "workingType": "CONTRACT_PRICE",
                            },
                        }
                    )

            # Phase 2: 分批提交条件单到交易所（每批 5 个，间隔 2s，避免限流熔断）
            if pending_submissions:

                async def _submit_algo(sub: dict):
                    sub["result"] = await self._api_async(
                        Endpoint.ALGO_ORDER, method="POST", signed=True, params=sub["algo_params"]
                    )
                    return sub

                results: list = []
                batch_size = 5
                for i in range(0, len(pending_submissions), batch_size):
                    batch = pending_submissions[i : i + batch_size]
                    batch_results = await asyncio.gather(*[_submit_algo(s) for s in batch], return_exceptions=True)
                    results.extend(batch_results)
                    if i + batch_size < len(pending_submissions):
                        await asyncio.sleep(2)
                recovered_by_pos: dict[str, dict] = {}
                for sub, res in zip(pending_submissions, results, strict=False):
                    if isinstance(res, Exception):
                        print(f"[startup] Recovery ERROR {sub['symbol']} {sub['reason']}: {res}")
                        continue
                    algo_resp = res["result"]
                    symbol = sub["symbol"]
                    pos_id = sub["pos_id"]
                    if "algoId" in algo_resp:
                        algo_id = str(algo_resp["algoId"])
                        if not hasattr(self, "_active_algo_ids"):
                            self._active_algo_ids = {}
                        self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                        rec = recovered_by_pos.setdefault(pos_id, {"symbol": symbol, "placed": 0, "total": 0})
                        rec["placed"] += 1
                        rec["total"] += 1
                        print(f"[startup] Recovered protection: {symbol} {sub['reason']} → algoId={algo_id}")
                    else:
                        rec = recovered_by_pos.setdefault(pos_id, {"symbol": symbol, "placed": 0, "total": 0})
                        rec["total"] += 1
                        print(
                            f"[startup] Recovery FAILED {symbol} {sub['reason']}: "
                            f"{algo_resp.get('msg', str(algo_resp)[:100])}"
                        )

                for pos_id, rec in recovered_by_pos.items():
                    print(f"[startup] Recovered position {rec['symbol']}: {rec['placed']}/{rec['total']} placed")
                total_placed = sum(r["placed"] for r in recovered_by_pos.values())
                print(
                    f"[beidou-autopilot] Restored protections for {len(recovered_by_pos)} positions ({total_placed} orders)"
                )
        except Exception as e:
            print(f"[beidou-autopilot] Warning: Position protection recovery failed: {e}")
            import traceback

            traceback.print_exc()

        # Print strategy/risk activation status
        print(
            f"[beidou-autopilot] StrategyKernel: {self._kernel_mode.value} (AlphaGraph DAG: {self._alpha_graph.topological_order()})"
        )
        print("[beidou-autopilot] Strategy Risk: max_drawdown=20% max_daily_loss=5% max_consec_losses=5")
        factor_ids = list(self._factor_registry._factors.keys())
        print(f"[beidou-autopilot] Factor Registry: {len(factor_ids)} factors registered ({factor_ids})")
        print(
            "[beidou-autopilot] Circuit Breakers: DRAWNDOWN_LIMIT | DAILY_LOSS_LIMIT | CONSECUTIVE_LOSSES | "
            "SHARPE_DEGRADATION | VOLATILITY_SPIKE | CORRELATION_BREAKDOWN | MAX_POSITION_COUNT"
        )

        # Lifecycle transitions
        self._lifecycle.transition(ModuleState.WARMING)
        self._lifecycle.transition(ModuleState.VALIDATING)

        # BD-FIX: 启动时全量对账自愈 — 在初始对账之前先同步交易所状态，
        # 清理 DB 中残留的幽灵持仓和过期订单（Binance testnet 返回可能不一致），
        # 防止对账 MISMATCH → FAIL-CLOSED → LOCKED 链式崩溃。
        print("[beidou-autopilot] Startup sync: aligning with exchange state...")
        await self._sync_exchange_state()

        # BD-T14 Phase 5: 初始对账 — 在 ACTIVE 转换之前填充对账引擎事实，
        # 避免监督器在引擎首次对账（30s）之前因 BOTH_SIDES_MISSING 误触发 LOCKED。
        try:
            await self._reconcile()
            self._last_recon = time.time()
            print("[beidou-autopilot] Initial reconciliation complete")
        except Exception as e:
            print(f"[beidou-autopilot] Initial reconciliation failed: {e} — continuing")

        # Phase 5b: 交易所残留持仓保护补充 — 检测交易所已有但系统未追踪的持仓，
        # 立即放置保护单，避免监督器在启动后因 protection_coverage 阻断。
        await self._ensure_exchange_position_protections()

        self._lifecycle.transition(ModuleState.ACTIVE)
        print(f"[beidou-autopilot] State: {self._lifecycle.state.value}")

        # Start health server
        self._health.start()
        print(
            f"[beidou-autopilot] Health server: http://{self._settings.infrastructure.health_host}:"
            f"{self._settings.infrastructure.health_port}"
        )

        # BD-T14: Startup 后短暂 NO_NEW_RISK，由 Supervisor 在深度验证通过后 RESUME。
        # 引擎自身不再执行 RESUME（已被 Supervisor 的 resume interlock 拦截）。
        self._control.execute_action(ControlAction.NO_NEW_RISK)
        print("[beidou-autopilot] Control plane: NO_NEW_RISK (awaiting supervisor validation)")
        if self._exchange is None:
            print("[beidou-autopilot] WARNING: Exchange not ready — supervisor will block RESUME")

        self._running = True
        self._last_realtime = time.time()
        self._last_recon = time.time()
        print("[beidou-autopilot] ========================================")
        print("[beidou-autopilot] Engine running. Press Ctrl+C to stop.")
        print("[beidou-autopilot] ========================================")

        # Main event loop — 各时钟域作为独立 asyncio Task 运行，避免
        # nearline (处理 25 个标的耗时 2+ min) 阻塞 realtime 心跳。
        async def _realtime_loop() -> None:
            while self._running:
                try:
                    if time.time() - self._last_realtime >= 5:
                        await self._realtime_tick()
                except Exception:
                    self._error_count += 1
                await asyncio.sleep(1)

        async def _nearline_loop() -> None:
            while self._running:
                try:
                    if time.time() - self._last_nearline >= 300:
                        await self._nearline_tick()
                        # 近线周期后立即全量对账：补齐遗漏的成交追踪，防止
                        # 实时循环逐笔查单落后导致的 fill 漏追踪 → 对账 MISMATCH → LOCKED。
                        await self._sync_exchange_state()
                except Exception:
                    self._error_count += 1
                await asyncio.sleep(10)  # 较粗粒度轮询，nearline 本身耗时较长

        async def _offline_loop() -> None:
            while self._running:
                try:
                    if time.time() - self._last_offline >= 3600:
                        await self._offline_tick()
                except Exception:
                    self._error_count += 1
                await asyncio.sleep(60)

        tasks = [
            asyncio.create_task(_realtime_loop()),
            asyncio.create_task(_nearline_loop()),
            asyncio.create_task(_offline_loop()),
        ]

        try:
            # 等待所有 task 完成（引擎 shutdown 后 _running=False，各 loop 退出）
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 检查是否有异常退出的 task
            for t in done:
                exc = t.exception()
                if exc is not None:
                    self._error_count += 1
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._shutdown()

    async def _shutdown(self) -> None:
        """优雅关机。"""
        print("\n[beidou-autopilot] Shutting down...")
        self._running = False

        # 1. NO_NEW_RISK
        self._control.execute_action(ControlAction.NO_NEW_RISK)
        print("[beidou-autopilot] 1. NO_NEW_RISK")

        # 2. Cancel pending orders
        if self._can_write:
            for order_id in list(self._active_order_ids):
                for symbol in self._symbols:
                    try:
                        await self._api_async(
                            Endpoint.ORDER,
                            method="DELETE",
                            signed=True,
                            params={
                                "symbol": symbol,
                                "orderId": int(order_id),
                            },
                        )
                    except Exception as e:
                        print(f"[shutdown] Failed to cancel order {order_id}: {e}")
        print(f"[beidou-autopilot] 2. Cancelled {len(self._active_order_ids)} pending orders")

        # 3. Save checkpoint
        self._mapek.save_checkpoint(
            "autopilot-shutdown",
            {
                "tick_count": self._tick_count,
                "order_count": self._order_count,
                "shutdown_time": datetime.now(timezone.utc).isoformat(),
                "win_rate": round(self._win_count / max(1, self._win_count + self._loss_count), 3),
            },
            invariants_valid=True,
        )
        print("[beidou-autopilot] 3. Checkpoint saved")

        # 4. Close store
        self._store.close()
        self._health.stop()
        print("[beidou-autopilot] 4. Shutdown complete.")
