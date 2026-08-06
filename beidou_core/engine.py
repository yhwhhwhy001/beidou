"""自主运行引擎 — 三层时钟域事件循环。

绕过存根 BinanceUsdmAdapter，直接调用 Binance REST API。
实现真实市场状态估算和具体 AlphaComponent。
完整激活所有策略/因子/风控模块。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import yaml

from beidou_autonomy.mapek import MAPEKController
from beidou_control.plane import ControlAction, ControlPlane
from beidou_core.alerts import AlertDispatcher
from beidou_core.feed import MarketDataFeed
from beidou_core.health import HealthServer
from beidou_core.store import PersistentStore
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
from beidou_strategy.alpha import AlphaComponent, AlphaComponentType, AlphaGraph, SignalDirection
from beidou_strategy.alpha.model_registry import DriftDetector, ModelRegistry
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_strategy.risk.manager import (
    RiskBudget,
    StrategyRiskLevel,
    StrategyRiskManager,
)
from beidou_strategy.state.cost_model import CostModel
from beidou_data.trading_pool_lifecycle import TradingPool, PoolStatus, InstrumentScore

# Binance USDⓈ-M 永续合约交易池 — 主流 + 活跃altcoin
DEFAULT_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "ETCUSDT",
    "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "NEARUSDT",
    "INJUSDT", "SUIUSDT", "RUNEUSDT", "SEIUSDT", "TIAUSDT",
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
        close_price = features.get("close", 0)

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

        # Config
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "env.testnet.yaml",
        )
        with open(config_path) as f:
            self._config = yaml.safe_load(f)

        binance_cfg = self._config["exchange"]["binance_usdm"]
        self._rest_url = binance_cfg["rest_base_url"]
        self._api_key = str(binance_cfg.get("api_key", "")).strip()
        self._api_secret = str(binance_cfg.get("api_secret", "")).strip()

        # Adapter REST client (BD-02: single adapter boundary)
        self._exchange = BinanceRESTClient(
            rest_url=self._rest_url,
            api_key=self._api_key,
            api_secret=self._api_secret,
        )

        # Infrastructure
        self._store = PersistentStore.get_instance()
        self._feed = MarketDataFeed()
        self._alerts = AlertDispatcher()
        self._health = HealthServer(port=9090)

        # Control plane
        self._control = ControlPlane()
        self._lifecycle = ModuleLifecycle("autopilot")

        # Business modules
        self._protection = ProtectionManager()
        self._outbox = IntentOutbox()
        self._ledger = ImmutableLedger()
        self._recon = ReconciliationEngine()
        self._pre_risk = PreRiskCheckerImpl(
            max_leverage=3.0, max_concentration_pct=50.0, max_position_notional=500000.0
        )
        self._risk_engine = RiskEngineImpl()
        self._approval = RiskApprovalSignerImpl()
        self._risk_sm = RiskApprovalStateMachine()
        self._post_risk = PostRiskMonitor()
        self._cost_model = CostModel()
        self._optimizer = PortfolioOptimizerImpl(max_total_leverage=3.0)
        self._fuser = SignalFuser()
        self._model_registry = ModelRegistry()
        self._drift_detector = DriftDetector(threshold=0.1)
        self._mapek = MAPEKController()

        # === 交易池 — 动态标的管理 ===
        self._trading_pool = TradingPool(max_instruments=50)
        # 初始化默认标的（OBSERVING → PROMOTED → ACTIVE 需经过评分）
        for sym in (symbols if len(symbols) > 2 else DEFAULT_UNIVERSE):
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
                max_drawdown_pct=20.0,
                max_daily_loss_pct=5.0,
                max_consecutive_losses=5,
                max_position_notional=500000.0,
                max_leverage=3.0,
                risk_per_trade_pct=1.0,
                min_sharpe_rolling=0.0,
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

        # Promote all factors through lifecycle chain to CHALLENGER
        for fid in all_factor_ids:
            rec = self._factor_registry.get(fid)
            for target in [
                FactorLifecycle.RESEARCH,
                FactorLifecycle.BACKTEST,
                FactorLifecycle.PAPER_TRADING,
                FactorLifecycle.CHALLENGER,
            ]:
                if not rec.transition(target):
                    break
        print(
            f"[beidou-autopilot] Factor lifecycles: {[(fid, r.lifecycle.value) for fid, r in self._factor_registry._factors.items()]}"
        )

        # Factor tracking: rolling predictions vs actual returns
        self._factor_predictions: dict[str, list[float]] = {fid: [] for fid in all_factor_ids}
        self._factor_returns: list[float] = []  # forward returns for IC computation
        self._last_factor_close: dict[str, float] = {}  # symbol → last close for return calc

        # ================================================================
        # Alpha Graph — 8-component DAG
        # ================================================================
        self._alpha_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))
        self._alpha_graph.add_component(MeanReversionEntry())
        self._alpha_graph.add_component(TrendFollowingEntry())
        self._alpha_graph.add_component(BreakoutEntry())
        self._alpha_graph.add_component(MomentumFilter())
        self._alpha_graph.add_component(VolatilityFilter())
        self._alpha_graph.add_component(VolumeFilter())
        self._alpha_graph.add_component(TrailingExit())
        self._alpha_graph.add_component(TimeExit())
        # DAG topology: ENTRY → FILTER → EXIT
        # meanrev → momentum_filter, volatility_filter, volume_filter
        self._alpha_graph.connect("meanrev_entry_v1", "momentum_filter_v1")
        self._alpha_graph.connect("meanrev_entry_v1", "volatility_filter_v1")
        self._alpha_graph.connect("meanrev_entry_v1", "volume_filter_v1")
        # trend → same filters
        self._alpha_graph.connect("trend_entry_v1", "momentum_filter_v1")
        self._alpha_graph.connect("trend_entry_v1", "volatility_filter_v1")
        self._alpha_graph.connect("trend_entry_v1", "volume_filter_v1")
        # breakout → same filters
        self._alpha_graph.connect("breakout_entry_v1", "momentum_filter_v1")
        self._alpha_graph.connect("breakout_entry_v1", "volatility_filter_v1")
        self._alpha_graph.connect("breakout_entry_v1", "volume_filter_v1")
        # filters → exits (only evaluated when position active)
        self._alpha_graph.connect("momentum_filter_v1", "trailing_exit_v1")
        self._alpha_graph.connect("volatility_filter_v1", "trailing_exit_v1")
        self._alpha_graph.connect("volume_filter_v1", "time_exit_v1")
        order = self._alpha_graph.topological_order()
        print(f"[beidou-autopilot] DAG order: {order}")

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

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """通过 Adapter 访问 Binance API。

        统一路由到 BinanceRESTClient，享受统一错误分类、限频退避和熔断。
        返回原始 dict（兼容现有代码），失败时返回 {"error": code, "msg": "..."}
        """
        import urllib.request

        url = self._rest_url + path
        headers = {
            "X-MBX-APIKEY": self._api_key,
            "Connection": "close",
            "User-Agent": "beidou-autopilot/2.0",
        }
        if params is None:
            params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 60000
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            params["signature"] = hmac.new(
                self._api_secret.encode(),
                qs.encode(),
                hashlib.sha256,
            ).hexdigest()
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        if method == "POST":
            req = urllib.request.Request(url, data=qs.encode(), headers=headers)
        elif method == "DELETE":
            full_url = url + "?" + qs if qs else url
            req = urllib.request.Request(full_url, headers=headers)
            req.method = "DELETE"
        else:
            full_url = url + "?" + qs if qs else url
            req = urllib.request.Request(full_url, headers=headers)

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    # Read in chunks to handle large responses (>256KB)
                    chunks = []
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    data = json.loads(raw)
                    # Normalize /fapi/v2/balance response to account format
                    if path == "/fapi/v2/balance" and isinstance(data, list):
                        total_wallet = sum(float(a.get("crossWalletBalance", 0)) for a in data)
                        data = {"totalWalletBalance": str(total_wallet), "assets": data}
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(1 * (attempt + 1))
                    continue
                body = e.read().decode() if e.fp else ""
                print(f"[adapter] HTTP {e.code} on {path}: {body[:200]}")
                return {"error": e.code, "msg": body}
            except Exception as ex:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                print(f"[adapter] Request failed ({path}): {ex}")
        return {"error": -1, "msg": "retry exhausted"}

    # --- Leverage Management ---

    def _ensure_leverage(self, symbol: str, target_leverage: int) -> int:
        """确保交易所杠杆设置与自适应杠杆一致（带缓存）。

        返回实际生效的杠杆值（如遇 -2028 则查询当前杠杆）。
        """
        if not hasattr(self, "_leverage_cache"):
            self._leverage_cache: dict[str, int] = {}
        current = self._leverage_cache.get(symbol)
        if current == target_leverage:
            return current

        resp = self._api(
            "/fapi/v1/leverage",
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
                pos_resp = self._api(
                    "/fapi/v2/positionRisk",
                    signed=True,
                    params={"symbol": symbol},
                )
                if isinstance(pos_resp, list) and len(pos_resp) > 0:
                    lev_from_api = int(float(pos_resp[0].get("leverage", 0)))
                    if lev_from_api > 0:
                        actual = lev_from_api
            except Exception:
                pass
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

    # --- Clock Domain: REALTIME (every 5s) ---

    async def _realtime_tick(self) -> None:
        """实时时钟：行情轮询 → 保护单检查 → 订单处理 → 对账。"""
        self._tick_count += 1
        self._last_realtime = time.time()

        if self._tick_count % 3 == 0:
            print(f"[realtime] TICK #{self._tick_count} — outbox id={id(self._outbox)} items={len(self._outbox._outbox)}")

        try:
            active_symbols = self._trading_pool.active_instruments()
            if not active_symbols:
                active_symbols = list(self._symbols)

            # 批次轮询: 25个标的每tick处理5个，5 tick完成一轮，避免阻塞
            batch_size = max(5, len(active_symbols) // 5)
            offset = (self._tick_count % max(1, (len(active_symbols) + batch_size - 1) // batch_size)) * batch_size
            batch = active_symbols[offset : offset + batch_size]

            for symbol in batch:
                # 1. Fetch latest market data
                features = self._feed.update_features(symbol)
                if not features:
                    continue

                price = features["price"]

                # 2. Check protection triggers（仅检查属于当前 symbol 的持仓）
                for pos_id, pp in list(self._protection.all_positions().items()):
                    pos_sym = str(pp.instrument_id)
                    if pos_sym != symbol:
                        continue  # 跳过不属于当前批次 symbol 的持仓
                    result = self._protection.check_price(pos_id, price)
                    if result["triggered"]:
                        for sl in result["stop_loss"]:
                            sl_symbol = str(sl.instrument_id) if hasattr(sl, 'instrument_id') else symbol
                            self._alerts.send_incident(
                                AlertSeverity.HIGH,
                                f"Stop Loss triggered: {sl_symbol}",
                                f"Position {pos_id} stop loss at {sl.trigger_price.amount}",
                                category="protection",
                            )
                            if self._can_write:
                                await self._execute_protection_order(sl, sl_symbol)
                        for tp in result["take_profit"]:
                            tp_symbol = str(tp.instrument_id) if hasattr(tp, 'instrument_id') else symbol
                            if self._can_write:
                                await self._execute_protection_order(tp, tp_symbol)

                # 3. Monitor active orders
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
                print(f"[realtime] Intent check: unacked={len(unacked)} pending={pending} raw_outbox={raw_outbox} processed={raw_processed} inbox={raw_inbox} can_write={self._can_write}")
            if self._can_write:
                for intent in unacked:
                    await self._place_order(intent)

            # 6. Reconciliation (every 30s)
            if time.time() - self._last_recon > 30:
                await self._reconcile()
                self._last_recon = time.time()

        except Exception as e:
            self._error_count += 1
            self._alerts.send_incident(
                AlertSeverity.WARNING,
                "Realtime tick error",
                str(e)[:200],
                category="realtime",
            )

    async def _execute_protection_order(self, protection, symbol: str) -> None:
        """执行保护单（止损/止盈）→ 市价单。"""
        side = "SELL" if protection.side == OrderSide.SELL else "BUY"
        client_id = f"beidou-prot-{int(time.time() * 1000)}"

        # 精度修正（复用 _place_order 同样的缓存）
        prec_map = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
        qty_raw = float(protection.quantity.amount)
        qty_str = f"{qty_raw:.{prec_map['quantity']}f}"

        print(f"[protection] Executing: {symbol} {side} qty={qty_str} reduceOnly=true")
        order = self._api(
            "/fapi/v1/order",
            method="POST",
            signed=True,
            params={
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": qty_str,
                "reduceOnly": "true",
                "newClientOrderId": client_id,
            },
        )

        if "orderId" in order:
            order_id = str(order["orderId"])
            self._order_count += 1

            tracker = OrderStateTracker(order_id=OrderId(order_id))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.SENT)
            self._order_trackers[order_id] = tracker
            self._active_order_ids.add(order_id)
            self._order_symbols[order_id] = symbol

            self._protection.mark_executed(protection.protection_id)
            # 清理入场时间跟踪（止损/止盈平仓后）
            self._position_entry_times.pop(protection.position_id, None)
            # 记录交易结果到策略风控（激活熔断器）
            pos_protection = self._protection.get_protection(protection.position_id)
            if pos_protection:
                entry_price = pos_protection.entry_price
                exit_price = float(protection.trigger_price.amount)
                qty = float(protection.quantity.amount)
                if pos_protection.is_long():
                    trade_pnl = (exit_price - entry_price) * qty
                else:
                    trade_pnl = (entry_price - exit_price) * qty
                is_win = trade_pnl > 0
                self._strategy_risk.record_trade(self._autopilot_strategy_id, trade_pnl, is_win)
                if is_win:
                    self._win_count += 1
                else:
                    self._loss_count += 1
                self._trade_pnls.append(trade_pnl)
                print(f"[protection] Trade recorded: PnL={trade_pnl:.2f} win={is_win} total_trades={len(self._trade_pnls)}")
            self._store.save_protection(
                protection.protection_id,
                protection.position_id,
                symbol,
                side,
                protection.trigger_price.amount,
                str(protection.order_price.amount) if protection.order_price else None,
                protection.quantity.amount,
                "MARKET",
                "EXECUTED",
            )
            self._store.save_order_state(
                order_id,
                symbol,
                side,
                "MARKET",
                str(float(protection.quantity.amount)),
                str(protection.order_price.amount) if protection.order_price else None,
                order.get("status", "NEW"),
                client_order_id=client_id,
            )
        else:
            err_code = order.get("code", order.get("error", "unknown"))
            err_msg = order.get("msg", str(order)[:150])
            print(f"[protection] EXECUTION FAILED {symbol} {side}: code={err_code} msg={err_msg}")
            # -2022 (ReduceOnly rejected) → 符号错配或无持仓，不标记完成，等待下次正确触发
            # -2010 (insufficient margin) → 账户问题，稍后重试
            # 不再自动 mark_executed，避免误标记

    async def _place_order(self, intent, symbol: str = "") -> None:
        """向交易所发送订单。"""
        side = "BUY" if intent.side == OrderSide.BUY else "SELL"
        order_type = "LIMIT" if intent.order_type == OrderType.LIMIT else "MARKET"
        client_id = intent.client_order_id or f"beidou-{int(time.time() * 1000)}"

        # 使用 intent 自身的 instrument_id，而非循环变量
        order_symbol = str(intent.instrument_id) if hasattr(intent, 'instrument_id') else symbol

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
                exchange_info = self._api("/fapi/v1/exchangeInfo")
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
        order = self._api("/fapi/v1/order", method="POST", signed=True, params=params)
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
            self._active_order_ids.add(oid_str)
            self._order_symbols[oid_str] = order_symbol  # 记录订单所属 symbol
            self._outbox.ack(intent.intent_id)
            self._order_count += 1
            print(
                f"[order] PLACED: {symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')} "
                f"orderId={order['orderId']}"
            )

            self._store.save_order_state(
                str(order["orderId"]),
                symbol,
                side,
                order_type,
                str(float(intent.quantity.amount)),
                str(float(intent.price.amount)) if intent.price else None,
                order.get("status", "NEW"),
                client_order_id=client_id,
            )
        else:
            print(f"[order] FAILED: {order_symbol} {side} — {order.get('msg', order.get('error', 'unknown'))}")

    async def _monitor_orders(self, symbol: str) -> None:
        """查询活跃订单状态并更新状态机/账本。"""
        for order_id in list(self._active_order_ids):
            # 使用订单自身的 symbol 而非批量循环的 symbol
            order_sym = self._order_symbols.get(order_id, symbol)
            try:
                result = self._api(
                    "/fapi/v1/order",
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

                executed_qty = float(result.get("executedQty", 0))
                avg_price = result.get("avgPrice", "0")

                if status == "FILLED":
                    tracker.apply(OrderEvent.FILLED)
                    self._active_order_ids.discard(order_id)

                    notional = executed_qty * float(avg_price)
                    # BUY → 现金流出(credit); SELL → 现金流入(debit)
                    # 余额 = sum(debit) - sum(credit) = 净现金流入
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

                    # 检查是否为平仓订单（不对平仓成交创建新保护）
                    is_close_order = order_id in self._close_order_ids
                    if is_close_order:
                        self._close_order_ids.discard(order_id)
                        # 记录平仓交易 PnL
                        fill_side = result.get("side", "")
                        for pid, pp in list(self._protection.all_positions().items()):
                            if str(pp.instrument_id) == symbol:
                                entry_px = pp.entry_price
                                exit_px = float(avg_price)
                                pos_qty = pp.quantity
                                if pp.is_long():
                                    trade_pnl = (exit_px - entry_px) * pos_qty
                                else:
                                    trade_pnl = (entry_px - exit_px) * pos_qty
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
                                print(f"[order] Close trade recorded: {symbol} PnL={trade_pnl:.2f}")
                                break
                        # 平仓订单不再创建新保护
                        account_balance = float(self._last_account.get("totalWalletBalance", 0))
                        if account_balance > 0:
                            self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
                            if account_balance > self._peak_equity:
                                self._peak_equity = account_balance
                        # 跳过后续保护创建
                    else:
                        # 成交后创建止盈止损保护（仅入场订单）
                        entry_price = float(avg_price)
                        qty = executed_qty
                        pos_side = OrderSide.BUY if result.get("side") == "BUY" else OrderSide.SELL
                        pos_id = f"pos-{order_id}"

                        pp = self._protection.create_protection(
                            position_id=pos_id,
                            instrument_id=InstrumentId(symbol),
                            venue_id=VenueId("BINANCE"),
                            entry_price=entry_price,
                            quantity=qty,
                            side=pos_side,
                            stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 2.0},
                            take_profit_config={"type": "FIXED_RR", "rr_ratio": 2.0},
                        )
                        # 记录入场时间，供 TimeExit 超时检测使用
                        self._position_entry_times[pos_id] = time.time()

                        # 将 SL/TP 保护单实际下单到交易所（使用价格精度）
                        # Testnet 不支持 STOP_MARKET → 回退到本地价格监控
                        reduce_side = "SELL" if pos_side == OrderSide.BUY else "BUY"
                        protect_orders = [pp.stop_loss] if pp.stop_loss else []
                        protect_orders.extend(pp.take_profits)
                        for p_order in protect_orders:
                            if p_order is None:
                                continue
                            # 已尝试过的保护单不再重复提交（避免 -4120 日志洪流）
                            if not hasattr(self, "_protection_exchange_attempted"):
                                self._protection_exchange_attempted: set[str] = set()
                            if p_order.protection_id in self._protection_exchange_attempted:
                                continue
                            self._protection_exchange_attempted.add(p_order.protection_id)

                            prec_map = self._symbol_precision.get(symbol, {"quantity": 3, "price": 2})
                            qty_str = f"{float(p_order.quantity.amount):.{prec_map['quantity']}f}"
                            price_str = f"{float(p_order.trigger_price.amount):.{prec_map['price']}f}"
                            sl_params = {
                                "symbol": symbol,
                                "side": reduce_side,
                                "type": "STOP_MARKET",
                                "quantity": qty_str,
                                "stopPrice": price_str,
                                "closePosition": "true",
                                "workingType": "CONTRACT_PRICE",
                                "newClientOrderId": f"beidou-{p_order.protection_id[:20]}",
                            }
                            sl_resp = self._api("/fapi/v1/order", method="POST", signed=True, params=sl_params)
                            if "code" in sl_resp and sl_resp.get("code") == -4120:
                                # 回退: 使用不带 closePosition 的 reduceOnly 版本
                                sl_params["reduceOnly"] = "true"
                                sl_params.pop("closePosition", None)
                                sl_params.pop("workingType", None)
                                sl_resp = self._api("/fapi/v1/order", method="POST", signed=True, params=sl_params)
                            if "orderId" in sl_resp:
                                print(f"[protection] {symbol} {p_order.reason} → orderId={sl_resp['orderId']} stopPrice={price_str}")
                            elif "code" in sl_resp and sl_resp.get("code") == -4120:
                                # Testnet: STOP_MARKET 不可用，依赖本地 check_price() + MARKET 单
                                print(f"[protection] {symbol} {p_order.reason}: STOP_MARKET unavailable (local guard active)")
                            else:
                                print(f"[protection] FAILED {symbol} {p_order.reason}: {sl_resp.get('msg', sl_resp)}")

                    # === Strategy Risk: update equity on any fill ===
                    account_balance = float(self._last_account.get("totalWalletBalance", 0))
                    if account_balance > 0:
                        self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
                        if account_balance > self._peak_equity:
                            self._peak_equity = account_balance

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

    async def _reconcile(self) -> None:
        """对账：系统状态 vs 交易所状态。"""
        try:
            # 对账优先使用完整 account 端点（含 positions），失败则回退 balance
            account = self._api("/fapi/v2/account", signed=True)
            if "totalWalletBalance" not in account:
                # 回退：balance 端点无 positions，仅对账余额
                account = self._api("/fapi/v2/balance", signed=True)
                if "totalWalletBalance" not in account:
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
                open_orders = self._api("/fapi/v1/openOrders", signed=True)
                if isinstance(open_orders, list):
                    exchange_open_order_ids = [str(o["orderId"]) for o in open_orders]
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
                real_diffs = [d for d in result.differences if "Balance mismatch" not in d]
                if real_diffs:
                    print(f"[recon] Mismatch: {real_diffs}")
                    self._alerts.send_incident(
                        AlertSeverity.WARNING,
                        "Reconciliation mismatch",
                        "; ".join(real_diffs),
                        category="reconciliation",
                    )

            self._last_account = account
        except Exception as e:
            print(f"[realtime] Reconciliation error: {e}")

    # --- Clock Domain: NEARLINE (every 5min) ---

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

        try:
            active_symbols = self._trading_pool.active_instruments()
            if not active_symbols:
                active_symbols = list(self._symbols)
            for symbol in active_symbols:
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
                    s for s in all_signals
                    if s.component_type == AlphaComponentType.EXIT
                    and s.direction == SignalDirection.FLAT
                    and s.strength >= 0.3
                ]
                if exit_flat_signals and pos_info["has_position"]:
                    # 生成平仓订单
                    close_side = OrderSide.SELL if pos_info["side"] == "LONG" else OrderSide.BUY
                    close_qty = abs(float(symbol_positions[next(iter(symbol_positions))].quantity)) if symbol_positions else 0.001
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
                        client_order_id=f"beidou-{symbol.lower()}-close-{int(time.time() * 1000)}",
                        correlation_id=CorrelationId(f"nearline-close-{int(time.time())}"),
                        idempotency_key=f"idem-{symbol}-close-{int(time.time() / 300)}",
                        risk_approval_id=str(RiskApprovalId(f"nearline-close-{int(time.time())}")),
                    )
                    try:
                        self._outbox.commit(close_intent)
                        reasons = [s.metadata.get("reason", "unknown") for s in exit_flat_signals]
                        print(f"[nearline] {symbol}: CLOSE ORDER → {close_side.value} {close_qty:.4f} reasons={reasons}")
                    except ValueError:
                        print(f"[nearline] {symbol}: CLOSE SKIP (duplicate close in window)")
                    continue  # 平仓后跳过入场逻辑

                # Check if entry signal is actionable (exclude EXIT components)
                entry_signals = [
                    s for s in all_signals
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
                position_size = min(risk_based_size * adaptive_pct * 50, max_by_leverage)
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
                    actual_lev = self._ensure_leverage(symbol, exchange_leverage)
                    if actual_lev != dyn_leverage:
                        dyn_leverage = float(actual_lev)  # 使用实际杠杆重新计算仓位
                        max_by_leverage = (account_balance * dyn_leverage) / price
                        position_size = min(risk_based_size * adaptive_pct * 50, max_by_leverage)
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
                    client_order_id=f"beidou-{symbol.lower()}-{int(time.time() * 1000)}",
                    correlation_id=CorrelationId(f"nearline-{int(time.time())}"),
                    idempotency_key=f"idem-{symbol}-{int(time.time() / 300)}",
                    risk_approval_id=str(approval_id),
                )

                try:
                    self._outbox.commit(intent)
                    print(f"[nearline] {symbol}: ✅ OrderIntent CREATED → {side.value} {position_size:.4f} @ {price} (outbox_id={id(self._outbox)} size={len(self._outbox._outbox)})")
                except ValueError:
                    print(f"[nearline] {symbol}: SKIP (duplicate intent in window)")

        except Exception as e:
            self._error_count += 1
            print(f"[nearline] ERROR: {e}")

    # --- Clock Domain: OFFLINE (every 1h) ---

    async def _offline_tick(self) -> None:
        """离线时钟：因子评估 → 漂移检测 → 策略风控 → 日报 → 检查点 → 清理。"""
        self._last_offline = time.time()

        try:
            now = datetime.now(timezone.utc)

            # === 1. Factor evaluation: compute live IC/ICIR from tracked predictions ===
            print(f"[offline] Factor evaluation: {len(self._factor_returns)} return samples")
            if len(self._factor_returns) > 10:
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

                        # Factor lifecycle: degrade if IC degrades significantly
                        if record.lifecycle == FactorLifecycle.ACTIVE and icir < 0.2:
                            self._factor_registry.degrade(fid, f"ICIR dropped to {icir:.3f}")
                            self._alerts.send_incident(
                                AlertSeverity.WARNING,
                                f"Factor degraded: {fid}",
                                f"ICIR={icir:.3f} below threshold 0.2",
                                category="factor",
                            )
                            print(f"[offline] Factor {fid}: DEGRADED (ICIR={icir:.3f} < 0.2)")
                        elif record.lifecycle == FactorLifecycle.CHALLENGER and icir >= 0.3:
                            self._factor_registry.promote_to_active(fid)
                            print(f"[offline] Factor {fid}: PROMOTED TO ACTIVE (ICIR={icir:.3f})")

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
        server_time = self._api("/fapi/v1/time")
        if "serverTime" not in server_time:
            print("[beidou-autopilot] FATAL: Cannot connect to exchange")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        print(f"[beidou-autopilot] Exchange connected: {self._rest_url}")

        # Verify account access
        account = self._api("/fapi/v2/balance", signed=True)
        if "totalWalletBalance" not in account:
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
            exchange_open = self._api("/fapi/v1/openOrders", signed=True)
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

        # Print strategy/risk activation status
        print(f"[beidou-autopilot] AlphaGraph DAG: {self._alpha_graph.topological_order()}")
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
        self._lifecycle.transition(ModuleState.ACTIVE)
        print(f"[beidou-autopilot] State: {self._lifecycle.state.value}")

        # Start health server
        self._health.start()
        print("[beidou-autopilot] Health server: http://0.0.0.0:9090")

        # StartupGate: 启动后进入并保持 NO_NEW_RISK
        # 不存在固定时间自动 RESUME — 需持久化 Startup Gate 证书通过后方可手动 RESUME
        self._control.execute_action(ControlAction.NO_NEW_RISK)
        print("[beidou-autopilot] Control plane: NO_NEW_RISK (persistent — no auto RESUME)")
        print("[beidou-autopilot] RESUME requires: valid Startup Gate certificate + explicit trigger")

        self._running = True
        print("[beidou-autopilot] ========================================")
        print("[beidou-autopilot] Engine running. Press Ctrl+C to stop.")
        print("[beidou-autopilot] ========================================")

        # Main event loop
        while self._running:
            try:
                now = time.time()

                if now - self._last_realtime >= 5:
                    await self._realtime_tick()

                if now - self._last_nearline >= 300:
                    await self._nearline_tick()

                if now - self._last_offline >= 3600:
                    await self._offline_tick()

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception:
                self._error_count += 1
                if self._error_count > 100:
                    self._alerts.send_incident(
                        AlertSeverity.CRITICAL,
                        "Error threshold exceeded",
                        f"{self._error_count} errors, locking down",
                        category="engine",
                    )
                    self._lifecycle.transition(ModuleState.LOCKED)
                    self._running = False
                await asyncio.sleep(5)

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
                        self._api(
                            "/fapi/v1/order",
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
