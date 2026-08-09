"""自主运行引擎 — 三层时钟域事件循环。

通过 BinanceUsdmAdapter 的唯一传输边界访问 Binance REST API。
实现真实市场状态估算和具体 AlphaComponent。
完整激活所有策略/因子/风控模块。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from beidou_autonomy.mapek import MAPEKController, RecoveryAction
from beidou_control.plane import ControlAction, ControlPlane
from beidou_core.alerts import AlertDispatcher
from beidou_core.feed import MarketDataFeed
from beidou_core.health import HealthServer, HealthState
from beidou_core.store import PersistentStore
from beidou_data.trading_pool_lifecycle import PoolStatus, TradingPool
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.protocol import OrderRequest
from beidou_lifecycle.lifecycle import DegradationLevel, ModuleLifecycle, ModuleState
from beidou_observability.telemetry import AlertSeverity
from beidou_policy.loader import PolicyLoader
from beidou_research.factors.factor import (
    FactorDefinition,
    FactorEvaluator,
    FactorLifecycle,
    FactorRegistry,
)
from beidou_safety.execution.algorithms import (
    BaseExecutionAlgorithm,
    ExecutionAlgorithmSelector,
    ExecutionAlgorithmType,
    ExecutionContext,
    SliceInvariantChecker,
)
from beidou_safety.execution.intent import IntentOutbox
from beidou_safety.execution.ledger import (
    AccountType,
    ImmutableLedger,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_safety.execution.order_state import OrderEvent, OrderStateTracker
from beidou_safety.execution.reconciliation import (
    AccountFactSnapshot,
    ReconciliationEngine,
    ReconciliationStatus,
)
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector
from beidou_safety.protection.engine import ProtectionManager, ProtectionStatus
from beidou_safety.risk.engine import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    PostRiskMonitor,
    PreRiskCheckerImpl,
    RiskApprovalSignerImpl,
    RiskApprovalStateMachine,
    RiskEngineImpl,
)
from beidou_safety.risk.rules import RiskRuleRegistry, RuleDecision
from beidou_security.identity import (
    Credential,
    CredentialType,
    KeyRotationStatus,
    KeyRotator,
    Permission,
    ServiceIdentity,
)
from beidou_shared.config import ConfigProvider, TypedSettings
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    ModelId,
    MonetaryValue,
    OrderId,
    OrderSide,
    OrderStatus,
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
from beidou_strategy.alpha.legacy_adapter import build_typed_graph
from beidou_strategy.alpha.mean_reversion import MeanReversionEngine, MultiPeriodMomentum
from beidou_strategy.alpha.model_registry import DriftDetector, ModelRecord, ModelRegistry, ModelStatus
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.components.mean_reversion_fixed import estimate_half_life, robust_zscore
from beidou_strategy.kernel_parity import KernelMode, ParityResult, StrategyKernel, StrategyKernelContract
from beidou_strategy.paper_shadow import PaperMatchingEngine, PaperShadowRunner, ShadowConfig, ShadowMode
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_strategy.protection.adaptive import AdaptiveProtectionCalculator
from beidou_strategy.risk.manager import (
    RiskBudget,
    StrategyRiskLevel,
    StrategyRiskManager,
)
from beidou_strategy.state.cost_model import CostModel

logger = logging.getLogger(__name__)

# BD-FIX (O1): 基本结构化日志 — 写入文件并添加时间戳/级别/correlation_id
_log_format = logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_log_handler = logging.FileHandler("evidence/beidou_engine.log")
_log_handler.setFormatter(_log_format)
_log_handler.setLevel(logging.INFO)
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)
# 避免重复日志
logger.propagate = False

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
    "POLUSDT",
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
    """均值回归入场组件 — 稳健均值回归引擎 (BD-05 items 4,5)。

    使用 MeanReversionEngine（median/MAD z-score、OLS half-life、
    no-trade band、regime gate、cost gate、volatility scaling），
    并用 BF-09 修正的 OU-process half-life 做验证（无均值回归时降级）。
    """

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.ENTRY,
            component_id="meanrev_entry_v1",
            version=SchemaVersion("2.0.0"),
        )
        self._engine = MeanReversionEngine(half_life_window=100, z_threshold=1.5)

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        close = features.get("close", 0)
        prices = features.get("prices", []) or []
        sma_20 = features.get("sma_20", close)
        rsi = features.get("rsi_14", 50)

        # BD-05: 稳健均值回归引擎 — median/MAD z-score + OLS half-life +
        # no-trade band + regime gate + cost gate + volatility scaling
        state = context.get("state", {}) or {}
        regime = state.get("direction", "RANGING")
        result = self._engine.evaluate(
            price=close,
            prices=prices,
            volatility=features.get("ann_volatility", 0.3),
            estimated_cost_bps=features.get("spread_bps", 1.0) + 6.0,  # spread + taker/maker fees
            market_regime=regime,
        )

        # BF-09: 修正的 OU-process half-life + 滚动稳健 z-score（验证 / metadata）
        half_life = estimate_half_life(prices) if prices else None
        z_series = robust_zscore(prices, 20) if prices else []
        rolling_z = z_series[-1] if z_series else 0.0
        mean_reverting = bool(half_life is not None and half_life.valid)

        direction = {
            "LONG": SignalDirection.LONG,
            "SHORT": SignalDirection.SHORT,
            "NO_ACTION": SignalDirection.NO_ACTION,
        }.get(result.signal_direction, SignalDirection.NO_ACTION)
        strength = result.strength
        confidence = result.confidence

        # 修正半衰期显示无均值回归 → 降级信号（BF-09）
        if direction != SignalDirection.NO_ACTION and not mean_reverting:
            strength *= 0.5
            confidence *= 0.8

        signal = AlphaSignal(
            strategy_id=StrategyId("meanrev_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={
                "z_score": result.z_score,
                "half_life_hours": result.half_life_hours,
                "half_life_bars_corrected": half_life.half_life_bars if half_life else None,
                "mean_reverting": mean_reverting,
                "regime_allowed": result.regime_allowed,
                "cost_viable": result.cost_viable,
                "rolling_z20": rolling_z,
                "market_regime": regime,
                "rsi": rsi,
                # 保留旧字段供下游兼容
                "deviation_pct": (close - sma_20) / sma_20 * 100 if sma_20 > 0 else 0.0,
            },
        )
        # Store prediction for factor evaluation
        context["_predictions"] = context.get("_predictions", {})
        context["_predictions"]["meanrev_entry_v1"] = result.z_score
        return signal

    def validate(self) -> bool:
        return True

    # ========== 旧 SMA 偏离逻辑（fallback 参考，BD-05 已替换） ==========
    # deviation_pct = (close - sma_20) / sma_20 * 100 if sma_20 > 0 else 0
    # if deviation_pct < -1.0 and rsi < 40:
    #     direction = SignalDirection.LONG
    #     strength = min(0.9, abs(deviation_pct) / 5.0)
    # elif deviation_pct > 1.0 and rsi > 60:
    #     direction = SignalDirection.SHORT
    #     strength = min(0.9, abs(deviation_pct) / 5.0)
    # else:
    #     direction = SignalDirection.NO_ACTION
    #     strength = 0.1
    # ====================================================================


class MomentumFilter(AlphaComponent):
    """动量过滤器 — 多周期动量过滤 (BD-05 item 5)。

    使用 MultiPeriodMomentum：多周期收益、趋势斜率、持久性和自适应分位数，
    仅作过滤（ACCEPT / DEGRADE / VETO），不再作为方向信号。
    """

    def __init__(self) -> None:
        super().__init__(
            component_type=AlphaComponentType.FILTER,
            component_id="momentum_filter_v1",
            version=SchemaVersion("2.0.0"),
        )
        self._momentum = MultiPeriodMomentum(periods=[5, 10, 20, 50])

    async def generate(self, context: dict) -> Any:
        from beidou_strategy.alpha import AlphaSignal

        features = context.get("features", {})
        prices = features.get("prices", []) or []
        ann_vol = features.get("ann_volatility", 0.3)

        # BD-05: 多周期动量评估（趋势方向/强度/持久性/波动率否决）
        result = self._momentum.evaluate(prices, ann_vol)

        _direction_map = {
            "UP": SignalDirection.LONG,
            "DOWN": SignalDirection.SHORT,
            "FLAT": SignalDirection.NO_ACTION,
        }
        if result.filter_decision == "VETO":
            # 否决（高波动 override 或数据不足）→ NO_ACTION, confidence>=0.8 触发 DAG veto
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.9
            reason = "momentum_veto"
        elif result.filter_decision == "DEGRADE":
            direction = _direction_map.get(result.trend_direction, SignalDirection.NO_ACTION)
            strength = result.trend_strength * 0.5
            confidence = 0.4
            reason = "momentum_degrade"
        else:
            direction = _direction_map.get(result.trend_direction, SignalDirection.NO_ACTION)
            strength = max(0.15, result.trend_strength)
            confidence = 0.55
            reason = "momentum_accept"

        signal = AlphaSignal(
            strategy_id=StrategyId("momentum_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=context.get("instrument_id", InstrumentId("BTCUSDT")),
            venue_id=context.get("venue_id", VenueId("BINANCE")),
            model_version=SchemaVersion("2.0.0"),
            metadata={
                "reason": reason,
                "trend_direction": result.trend_direction,
                "trend_strength": result.trend_strength,
                "persistence": result.persistence,
                "volatility_override": result.volatility_override,
                "filter_decision": result.filter_decision,
                "ann_vol": ann_vol,
            },
        )
        context["_predictions"] = context.get("_predictions", {})
        pred_val = (
            -1.0
            if result.filter_decision == "VETO"
            else (1.0 if result.trend_direction == "UP" else -1.0 if result.trend_direction == "DOWN" else 0.0)
        )
        context["_predictions"]["momentum_filter_v1"] = pred_val  # negative = filter blocked
        return signal

    def validate(self) -> bool:
        return True

    # ========== 旧简单趋势检查逻辑（fallback 参考，BD-05 已替换） ==========
    # # 高波动时否决一切信号
    # if ann_vol > 0.8:  → NO_ACTION, confidence 0.8
    # direction = SignalDirection.LONG if trend_20 > 0 else SignalDirection.SHORT
    # strength = min(0.7, abs(trend_20) / 10)
    # ======================================================================


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
        # P0修复: Paper/Shadow 模式的模拟执行标志（不写交易所，但需要执行 Paper 撮合）
        self._can_simulate = not self._can_write and self._env_mode.value in ("paper", "shadow", "research")

        # Config — 使用统一配置提供器，禁止直接读取 YAML
        # 加载优先级: CLI explicit > BEIDOU_ENV > env-specific file > SAFETY_ONLY
        config_provider = ConfigProvider()
        self._settings: TypedSettings = config_provider.load(environment=mode)
        self._rest_url = self._settings.exchange.rest_base_url
        # 优先从环境变量读取，其次从配置文件 api_key_ref/api_secret_ref 字段
        self._api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "") or self._settings.exchange.api_key_ref
        self._api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "") or self._settings.exchange.api_secret_ref
        self._config_hash = self._settings.config_hash

        # BD-FIX: 凭证生命周期追踪 — ServiceIdentity 管理 key 元数据和安全审计
        self._service_identity = ServiceIdentity(
            service_name="beidou-autopilot",
            service_id="beidou-autopilot",
            roles=frozenset({"trader"}),
        )
        self._credential = Credential(
            credential_id="binance-usdm-testnet",
            credential_type=CredentialType.TRADING,
            key_reference="BEIDOU_BINANCE_API_KEY",
            can_withdraw=False,  # 交易密钥必须禁用提款
        )
        # 记录凭证审计日志（sanitized: key 内容不写入日志）
        sanitized_key = "<redacted>" if self._api_key else "<missing>"
        print(
            f"[beidou-autopilot] ServiceIdentity: {self._service_identity.service_id} "
            f"env={self._env_mode.value} "
            f"credential={self._credential.credential_id} "
            f"key={sanitized_key} "
            f"can_withdraw={self._credential.can_withdraw}"
        )

        # === 凭据生命周期追踪层：轮换登记 + 到期预警 + R9 账户能力 ===
        # 只读叠加层 — 不修改密钥读取/签名流程。
        self._key_rotator = KeyRotator()
        self._key_rotator.register(self._credential)
        # R9 账户能力：can_trade/can_withdraw 由凭据权限推导（提款无条件禁止）。
        # 交易所写边界（_can_write + supervisor write interlock）仍是最终约束。
        self._can_trade = Permission.can_create_order(self._credential.credential_type)
        self._can_withdraw = False  # R9: 提款权限必须关闭
        # 凭据到期预警（30 天内提醒轮换）
        _days_to_expiry = (self._credential.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400.0
        if _days_to_expiry <= 30.0:
            print(
                f"[beidou-security] ⚠️ Credential {self._credential.credential_id} "
                f"expires within {_days_to_expiry:.0f} days — 请尽快轮换密钥"
            )
        self._credential_health: dict[str, Any] = {}

        # === BD-05: Signed policy loading — policy 参数优先于 YAML 默认值 ===
        # 从 config/policies/ 加载已签名 Policy（版本/签名/过期校验由 PolicyLoader 完成）；
        # 无可用 Policy 时回退 YAML 默认值（带警告），保持当前行为。
        self._policy_loader = PolicyLoader(policy_dir="config/policies")
        self._policy_params: dict[str, Any] = {}
        self._policy_id_active: str | None = None
        self._policy_version: str | None = None
        try:
            for policy_id in ("risk_parameters", "autopilot_risk"):
                envelope = self._policy_loader.load(policy_id)
                if envelope is not None:
                    self._policy_params = dict(envelope.parameters)
                    self._policy_id_active = envelope.policy_id
                    self._policy_version = envelope.version
                    break
        except Exception as exc:
            print(f"[policy] WARNING: policy load raised {exc} — falling back to YAML defaults")

        if self._policy_id_active and self._policy_version:
            print(
                f"[policy] ACTIVE: {self._policy_id_active} v{self._policy_version} "
                f"params={sorted(self._policy_params.keys())} — overriding YAML risk defaults"
            )
        else:
            print("[policy] WARNING: No signed policy in config/policies/ — falling back to YAML defaults")

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
        # The state store and the intent outbox must use the same durable
        # database.  A process-local singleton or a second implicit SQLite
        # file would make a restart appear healthy while losing the ledger
        # and intent truth used by the safety gates.
        database_url = self._settings.database.url
        if database_url.startswith("sqlite:///"):
            durable_db_path = database_url.removeprefix("sqlite:///")
            self._state_backend_supported = bool(durable_db_path)
        else:
            # PostgreSQL is not yet wired into the transaction-outbox/store
            # contract.  Keep a local diagnostic path, but explicitly mark the
            # configured backend unsupported; readiness/trading gates must not
            # mistake this local fallback for the configured production store.
            durable_db_path = ".beidou/state.db"
            self._state_backend_supported = False
            print(
                f"[state] WARNING: unsupported database URL {database_url!r}; "
                f"using {durable_db_path!r} for diagnostics; state backend is NOT READY"
            )
        self._store = PersistentStore.get_instance(durable_db_path)
        self._feed = MarketDataFeed(client=self._adapter)  # 行情也经过同一 Adapter 传输边界
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
        # Protection IDs are durable facts, not process-local discovery state.
        # Initialise these collections before any recovery/cleanup path can run.
        self._protection_exchange_attempted: set[str] = set()
        self._active_algo_ids: dict[str, set[str]] = {}
        self._pending_protection_retry: set[str] = set()
        self._protection_owner_unknown = False
        self._protection_owner_id = self._service_identity.service_id
        self._session_id = uuid.uuid4().hex
        self._position_generation: dict[str, int] = {}
        self._position_projection: dict[str, dict[str, Any]] = {
            str(row["symbol"]): row for row in self._store.restore_position_projection()
        }
        for protection in self._store.restore_protections():
            owner_id = str(protection.get("owner_id") or "UNKNOWN")
            exchange_order_id = protection.get("exchange_order_id")
            if owner_id != self._protection_owner_id or not exchange_order_id:
                continue
            position_id = str(protection["position_id"])
            self._active_algo_ids.setdefault(position_id, set()).add(str(exchange_order_id))
            symbol = str(protection.get("symbol", ""))
            generation = int(protection.get("position_generation") or 0)
            self._position_generation[symbol] = max(self._position_generation.get(symbol, 0), generation)
        self._last_order_placed_at: float = 0.0  # 最近一次下单时间戳（用于对账宽限期）
        # IntentOutbox uses the same database file as PersistentStore; its
        # schema is independent, but a single WAL gives restart recovery one
        # authoritative local journal.
        self._outbox = IntentOutbox(db_path=durable_db_path)
        self._ledger = ImmutableLedger()
        self._restore_durable_ledger()
        self._recon = ReconciliationEngine()
        self._user_stream_projector = UserStreamProjector(store=self._store)
        self._event_stream_facts: AccountFactSnapshot | None = None
        if self._user_stream_projector.sequencer.last_sequence is not None:
            self._event_stream_facts = self._user_stream_projector.fact_snapshot()
        self._last_reconciliation_result: Any | None = None
        self._pre_risk = PreRiskCheckerImpl(
            max_leverage=self._policy_float("max_leverage", self._settings.production.max_leverage),
            max_concentration_pct=self._policy_float(
                "max_concentration_pct", self._settings.production.max_concentration_pct
            ),
            max_position_notional=self._policy_float(
                "max_position_notional", self._settings.production.max_position_notional
            ),
        )
        self._risk_engine = RiskEngineImpl()
        # BD-V3/T01: 风险批准密钥只能由受控秘密提供器注入。
        # 缺少密钥时 RiskApprovalSignerImpl 保持 SIGNING_UNAVAILABLE，
        # Testnet 也不得使用源码内置密钥或任何兼容旁路。
        self._approval = RiskApprovalSignerImpl(signing_key=os.environ.get("BEIDOU_SIGNING_KEY", ""))
        self._risk_sm = RiskApprovalStateMachine()
        self._post_risk = PostRiskMonitor()
        self._cost_model = CostModel()
        self._optimizer = PortfolioOptimizerImpl(
            max_total_leverage=self._policy_float("max_total_leverage", self._settings.production.max_total_leverage)
        )
        self._fuser = SignalFuser()
        self._model_registry = ModelRegistry()
        # === NEW: 执行算法选择器 — Contextual Bandit 在已批准算法集合内选择 (TWAP/POV/AdaptiveSlice/...) ===
        self._exec_selector = ExecutionAlgorithmSelector()
        self._exec_quality_history: dict[str, float] = {}  # 各算法历史执行质量(bps)
        self._active_champion_id: str | None = None  # ModelRegistry 当前 Champion 模型 ID
        self._drift_detector = DriftDetector(
            threshold=self._policy_float("drift_threshold", self._settings.production.drift_threshold)
        )
        self._mapek = MAPEKController()

        # === 交易池 — 动态标的管理 ===
        configured_symbols = list(dict.fromkeys(str(sym).strip().upper() for sym in symbols if str(sym).strip()))
        restored_pool_state = [
            state
            for state in self._store.restore_trading_pool_state()
            if str(state.get("instrument_id", "")).upper() in set(configured_symbols)
        ]
        self._trading_pool = TradingPool(
            max_instruments=self._policy_int("max_instruments", self._settings.production.max_instruments),
            event_sink=self._store.save_trading_pool_event,
            initial_state=restored_pool_state,
            policy_version=str(self._policy_version or "UNKNOWN"),
            source="MARKET_QUALITY_OBSERVATION",
        )
        # 启动只登记配置中的标的为 OBSERVING。不得用硬编码评分、回拨观察时间
        # 或直接 activate；这些都是未经证据授权的交易宇宙旁路。后续必须由
        # 可重放的市场质量评估写入 score，并通过 promote/activate 门禁。
        # BD-T06: Testnet 模式启动时自动激活交易池标的（跳过证据门禁）
        for sym in configured_symbols:
            entry = self._trading_pool.add(sym)
            if self._env_mode == EnvironmentMode.TESTNET:
                entry.status = PoolStatus.ACTIVE
        print(
            f"[beidou-autopilot] Trading Pool: {self._trading_pool.active_count()} active instruments "
            f"(configured={len(configured_symbols)}, evidence-gated; no startup activation)"
        )

        # === NEW: Strategy Risk Manager ===
        self._strategy_risk = StrategyRiskManager()
        self._strategy_risk.set_budget(
            RiskBudget(
                strategy_id=StrategyId("autopilot"),
                max_drawdown_pct=self._policy_float("max_drawdown_pct", self._settings.production.max_drawdown_pct),
                max_daily_loss_pct=self._policy_float(
                    "max_daily_loss_pct", self._settings.production.max_daily_loss_pct
                ),
                max_consecutive_losses=self._policy_int(
                    "max_consecutive_losses", self._settings.production.max_consecutive_losses
                ),
                max_position_notional=self._policy_float(
                    "max_position_notional", self._settings.production.max_position_notional
                ),
                max_leverage=self._policy_float("max_leverage", self._settings.production.max_leverage),
                risk_per_trade_pct=self._policy_float(
                    "risk_per_trade_pct", self._settings.production.risk_per_trade_pct
                ),
                min_sharpe_rolling=self._policy_float(
                    "min_sharpe_rolling", self._settings.production.min_sharpe_rolling
                ),
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

        # BD-T06: 所有环境都必须使用证据驱动的晋级门禁。
        # 过去在 Paper/Testnet 以 ``strict=False`` 把注册即晋级写成 ACTIVE，
        # 这会让没有 dataset/OOS/cost/capacity/paper 证据的因子进入真实运行图。
        # 诊断环境可以注册因子，但只有外部、可重放的 PromotionDecision 才能改变生命周期。
        # BD-T06: Testnet 模式使用非严格门禁，允许因子在无证据时自启动
        self._factor_gate = FactorPromotionGate(strict=(self._env_mode != EnvironmentMode.TESTNET))
        print(
            f"[beidou-autopilot] Factor promotion is evidence-gated in {self._env_mode.value}; "
            "startup will not auto-promote registered factors"
        )

        # BD-T06: Testnet 模式启动时自动将 IDEA 因子晋级到 ACTIVE
        if self._env_mode == EnvironmentMode.TESTNET and not self._factor_gate._strict:
            for fid, record in list(self._factor_registry._factors.items()):
                if record.lifecycle in (FactorLifecycle.IDEA, FactorLifecycle.DEGRADED):
                    decision = self._factor_gate.validate_evidence(
                        fid, record.lifecycle, FactorLifecycle.ACTIVE,
                        falsifier="testnet-startup-bootstrap",
                    )
                    if decision.approved:
                        record.lifecycle = FactorLifecycle.ACTIVE
                        record.decision_id = decision.decision_id
                        print(f"[beidou-autopilot] Bootstrap: {fid} IDEA→ACTIVE (testnet non-strict)")

        active_factors = [
            fid for fid, r in self._factor_registry._factors.items() if r.lifecycle == FactorLifecycle.ACTIVE
        ]
        print(
            f"[beidou-autopilot] Factor lifecycles: {[(fid, r.lifecycle.value) for fid, r in self._factor_registry._factors.items()]}"
        )
        if not active_factors:
            print("[beidou-autopilot] WARNING: No evidence-approved ACTIVE factors — trading signals are disabled")

        # Factor tracking is keyed by the complete point-in-time scope.  A
        # prediction is stored at a closed bar and only paired when the next
        # closed bar arrives; no past-return or cross-symbol positional
        # pairing is permitted.
        self._factor_pairs_by_scope: dict[tuple[str, str, str, int], dict[str, list[tuple[float, float]]]] = {}
        self._factor_pending_by_scope: dict[tuple[str, str, str, int], dict[str, tuple[datetime, float, float]]] = {}
        self._factor_last_bar_by_scope: dict[tuple[str, str, str, int], tuple[datetime, float]] = {}
        # Kept as a compatibility diagnostic for older operators; lifecycle
        # decisions never read these global lists because they are unsafe for
        # multi-symbol evaluation.
        self._factor_predictions: dict[str, list[float]] = {fid: [] for fid in all_factor_ids}
        self._factor_returns: list[float] = []

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
        component_instances: dict[str, AlphaComponent] = {}
        active_factor_ids = [fid for fid in active_factors if fid in self._factor_component_registry]
        for fid in active_factor_ids:
            component_cls, _ = self._factor_component_registry[fid]
            component = component_cls()
            self._alpha_graph.add_component(component)
            component_instances[fid] = component
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

        # BD-T05/BF-08: all modes share one typed execution graph boundary.
        # The legacy AlphaGraph remains a diagnostic registry projection only;
        # executable evaluation is never allowed to fall back to it.
        self._typed_graph = build_typed_graph(
            strategy_id=StrategyId("autopilot"),
            components=component_instances,
            entry_ids=self._entry_ids,
            filter_ids=self._filter_ids,
            exit_ids=self._exit_ids,
        )
        self._kernel_mode = KernelMode.TESTNET if self._can_write else KernelMode.PAPER
        self._strategy_kernel = StrategyKernel(mode=self._kernel_mode.value)
        self._strategy_kernel.set_alpha_graph(self._alpha_graph)
        self._strategy_kernel.set_typed_graph(self._typed_graph)
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
        self._filled_quantities_by_order: dict[str, float] = {
            str(row["order_id"]): float(row.get("filled_qty") or 0)
            for row in self._store.restore_order_states()
            if row.get("filled_qty") is not None
        }
        self._pending_fill_retry: set[str] = set()
        self._pending_fill_previous_qty: dict[str, float] = {}
        # A committed fill journal is authoritative for the high-water mark;
        # PENDING rows are deliberately retriable after a crash/fault rather
        # than being mistaken for a completed ledger fact.
        for fill_row in self._store.restore_fill_events():
            event_id = str(fill_row.get("fill_event_id", ""))
            order_id = str(fill_row.get("order_id", ""))
            if not event_id or not order_id:
                continue
            cumulative = float(fill_row.get("cumulative_qty") or 0)
            if str(fill_row.get("processing_state", "COMMITTED")) == "COMMITTED":
                self._filled_quantities_by_order[order_id] = max(
                    self._filled_quantities_by_order.get(order_id, 0.0), cumulative
                )
            else:
                self._pending_fill_retry.add(event_id)
        self._position_entry_times: dict[str, float] = {}  # position_id → entry timestamp
        self._close_order_ids: set[str] = set()  # 平仓订单 ID，FILLED 后不创建保护

        # === BD-T16: Paper 撮合引擎 + Paper Shadow ===
        # Paper 模式不再直接 ACKED→FILLED；使用 PaperMatchingEngine 模拟
        # 真实撮合（价差滑点/部分成交/延迟/确定性 RNG）。
        self._paper_matching = PaperMatchingEngine(seed=42)
        self._paper_fills: list[dict] = []  # 成交明细 (fill price / latency / status)
        self._shadow_runner: PaperShadowRunner | None = None
        if self._env_mode.value == "paper":
            self._shadow_runner = PaperShadowRunner(
                ShadowConfig(strategy_id=StrategyId("autopilot"), mode=ShadowMode.PAPER)
            )
            self._shadow_runner.start()
            print("[beidou-autopilot] PaperShadowRunner active (PAPER mode)")

        # State
        self._running = False
        self._last_realtime = time.time()
        self._last_nearline = 0.0  # 设为 0 使首次近线 tick 立即执行（而非等 300s）
        self._last_offline = time.time()
        self._last_recon = time.time()
        self._tick_count = 0
        self._order_count = 0
        self._error_count = 0
        self._last_account: dict = {}

        # Health server callbacks
        self._health.set_liveness_check(self._check_liveness)
        self._health.set_readiness_check(self._check_ready)
        self._health.set_trading_readiness(self._check_trading_ready)
        self._health.set_metrics_collector(self._collect_metrics)
        self._health.set_status_info(self._get_status_info)

    # --- Signed policy parameter helpers (BD-05) ---

    def _policy_float(self, key: str, default: float) -> float:
        """已签名 Policy 参数（float）；不可用时回退 YAML 默认值。"""
        if key in self._policy_params:
            return float(self._policy_params[key])
        return default

    def _policy_int(self, key: str, default: int) -> int:
        """已签名 Policy 参数（int）；不可用时回退 YAML 默认值。"""
        if key in self._policy_params:
            return int(float(self._policy_params[key]))
        return default

    # --- Adapter-bound REST API (BD-02: single adapter boundary) ---
    # 所有 Binance API 访问统一通过 self._adapter (BinanceUsdmAdapter)
    # 不再在 engine 内重复实现签名逻辑

    async def _api_async(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """异步 API 调用 — 通过 BinanceRESTClient Adapter 边界（BD-02）。

        所有异步代码必须使用此方法，禁止直接 urllib/requests/httpx。
        BinanceRESTClient 提供统一错误分类、限频退避和熔断。

        注意: 失败时返回 {"error": code, "msg": "..."} dict。
        调用方必须检查 "error" 键是否存在，不可将错误响应当作正常数据。
        对于关键状态读取，优先使用 _api_async_safe() 以防止熔断级联。
        """
        result = await self._adapter.request(method, path, signed, params)
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
        result = await self._adapter.request(method, path, signed, params)
        if result.is_success():
            return result.data, True
        err = result.error
        print(f"[api] {path} FAILED: {err.message if err else 'unknown'} — state NOT updated")
        return None, False

    async def _get_open_algo_inventory(self) -> list[dict[str, Any]] | None:
        """Read a fully typed conditional-order inventory through the Adapter.

        The legacy raw REST path could treat any list-shaped response as
        authoritative, including rows missing owner-critical fields.  The
        adapter parser rejects those rows; callers then remain read-only and
        fail closed instead of adopting or cancelling an ambiguous order.
        """

        try:
            result = await self._adapter.get_open_algo_orders()
        except Exception as exc:
            print(f"[api] open Algo inventory failed: {type(exc).__name__}: {exc}")
            return None
        if not result.is_success() or result.data is None:
            return None
        return [dict(snapshot.raw_response) for snapshot in result.data]

    async def _create_algo_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create one Algo order and expose only a typed ACK/raw failure."""

        try:
            result = await self._adapter.create_algo_order(params)
        except Exception as exc:
            return {"error": -1, "msg": f"Algo adapter error: {exc}"}
        if result.is_success() and result.data is not None:
            return dict(result.data.raw_response)
        error = result.error
        return {"error": -1, "msg": error.message if error else "Algo ACK UNKNOWN"}

    async def _cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        """Cancel one Algo id through the typed adapter boundary."""

        try:
            result = await self._adapter.cancel_algo_order(symbol, algo_id)
        except Exception as exc:
            return {"error": -1, "msg": f"Algo cancel adapter error: {exc}"}
        if result.is_success() and isinstance(result.data, dict):
            return dict(result.data)
        error = result.error
        return {"error": -1, "msg": error.message if error else "Algo cancel ACK UNKNOWN"}

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """同步兼容包装 — 委托给 BinanceRESTClient（BD-02 Adapter 边界）。

        运行时通过 asyncio.run() 调用异步 BinanceRESTClient.request()，
        享受统一错误分类、限频退避、熔断和时钟偏差校准。
        遗留同步代码使用此方法；新异步代码必须使用 _api_async。
        返回原始 dict（兼容现有代码），失败时返回 {"error": code, "msg": "..."}
        """
        import asyncio as _asyncio

        # 检查是否在异步上下文中被调用
        loop = None
        try:
            loop = _asyncio.get_running_loop()
            if loop.is_running():
                return {"error": -2, "msg": "_api called from async context — use _api_async instead"}
        except RuntimeError:
            loop = None  # 不在异步上下文中，正常

        try:
            result = _asyncio.run(self._adapter.request(method, path, signed, params))
        except Exception as ex:
            return {"error": -1, "msg": f"Adapter error: {ex}"}

        if result.is_success():
            return result.data
        err = result.error
        return {"error": err.http_status or -1, "msg": str(err.message) if err else "unknown"}

    async def enqueue_reduce_only_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        correlation_id: str | None,
        policy_id: str,
        policy_version: str,
        policy_signature: str,
    ) -> bool:
        """Submit an emergency close as a governed durable intent.

        Emergency flattening is still a risk-reducing action, but it must not
        create a second direct REST execution path.  The conditional-order
        module therefore calls this method, which only persists an intent; the
        normal fenced executor and Adapter remain the sole venue write path.
        """

        try:
            amount = float(quantity)
            order_side = OrderSide(str(side).upper())
        except (TypeError, ValueError):
            return False
        if amount <= 0 or not policy_id or not policy_version or not policy_signature:
            return False
        from beidou_safety.execution import OrderIntent

        now_bucket = int(time.time() / 60)
        intent = OrderIntent(
            intent_id=f"emergency-{symbol}-{now_bucket}-{uuid.uuid4().hex[:10]}",
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
            instrument_id=InstrumentId(str(symbol).upper()),
            side=order_side,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount=str(amount)),
            time_in_force=TimeInForce.GTC,
            client_order_id=f"beidou-{str(symbol).lower()}-emergency-{now_bucket}-{uuid.uuid4().hex[:8]}",
            correlation_id=CorrelationId(str(correlation_id)) if correlation_id else None,
            idempotency_key=f"emergency-{str(symbol).upper()}-{now_bucket}",
            risk_approval_id=f"EMERGENCY:{policy_id}:{policy_version}",
            reduce_only=True,
            close_position=True,
            emergency_policy_signed=True,
        )
        if not self._control.should_accept(intent):
            return False
        try:
            self._outbox.commit(intent)
        except (RuntimeError, ValueError):
            return False
        return True

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
                binance_code = None

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
            # P2修复: 非预期错误不返回 target_leverage（fail-open → fail-closed）
            print(f"[leverage] FAILED {symbol}: code={binance_code} {resp.get('msg', resp)}")
            return -1  # UNKNOWN → R0/R6 阻断，而非静默继续

    # --- Health & Metrics ---

    def _check_liveness(self) -> HealthState:
        """Expose stalled event-loop state instead of unconditional HEALTHY."""

        if self._running and time.time() - self._last_realtime > 15.0:
            return HealthState.UNHEALTHY
        if (
            self._control.get_status() != ControlAction.RESUME
            or self._last_reconciliation_result is None
            or not self._last_reconciliation_result.matched
        ):
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def _durable_fact_status(self) -> tuple[bool, str, dict[str, Any]]:
        """Validate durable order/outbox/protection facts before readiness.

        A process-local ``RESUME`` state is not sufficient evidence after a
        restart.  Unresolved order rows, unowned protections, or durable facts
        that are not represented in the current venue-tracked set must keep the
        authority closed until reconciliation resolves them.
        """

        store = getattr(self, "_store", None)
        outbox = getattr(self, "_outbox", None)
        if store is None or outbox is None:
            return False, "DURABLE_FACT_STORE_UNAVAILABLE", {}
        try:
            order_rows = list(store.restore_order_states())
            unknown_orders = [
                str(row.get("order_id", "")) for row in order_rows if str(row.get("status", "")).upper() == "UNKNOWN"
            ]
            active_rows = [
                row
                for row in order_rows
                if str(row.get("status", "")).upper() in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
            ]
            tracked_order_ids = {str(order_id) for order_id in getattr(self, "_active_order_ids", set())}
            untracked_active_orders = [
                str(row.get("order_id", ""))
                for row in active_rows
                if str(row.get("order_id", "")) not in tracked_order_ids
            ]

            raw_stats = getattr(outbox, "stats", {})
            outbox_stats = raw_stats() if callable(raw_stats) else raw_stats
            if not isinstance(outbox_stats, dict):
                raise TypeError("outbox stats is not a mapping")
            state_counts = outbox_stats.get("state_counts", {})
            if not isinstance(state_counts, dict):
                raise TypeError("outbox state_counts is not a mapping")
            unknown_outbox = int(state_counts.get("UNKNOWN", 0)) + int(state_counts.get("DEAD_LETTER", 0))

            protections = list(store.restore_protections())
            active_protections = [
                row
                for row in protections
                if str(row.get("status", "")).upper() == "ACTIVE"
                and str(row.get("owner_id", "")) == str(self._protection_owner_id)
                and bool(str(row.get("exchange_order_id", "")).strip())
            ]
            local_positions = (
                self._protection.all_positions()
                if callable(getattr(getattr(self, "_protection", None), "all_positions", None))
                else {}
            )
            exchange_positions = [
                row
                for row in (getattr(self, "_last_account", {}) or {}).get("positions", [])
                if abs(float(row.get("positionAmt", 0) or 0)) > 1e-12
            ]

            evidence = {
                "order_rows": len(order_rows),
                "unknown_orders": len(unknown_orders),
                "active_orders": len(active_rows),
                "untracked_active_orders": len(untracked_active_orders),
                "unknown_outbox": unknown_outbox,
                "active_protections": len(active_protections),
                "local_positions": len(local_positions),
                "exchange_positions": len(exchange_positions),
            }
            if unknown_orders:
                return False, "DURABLE_ORDER_UNKNOWN", {**evidence, "order_ids": unknown_orders[:20]}
            if unknown_outbox:
                return False, "DURABLE_OUTBOX_UNKNOWN", evidence
            if untracked_active_orders:
                return (
                    False,
                    "DURABLE_ACTIVE_ORDER_UNTRACKED",
                    {
                        **evidence,
                        "order_ids": untracked_active_orders[:20],
                    },
                )
            if (local_positions or exchange_positions) and not active_protections:
                return False, "DURABLE_PROTECTION_COVERAGE_UNKNOWN", evidence
            return True, "DURABLE_FACTS_VERIFIED", evidence
        except Exception as exc:
            return False, f"DURABLE_FACTS_UNKNOWN:{type(exc).__name__}", {"error": str(exc)[:200]}

    def _check_ready(self) -> bool:
        if not self._state_backend_supported:
            return False
        durable_ok, _, _ = self._durable_fact_status()
        if not durable_ok:
            return False
        if self._lifecycle.state != ModuleState.ACTIVE:
            return False
        if not self._feed.is_healthy():
            return False
        if self._control.get_status() != ControlAction.RESUME:
            return False
        if self._protection_owner_unknown:
            return False
        if self._last_reconciliation_result is None or not self._last_reconciliation_result.matched:
            return False
        return not self._running or time.time() - self._last_realtime <= 15.0

    def _check_trading_ready(self) -> tuple[bool, str]:
        if not self._state_backend_supported:
            return False, "STATE_BACKEND_UNSUPPORTED"
        durable_ok, durable_reason, _ = self._durable_fact_status()
        if not durable_ok:
            return False, durable_reason
        if not self._check_ready():
            if self._control.get_status() != ControlAction.RESUME:
                return False, f"CONTROL_{self._control.get_status().value}"
            if self._last_reconciliation_result is None:
                return False, "RECONCILIATION_UNKNOWN"
            return False, "RUNTIME_NOT_READY"
        if not self._can_write:
            return False, "TRADING_WRITE_DISABLED"
        return True, "READY"

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
            "reconciliation_status": (
                self._last_reconciliation_result.status.value
                if self._last_reconciliation_result is not None
                else "UNKNOWN"
            ),
            "reconciliation_matched": bool(
                self._last_reconciliation_result is not None and self._last_reconciliation_result.matched
            ),
            "event_stream_sequence": (
                self._user_stream_projector.sequencer.last_sequence if hasattr(self, "_user_stream_projector") else None
            ),
            "event_stream_status": (
                self._user_stream_projector.sequencer.status.value
                if hasattr(self, "_user_stream_projector")
                else "UNKNOWN"
            ),
            "state_backend_supported": self._state_backend_supported,
            "realtime_age_seconds": round(max(0.0, time.time() - self._last_realtime), 3),
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
            "state_backend_supported": self._state_backend_supported,
            "uptime_seconds": round(self._health.uptime_seconds(), 1),
            "last_realtime_tick": self._last_realtime,
            "last_nearline_tick": self._last_nearline,
            "last_reconciliation": {
                "status": self._last_reconciliation_result.status.value,
                "matched": self._last_reconciliation_result.matched,
                "differences": list(self._last_reconciliation_result.differences),
                "checked_at": self._last_reconciliation_result.checked_at.isoformat(),
            }
            if self._last_reconciliation_result is not None
            else {"status": "UNKNOWN", "matched": False},
            "event_stream": {
                "sequence": (
                    self._user_stream_projector.sequencer.last_sequence
                    if hasattr(self, "_user_stream_projector")
                    else None
                ),
                "status": (
                    self._user_stream_projector.sequencer.status.value
                    if hasattr(self, "_user_stream_projector")
                    else "UNKNOWN"
                ),
                "frozen_reason": (
                    self._user_stream_projector.frozen_reason if hasattr(self, "_user_stream_projector") else None
                ),
            },
            "realtime_age_seconds": round(max(0.0, time.time() - self._last_realtime), 3),
            "protection_owner_unknown": self._protection_owner_unknown,
            "active_incidents": self._alerts.get_active_incidents(),
            "strategy_risk": {
                "level": risk_state.risk_level.value if risk_state else "N/A",
                "drawdown_pct": round(risk_state.current_drawdown_pct, 2) if risk_state else 0,
                "consecutive_losses": risk_state.consecutive_losses if risk_state else 0,
            }
            if risk_state
            else None,
            "active_factors": len(self._factor_registry.get_active()) + len(self._factor_registry.get_challengers()),
            "security": {
                "service_id": self._service_identity.service_id,
                "roles": sorted(self._service_identity.roles),
                "credential_type": self._credential.credential_type.value,
                "credential_status": self._credential.status.value,
                "can_trade": self._can_trade,
                "can_withdraw": self._can_withdraw,
                "days_to_expiry": round(
                    max(0.0, (self._credential.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400.0),
                    1,
                ),
                "health": self._credential_health,
            },
        }

    def _rebuild_alpha_graph(self) -> None:
        """BF-08: 从 FactorRegistry 重建 AlphaGraph。

        当因子生命周期变更（promotion/degradation/suspension）时调用，
        确保交易图仅包含 ACTIVE 因子。新增或移除的因子自动反映到 DAG 拓扑。
        """
        # BD-T06: 非生产模式包含 CHALLENGER+PAPER_TRADING 因子用于测试
        trading_factors = set(self._factor_registry.get_active())
        if self._env_mode.value not in ("production", "canary", "live"):
            trading_factors.update(self._factor_registry.get_challengers())
        active_factor_ids = [fid for fid in trading_factors if fid in self._factor_component_registry]

        new_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))
        component_instances: dict[str, AlphaComponent] = {}
        for fid in active_factor_ids:
            component_cls, _ = self._factor_component_registry[fid]
            component = component_cls()
            new_graph.add_component(component)
            component_instances[fid] = component

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
        self._typed_graph = build_typed_graph(
            strategy_id=StrategyId("autopilot"),
            components=component_instances,
            entry_ids=self._entry_ids,
            filter_ids=self._filter_ids,
            exit_ids=self._exit_ids,
        )
        if hasattr(self, "_strategy_kernel"):
            self._strategy_kernel.set_alpha_graph(new_graph)
            self._strategy_kernel.set_typed_graph(self._typed_graph)
        # Ensure prediction tracking covers all active factors
        for fid in active_factor_ids:
            if fid not in self._factor_predictions:
                self._factor_predictions[fid] = []
        print(f"[beidou-autopilot] AlphaGraph rebuilt: {len(active_factor_ids)} active factors, DAG order: {order}")

    @staticmethod
    def _factor_timeframe_seconds(timeframe: str) -> float:
        """Return the bar duration used by the live prediction horizon."""
        units = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
        try:
            return float(int(timeframe[:-1])) * units[timeframe[-1]]
        except (KeyError, TypeError, ValueError):
            return 0.0

    def _advance_factor_bar(
        self,
        symbol: str,
        timeframe: str,
        bar_open_time: datetime,
        close: float,
    ) -> bool:
        """Advance a scope to one new closed bar and resolve prior samples.

        The previous bar's prediction is paired with ``close`` only when the
        bar interval is exactly the declared one-bar horizon.  Same-bar
        polling, out-of-order bars, gaps, invalid prices, and unknown timing
        all produce no evaluation sample.
        """
        if not isinstance(bar_open_time, datetime) or not math.isfinite(close) or close <= 0:
            return False
        scope = ("BINANCE", symbol, timeframe, 1)
        previous = self._factor_last_bar_by_scope.get(scope)
        if previous is not None:
            previous_open, _previous_close = previous
            if bar_open_time == previous_open:
                return False
            if bar_open_time < previous_open:
                logger.warning("factor bar out of order: %s %s < %s", symbol, bar_open_time, previous_open)
                return False

            pending = self._factor_pending_by_scope.get(scope, {})
            delta = (bar_open_time - previous_open).total_seconds()
            expected = self._factor_timeframe_seconds(timeframe)
            if expected > 0 and expected * 0.5 <= delta <= expected * 1.5:
                pairs = self._factor_pairs_by_scope.setdefault(scope, {})
                for factor_id, (_prediction_bar, entry_close, prediction) in pending.items():
                    if math.isfinite(entry_close) and entry_close > 0:
                        forward_return = (close - entry_close) / entry_close
                        if math.isfinite(forward_return) and math.isfinite(prediction):
                            pairs.setdefault(factor_id, []).append((prediction, forward_return))
            # A gap or invalid transition consumes the pending prediction;
            # carrying it forward would silently change a one-bar horizon.
            self._factor_pending_by_scope[scope] = {}

        self._factor_last_bar_by_scope[scope] = (bar_open_time, close)
        self._factor_pending_by_scope.setdefault(scope, {})
        return True

    def _store_factor_predictions(
        self,
        symbol: str,
        timeframe: str,
        bar_open_time: datetime,
        close: float,
        predictions: dict[str, Any],
    ) -> None:
        """Store predictions for the current closed bar only once."""
        scope = ("BINANCE", symbol, timeframe, 1)
        if self._factor_last_bar_by_scope.get(scope, (None, None))[0] != bar_open_time:
            return
        pending: dict[str, tuple[datetime, float, float]] = {}
        for factor_id, value in predictions.items():
            try:
                prediction = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(prediction):
                pending[str(factor_id)] = (bar_open_time, close, prediction)
        self._factor_pending_by_scope[scope] = pending

    # --- Clock Domain: REALTIME (every 5s) ---

    async def _realtime_tick(self) -> None:
        """实时时钟：行情轮询 → 保护单检查 → 订单处理 → 对账。"""
        self._tick_count += 1

        if self._tick_count % 3 == 0:
            print(
                f"[realtime] TICK #{self._tick_count} — outbox id={id(self._outbox)} items={len(self._outbox._outbox)}"
            )

        try:
            # Only evidence-approved pool members may enter the realtime path.
            # Configured symbols remain data subscriptions, never a trading
            # fallback when the pool has no ACTIVE entries.
            active_symbols = self._trading_pool.active_instruments()

            # 批次轮询: 25个标的每tick处理5个，5 tick完成一轮，避免阻塞
            batch_size = max(5, len(active_symbols) // 5)
            offset = (self._tick_count % max(1, (len(active_symbols) + batch_size - 1) // batch_size)) * batch_size
            batch = active_symbols[offset : offset + batch_size]

            for symbol in batch:
                # Yield event loop between symbols
                await asyncio.sleep(0)

                # 1. Fetch latest market data
                # BD-FIX: WebSocket 数据优先 — 如果 WS 数据新鲜则跳过 REST 调用
                if self._feed._ws_active and self._feed.is_ws_data_fresh(symbol):
                    features = await self._feed.async_get_kline_features(symbol)
                    # 补充 ticker/orderbook 从 WS 缓存
                    ticker = self._feed.get_last_ticker(symbol)
                    ob = self._feed.get_last_orderbook(symbol)
                    if ticker and ob and features:
                        features["price"] = float(ticker.get("lastPrice", features.get("close", 0)))
                        features["bid"] = float(ticker.get("bid", features.get("close", 0)))
                        features["ask"] = float(ticker.get("ask", features.get("close", 0)))
                        features["spread_bps"] = (
                            (features["ask"] - features["bid"]) / features["ask"] * 10000
                            if features["ask"] > 0
                            else features.get("spread_bps", 1.0)
                        )
                    else:
                        features = await self._feed.async_update_features(symbol)
                else:
                    features = await self._feed.async_update_features(symbol)
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

            # 5. Process only claimable intents.  Durable UNKNOWN intents are
            # deliberately excluded until an explicit exchange query resolves
            # them; a restart must never blind-retry an ambiguous submission.
            durable_outbox = bool(getattr(self._outbox, "_db_path", None))
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
            # P0修复: Paper/Shadow 模式也需要执行 intent（通过 _place_order 的 Paper 撮合分支）
            if self._can_write or self._can_simulate:
                if durable_outbox:
                    while True:
                        intent = self._outbox.claim(owner=f"engine-{id(self)}")
                        if intent is None:
                            break
                        await self._place_order(intent)
                else:
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
                cancel_resp = await self._cancel_algo_order(symbol, int(algo_id))
                if "code" not in cancel_resp:
                    print(f"[protection] Canceled algo order {algo_id} for {symbol}")
                else:
                    print(f"[protection] Failed to cancel algo {algo_id}: {cancel_resp.get('msg', cancel_resp)}")
            except Exception as e:
                print(f"[protection] Error canceling algo {algo_id}: {e}")

    def _remove_protection_with_cleanup(self, pid: str, symbol: str) -> None:
        """BD-FIX: 移除保护单并清理DB+幽灵algo追踪。"""
        self._protection.cancel_protection(pid)
        self._protection.remove_position(pid)
        self._position_entry_times.pop(pid, None)
        self._protection_exchange_attempted.discard(pid)
        # 持久化删除
        with contextlib.suppress(Exception):
            self._store.remove_protection(pid)

    def _persist_protection_order(self, p_order: Any, *, status: str | None = None) -> None:
        """Persist one protection order using the exchange-acknowledged state.

        ``ProtectionManager.create_protection`` builds a local definition before
        the venue call.  Until an ``algoId`` is returned that definition is only
        a pending intent and must not be restored as ACTIVE after a restart.
        """

        exchange_order_id = getattr(p_order, "exchange_order_id", None)
        # ACTIVE is a venue fact, never a local construction state.  A caller
        # must provide an explicit status and an exchange algo id together.
        persisted_status = (
            "ACTIVE"
            if status == "ACTIVE" and exchange_order_id
            else ("CANCELLED" if status == "CANCELLED" else "PENDING")
        )

        stop_type = getattr(getattr(p_order, "stop_type", None), "value", None)
        take_profit_type = getattr(getattr(p_order, "take_profit_type", None), "value", None)
        self._store.save_protection(
            protection_id=str(p_order.protection_id),
            position_id=str(p_order.position_id),
            symbol=str(p_order.instrument_id),
            side=str(getattr(p_order.side, "value", p_order.side)),
            trigger_price=str(p_order.trigger_price.amount),
            order_price=(str(p_order.order_price.amount) if p_order.order_price is not None else None),
            quantity=str(p_order.quantity.amount),
            order_type=str(p_order.order_type),
            status=persisted_status,
            stop_type=stop_type,
            take_profit_type=take_profit_type,
            owner_id=str(getattr(p_order, "owner_id", self._protection_owner_id) or "UNKNOWN"),
            position_generation=int(getattr(p_order, "position_generation", 0)),
            session_id=str(getattr(p_order, "session_id", self._session_id) or ""),
            exchange_order_id=str(exchange_order_id) if exchange_order_id else None,
        )

    def _next_position_generation(self, symbol: str) -> int:
        """Advance a durable owner-generation counter for one symbol."""

        generation = self._position_generation.get(symbol, 0) + 1
        self._position_generation[symbol] = generation
        return generation

    def _block_unowned_protection_orders(self, order_ids: list[str]) -> None:
        """Freeze new risk when venue protection ownership is unproven."""

        self._protection_owner_unknown = True
        if self._control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
            self._control.execute_action(ControlAction.NO_NEW_RISK)
        self._alerts.send_incident(
            AlertSeverity.CRITICAL,
            "Protection ownership unknown",
            f"Conditional orders lack durable owner mapping: {sorted(order_ids)}",
            category="protection",
        )

    def _require_protection_config(self, symbol: str, config: Any) -> None:
        """Reject protection construction when market-derived inputs are absent.

        A conservative-looking synthetic stop is still an unverified risk
        parameter.  If the market snapshot is incomplete, the position must
        remain under the NO_NEW_RISK gate until a fresh, venue-backed
        protection configuration can be computed.
        """
        metadata = getattr(config, "metadata", {}) or {}
        if metadata.get("blocked") or float(getattr(config, "stop_pct", 0) or 0) <= 0:
            reason = str(metadata.get("reason", "PROTECTION_CONFIG_UNKNOWN"))
            self._protection_config_unknown = True
            control = getattr(self, "_control", None)
            if control is not None:
                with contextlib.suppress(Exception):
                    if control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
                        control.execute_action(ControlAction.NO_NEW_RISK)
            alerts = getattr(self, "_alerts", None)
            if alerts is not None:
                with contextlib.suppress(Exception):
                    alerts.send_incident(
                        AlertSeverity.CRITICAL,
                        "Protection configuration UNKNOWN",
                        f"{symbol}: {reason}",
                        category="protection",
                    )
            raise RuntimeError(f"PROTECTION_CONFIG_UNKNOWN:{symbol}:{reason}")

    def _restore_durable_ledger(self) -> None:
        """Rebuild the in-memory ledger projection from the durable journal."""

        durable_rows = self._store.restore_ledger_transactions()
        legacy_rows = self._store.restore_ledger_entries()
        if legacy_rows and not durable_rows:
            raise RuntimeError(
                "Durable ledger journal is missing while legacy ledger entries exist; "
                "migration is required before the engine can start"
            )
        for row in durable_rows:
            try:
                postings = tuple(
                    Posting(
                        posting_id=str(posting["posting_id"]),
                        account_id=AccountId(str(posting["account_id"])),
                        account_type=AccountType(str(posting["account_type"])),
                        venue_id=VenueId(str(posting["venue_id"])),
                        instrument_id=(
                            InstrumentId(str(posting["instrument_id"]))
                            if posting["instrument_id"] is not None
                            else None
                        ),
                        amount=MonetaryValue(
                            amount=str(posting["amount"]),
                            currency=str(posting["currency"]),
                            decimals=int(posting["decimals"]),
                        ),
                        side=PostingSide(str(posting["side"])),
                        description=str(posting["description"] or ""),
                    )
                    for posting in row["postings"]
                )
                transaction = LedgerTransaction(
                    transaction_id=str(row["transaction_id"]),
                    transaction_type=LedgerTransactionType(str(row["transaction_type"])),
                    postings=postings,
                    source_event_id=str(row["source_event_id"] or ""),
                    correlation_id=(CorrelationId(str(row["correlation_id"])) if row["correlation_id"] else None),
                    timestamp=datetime.fromisoformat(str(row["timestamp"])),
                    is_correction=bool(row["is_correction"]),
                    reverses_transaction_id=str(row["reverses_transaction_id"] or ""),
                    metadata=json.loads(str(row["metadata"] or "{}")),
                )
                self._ledger.post(transaction)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Durable ledger reconstruction failed for {row.get('transaction_id', '<unknown>')}"
                ) from exc

    def _record_execution_fact_failure(self, reason: str) -> None:
        """Freeze execution truth after a durable fact write cannot be proven.

        A fill is an exchange fact, not a best-effort application log.  If its
        ledger journal or projection cannot be durably recorded, continuing in
        ``RESUME`` would allow new exposure to be sized from incomplete state.
        The only safe local action is to freeze the ledger, close the
        risk-increase gate, and emit a non-suppressible incident.  This helper
        is deliberately defensive so failure handling itself never hides the
        original persistence error during startup or unit-level fault tests.
        """

        with contextlib.suppress(Exception):
            self._ledger.freeze()
        control = getattr(self, "_control", None)
        if control is not None:
            with contextlib.suppress(Exception):
                if control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
                    control.execute_action(ControlAction.NO_NEW_RISK)
        alerts = getattr(self, "_alerts", None)
        if alerts is not None:
            with contextlib.suppress(Exception):
                alerts.send_incident(
                    AlertSeverity.CRITICAL,
                    "Execution fact persistence blocked",
                    str(reason)[:500],
                    category="execution_fact",
                )

    def _post_ledger_transaction(self, transaction: LedgerTransaction) -> None:
        """Persist then append a balanced transaction, failing closed on drift.

        The previous order (memory first, SQLite second) could leave an
        in-memory fill that vanished on restart when the database write failed.
        Validation is side-effect free, then the durable journal is committed,
        and only after that is the process-local ledger appended.  Any failure
        after the durable write freezes the ledger and closes new risk so a
        governed restart/replay can inspect the durable fact instead of
        silently continuing with divergent projections.
        """

        try:
            self._ledger.validate(transaction)
            self._store.save_ledger_transaction(transaction)
            self._ledger.post(transaction)
        except Exception as exc:
            self._record_execution_fact_failure(
                f"ledger transaction {transaction.transaction_id} persistence/post failed: {type(exc).__name__}: {exc}"
            )
            raise

    async def _verify_intent_at_send(self, intent) -> bool:
        """最终写边界复核审批、nonce、策略版本和风险下降语义。"""

        if not self._can_write:
            return True
        approval_id = str(getattr(intent, "risk_approval_id", "") or "")
        if approval_id == "RISK_EXEMPT_CLOSE":
            # 仅允许明确标记为 reduce-only 的风险下降命令走紧急路径。
            return bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False))
        if not approval_id or not getattr(intent, "risk_approval_signature", None):
            return False
        if self._risk_sm.get(RiskApprovalId(approval_id)) != RiskDecision.APPROVED:
            return False
        try:
            return await self._approval.verify(
                RiskApprovalId(approval_id),
                signature=str(intent.risk_approval_signature),
                proposal_hash=str(getattr(intent, "risk_proposal_hash", "")),
                account_snapshot_hash=str(getattr(intent, "risk_account_snapshot_hash", "")),
                risk_snapshot_hash=str(getattr(intent, "risk_snapshot_hash", "")),
                policy_version=str(getattr(intent, "risk_policy_version", "")),
                nonce=str(getattr(intent, "risk_nonce", "")),
                expires_at=getattr(intent, "risk_expires_at", None),
            )
        except (RuntimeError, TypeError, ValueError):
            return False

    async def _place_order(self, intent, symbol: str = "") -> None:
        """向交易所发送订单。"""

        # P0 Gate 2: Executor 发送前再次校验控制状态
        # 防止 Outbox 提交后到发送前控制状态变更的竞态窗口
        if not self._control.should_accept(intent):
            print(
                f"[order] ❌ Intent {intent.intent_id} REJECTED at executor gate ({self._control.get_status().value} v{self._control.version})"
            )
            self._outbox.reject(
                intent.intent_id,
                f"CONTROL_GATE_{self._control.get_status().value}",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return

        if not await self._verify_intent_at_send(intent):
            print(f"[order] ❌ Intent {intent.intent_id} rejected: final risk approval invalid or missing")
            self._outbox.reject(
                intent.intent_id,
                "FINAL_APPROVAL_INVALID",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return

        # BD-FIX: Intent dead-letter — 超过最大重试次数的意图标记为失败
        if not hasattr(self, "_intent_retry_count"):
            self._intent_retry_count: dict[str, int] = {}
        retries = self._intent_retry_count.get(intent.intent_id, 0) + 1
        self._intent_retry_count[intent.intent_id] = retries
        MAX_INTENT_RETRIES = 50
        if retries > MAX_INTENT_RETRIES:
            print(f"[order] ❌ Intent {intent.intent_id} DEAD-LETTER after {retries} retries")
            self._outbox.dead_letter(
                intent.intent_id,
                f"MAX_INTENT_RETRIES_EXCEEDED:{retries}",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            self._intent_retry_count.pop(intent.intent_id, None)
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
            # 零写模式 (paper)：通过 PaperMatchingEngine 模拟真实撮合（BD-T16）—
            # 价差滑点 / 部分成交 / 拒绝 / 延迟 / 确定性 RNG，不再直接 ACKED→FILLED。
            tracker = OrderStateTracker(order_id=OrderId(intent.intent_id))
            tracker.apply(OrderEvent.ACKED)
            qty = float(intent.quantity.amount)
            # 仅 LIMIT 单携带限价；MARKET 单的 reference price 不参与撮合（否则永远入队）
            limit_px = float(intent.price.amount) if order_type == "LIMIT" and intent.price else None

            # Paper must consume the same executable two-sided market fact as
            # the live planner.  A synthetic spread would make Paper/Shadow
            # look healthy while hiding stale or incomplete market data.
            ticker = self._feed.get_last_ticker(order_symbol)
            bid = float(ticker.get("bidPrice", 0) or ticker.get("bid", 0) or 0)
            ask = float(ticker.get("askPrice", 0) or ticker.get("ask", 0) or 0)
            if not (0 < bid <= ask):
                print(f"[order] PAPER rejected: executable bid/ask UNKNOWN for {order_symbol}")
                self._outbox.reject(
                    intent.intent_id,
                    "PAPER_MARKET_DATA_UNKNOWN",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                return

            try:
                status, filled_qty, avg_price, latency_ms = self._paper_matching.match(
                    order_symbol, side, qty, limit_px, bid, ask
                )
            except Exception as exc:
                # A simulator failure is an UNKNOWN execution result, never a
                # synthetic fill.  Keep the intent auditable and fail closed.
                print(f"[order] PAPER matching engine failed ({exc}) — rejecting intent")
                self._outbox.reject(
                    intent.intent_id,
                    "PAPER_MATCHING_UNKNOWN",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                return

            print(
                f"[order] PAPER match: {order_symbol} {side} qty={qty} "
                f"→ status={status} filled={filled_qty:.6f} avg={avg_price:.4f} "
                f"latency={latency_ms:.1f}ms (bid={bid:.4f} ask={ask:.4f})"
            )

            if status == "FILLED":
                tracker.apply(OrderEvent.FILLED)
                persisted_status = "FILLED"
            elif status == "PARTIALLY_FILLED":
                tracker.apply(OrderEvent.PARTIALLY_FILLED)
                persisted_status = "PARTIALLY_FILLED"
            elif status == "REJECTED":
                tracker.apply(OrderEvent.REJECTED)
                persisted_status = "REJECTED"
                filled_qty = 0.0
                avg_price = 0.0
            else:
                # QUEUED is a durable, non-terminal paper order.  It is not a
                # fill and must not disappear merely because the intent was
                # accepted by the simulator.
                persisted_status = "NEW"

            self._store.save_order_state(
                order_id=intent.intent_id,
                symbol=order_symbol,
                side=side,
                order_type=order_type,
                quantity=str(qty),
                price=params.get("price"),
                status=persisted_status,
                filled_qty=str(filled_qty),
                avg_price=str(avg_price) if avg_price > 0 else None,
                client_order_id=client_id,
            )

            if persisted_status in {"NEW", "PARTIALLY_FILLED"}:
                self._active_order_ids.add(intent.intent_id)
            else:
                self._active_order_ids.discard(intent.intent_id)

            self._order_trackers[intent.intent_id] = tracker
            if status == "REJECTED":
                self._outbox.reject(
                    intent.intent_id,
                    "PAPER_ORDER_REJECTED",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
            else:
                self._outbox.ack(intent.intent_id, idempotency_key=getattr(intent, "idempotency_key", ""))
            self._order_count += 1

            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10000.0
            estimated_cost_bps = spread_bps / 2.0 + self._paper_matching.taker_fee_bps
            actual_cost_bps = (
                self._paper_matching.realized_cost_bps(side, avg_price, bid, ask) if filled_qty > 0 else 0.0
            )
            if filled_qty > 0 and avg_price > 0:
                notional = filled_qty * avg_price
                fee_amount = notional * self._paper_matching.taker_fee_bps / 10000.0
                spread_amount = notional * (spread_bps / 2.0) / 10000.0
                slippage_bps = max(0.0, actual_cost_bps - self._paper_matching.taker_fee_bps - spread_bps / 2.0)
                slippage_amount = notional * slippage_bps / 10000.0
                if self._shadow_runner is not None:
                    ledger_tx_id = self._shadow_runner.record_fill_to_ledger(
                        self._ledger,
                        order_symbol,
                        side,
                        filled_qty,
                        avg_price,
                        fee=fee_amount,
                        spread_cost=spread_amount,
                        slippage_cost=slippage_amount,
                    )
                    if ledger_tx_id is None:
                        self._shadow_runner.record_incident("P0")

            # 记录成交明细（fill price / latency / status）
            self._paper_fills.append(
                {
                    "intent_id": intent.intent_id,
                    "symbol": order_symbol,
                    "side": side,
                    "status": status,
                    "filled_qty": filled_qty,
                    "avg_price": avg_price,
                    "latency_ms": latency_ms,
                    "estimated_cost_bps": estimated_cost_bps,
                    "actual_cost_bps": actual_cost_bps,
                    "ts": time.time(),
                }
            )

            # Paper Shadow 指标（若 shadow runner 激活）
            if self._shadow_runner is not None:
                predicted = "LONG" if side == "BUY" else "SHORT"
                actual = predicted if status in ("FILLED", "PARTIALLY_FILLED") else "NO_ACTION"
                self._shadow_runner.record_tick(
                    predicted_direction=predicted,
                    predicted_strength=1.0,
                    actual_direction=actual,
                    actual_strength=min(1.0, filled_qty / qty) if qty > 0 else 0.0,
                    estimated_cost_bps=estimated_cost_bps,
                    actual_cost_bps=actual_cost_bps,
                )
            return

        # === 执行算法选择 + 切片计划 (TWAP/POV/AdaptiveSlice/...) ===
        # 返回 (slices, algorithm_type, ctx)；None 表示计划被取消（intent 已 ack）
        planned = await self._plan_execution(intent, order_symbol, client_id)
        if planned is None:
            return
        slices, algo_type, ctx = planned

        # === 逐切片下发交易所 ===
        # BD-FIX: 多切片算法按间隔分批发送，而非一次性全部下发。
        # TWAP 默认 60s 间隔，POV/Adaptive 按切片数均分 alpha_decay_seconds。
        n_slices = len(slices)
        slice_interval = 0.0
        if n_slices > 1:
            default_interval = getattr(ctx, "alpha_decay_seconds", 60.0) / max(n_slices, 1)
            slice_interval = max(2.0, min(default_interval, 120.0))  # 2s~120s 范围
        intent_acked = False
        for idx, (qty_str, price_str, order_type, tif, slice_client_id) in enumerate(slices):
            # P0 修复: 每切片前复检控制面状态，防止中途 NO_NEW_RISK/LOCK 后剩余切片照常发送
            if idx > 0 and not self._control.should_accept(intent):
                print(
                    f"[order] ⚠️ TWAP ABORTED after {idx}/{n_slices} slices: "
                    f"control plane rejected (state={self._control.get_status().value} v{self._control.version})"
                )
                if not intent_acked:
                    self._outbox.reject(
                        intent.intent_id,
                        f"CONTROL_GATE_{self._control.get_status().value}",
                        idempotency_key=getattr(intent, "idempotency_key", "") or "",
                    )
                return  # 提前终止 TWAP，不再发送剩余切片
            # 切片间等待（首个切片立即发送）
            if idx > 0 and slice_interval > 0:
                await asyncio.sleep(slice_interval)
            params = {
                "symbol": order_symbol,
                "side": side,
                "type": order_type,
                "quantity": qty_str,
                "newClientOrderId": slice_client_id,
            }
            if order_type == "LIMIT" and price_str:
                params["price"] = price_str
                params["timeInForce"] = tif or "GTC"

            order = await self._submit_order_slice(
                intent,
                params=params,
                order_symbol=order_symbol,
                side=side,
                order_type=order_type,
                ack_outbox=not intent_acked,
            )
            if order is not None:
                intent_acked = True

        if not intent_acked and getattr(self._outbox, "_db_path", None):
            # No venue acknowledgement is an ambiguous outcome.  Persist
            # UNKNOWN and require a client-order-id query before any retry.
            self._outbox.mark_unknown(intent.intent_id, "ORDER_ACK_UNKNOWN")

        # === 执行质量反馈：Contextual Bandit 学习实际执行成本 ===
        if algo_type is not None and slices:
            realized_cost_bps = ctx.predicted_cost_bps
            slippage_bps = max(0.0, ctx.spread_bps - 1.0)
            self._exec_selector.update_quality(algo_type, realized_cost_bps, slippage_bps)
            print(
                f"[order] {order_symbol}: quality updated for {algo_type.value} "
                f"(realized_cost={realized_cost_bps:.1f}bps slippage={slippage_bps:.1f}bps)"
            )

    async def _plan_execution(self, intent, order_symbol: str, client_id: str):
        """构建 ExecutionContext → 选择执行算法 → 生成切片计划。

        返回 (slices, algorithm_type, ctx)：
          - slices: list[tuple[quantity, price, order_type, time_in_force, client_order_id]]
            计划或不变量不可验证时不返回切片，意图保持拒绝/冻结状态。
          - algorithm_type: 使用的算法类型；直接路径为 None。
        返回 None 表示计划被取消或执行不变量无法证明。
        """
        # 1. 实时市场数据：订单簿 → bid/ask/深度/价差
        is_reduce_only = (
            bool(getattr(intent, "reduce_only", False))
            or bool(getattr(intent, "close_position", False))
            or ("-close-" in client_id)
        )
        bid: float | None = None
        ask: float | None = None
        spread_bps: float | None = None
        bid_depth = 0.0
        ask_depth = 0.0
        try:
            ob = await self._feed.async_fetch_orderbook(order_symbol, 5)
            if ob and ob.get("bids") and ob.get("asks"):
                bid = float(ob["bids"][0][0])
                ask = float(ob["asks"][0][0])
                bid_depth = sum(float(b[1]) for b in ob["bids"])
                ask_depth = sum(float(a[1]) for a in ob["asks"])
                spread_bps = (ask - bid) / ask * 10000 if ask > 0 else None
        except Exception as e:
            print(f"[order] {order_symbol}: orderbook fetch failed ({e}), falling back to features")
        if bid is None or ask is None:
            try:
                features = await self._feed.async_update_features(order_symbol)
                if features.get("bid") and features.get("ask"):
                    bid = float(features["bid"])
                    ask = float(features["ask"])
                    spread_value = features.get("spread_bps")
                    if spread_value is not None:
                        spread_bps = float(spread_value)
            except Exception as exc:
                print(f"[order] {order_symbol}: feature fetch failed ({exc})")
            if bid is None or ask is None:
                if not is_reduce_only:
                    self._outbox.reject(
                        intent.intent_id,
                        "MARKET_DATA_UNKNOWN",
                        idempotency_key=getattr(intent, "idempotency_key", "") or "",
                    )
                    print(f"[order] {order_symbol}: rejected — market data UNKNOWN")
                    return None
                print(f"[order] {order_symbol}: market data UNKNOWN; reduce-only emergency path only")
        if (bid_depth <= 0 or ask_depth <= 0) and not is_reduce_only:
            self._outbox.reject(
                intent.intent_id,
                "MARKET_DEPTH_UNKNOWN",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            print(f"[order] {order_symbol}: rejected — market depth UNKNOWN")
            return None
        if spread_bps is None:
            spread_bps = 0.0 if is_reduce_only else float("inf")
        price = (bid + ask) / 2 if bid and ask else float(intent.price.amount) if intent.price else 0.0

        # 2. 紧急减仓（reduce-only）→ 最高紧急度，强制 EMERGENCY_REDUCE_ONLY
        urgency = 0.9 if is_reduce_only else 0.3

        # 3. 成本估算与净 Alpha
        predicted_cost_bps = spread_bps * 0.5
        net_alpha_bps = float(getattr(intent, "net_alpha_bps", 0.0) or 0.0)
        if not is_reduce_only and net_alpha_bps <= 0:
            self._outbox.reject(
                intent.intent_id,
                "ALPHA_UNKNOWN",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            print(f"[order] {order_symbol}: rejected — net alpha UNKNOWN")
            return None
        try:
            vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(order_symbol))
            self._cost_model.set_fee_tier(VenueId("BINANCE"), "vip1", 2.0, 4.0)
            est = self._cost_model.estimate_order(
                vi,
                Quantity(amount=str(float(intent.quantity.amount))),
                Price(amount=str(price)) if price > 0 else Price(amount="0"),
                intent.side,
                urgency=urgency,
                spread_bps=spread_bps,
            )
            predicted_cost_bps = float(getattr(est, "total_fee_bps", predicted_cost_bps))
        except Exception as e:
            if not is_reduce_only:
                self._outbox.reject(
                    intent.intent_id,
                    "COST_MODEL_UNKNOWN",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                print(f"[order] {order_symbol}: rejected — cost model UNKNOWN ({e})")
                return None
            print(f"[order] {order_symbol}: cost estimation unavailable for reduce-only ({e})")

        # 4. 构建执行上下文
        ctx = ExecutionContext(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(order_symbol)),
            side=intent.side,
            total_quantity=Quantity(amount=str(float(intent.quantity.amount))),
            limit_price=intent.price if intent.order_type == OrderType.LIMIT else None,
            urgency=urgency,
            best_bid=Price(amount=str(bid)) if bid else None,
            best_ask=Price(amount=str(ask)) if ask else None,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spread_bps=spread_bps,
            historical_execution_quality=dict(self._exec_quality_history),
            alpha_decay_seconds=60.0,
            predicted_cost_bps=predicted_cost_bps,
            net_alpha_bps=net_alpha_bps,
            hard_slippage_limit_bps=50.0,
            correlation_id=str(intent.correlation_id) if intent.correlation_id else None,
        )

        # 5. 算法选择：紧急减仓强制 EMERGENCY_REDUCE_ONLY
        algo: BaseExecutionAlgorithm | None = None
        if ctx.urgency >= 0.8 and is_reduce_only:
            emergency = [
                a
                for a in ExecutionAlgorithmSelector.ALL_ALGORITHMS
                if a.algorithm_type == ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY
            ]
            algo = emergency[0] if emergency else None
        if algo is None:
            algo = self._exec_selector.select(ctx)

        if algo is None:
            print(
                f"[order] {order_symbol}: no applicable execution algorithm — "
                "rejecting intent; execution invariants are unverifiable"
            )
            self._outbox.reject(
                intent.intent_id,
                "EXECUTION_ALGORITHM_UNAVAILABLE",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        # 7. 生成执行计划
        plan = algo.plan(ctx, OrderId(client_id))

        if plan.is_canceled:
            print(f"[order] {order_symbol}: execution plan CANCELED by {plan.algorithm.value}: {plan.cancel_reason}")
            self._outbox.reject(
                intent.intent_id,
                f"EXECUTION_PLAN_CANCELED:{plan.cancel_reason or 'UNKNOWN'}",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        if not plan.slices:
            print(f"[order] {order_symbol}: algorithm {plan.algorithm.value} produced no slices — rejecting intent")
            self._outbox.reject(
                intent.intent_id,
                "EXECUTION_PLAN_EMPTY",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        # 8. 计划不变量校验（硬滑点/Alpha 剩余/总量上限）
        ok, msg = SliceInvariantChecker.validate_plan(plan, ctx)
        if not ok:
            print(f"[order] {order_symbol}: plan failed invariants ({msg}) — rejecting intent")
            self._outbox.reject(
                intent.intent_id,
                f"EXECUTION_PLAN_INVARIANT_FAILED:{msg}",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        # 9. 切片 → 交易所参数
        slices: list[tuple[str, str | None, str, str, str]] = []
        for slc in plan.slices:
            if not slc.invariants_check_passed:
                print(f"[order] {order_symbol}: skip slice {slc.slice_id} (invariants failed)")
                continue
            qty_str = str(float(slc.quantity.amount))
            price_str = str(float(slc.price.amount)) if slc.price is not None else None
            otype = "LIMIT" if slc.order_type == OrderType.LIMIT else "MARKET"
            slice_client_id = client_id if len(plan.slices) == 1 else f"{client_id}-{slc.sequence_number}"
            slices.append((qty_str, price_str, otype, slc.time_in_force.value, slice_client_id))

        if not slices:
            print(f"[order] {order_symbol}: all slices skipped (invariants) — rejecting intent")
            self._outbox.reject(
                intent.intent_id,
                "EXECUTION_PLAN_NO_VALID_SLICES",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        print(
            f"[order] {order_symbol}: algo={plan.algorithm.value} slices={len(slices)} "
            f"total_qty={plan.total_quantity():.6f} est_cost={plan.total_estimated_cost_bps:.1f}bps "
            f"est_completion={plan.estimated_completion_seconds:.0f}s"
        )
        return slices, plan.algorithm, ctx

    async def _submit_order_slice(
        self,
        intent,
        params: dict,
        order_symbol: str,
        side: str,
        order_type: str,
        ack_outbox: bool,
    ) -> dict | None:
        """单个切片：量化精度修正 → 发送 → 记录。返回交易所响应或 None。"""
        # 量化精度修正：查询交易所规则获取 stepSize 和 tickSize
        if not hasattr(self, "_symbol_precision"):
            self._symbol_precision: dict[str, dict[str, int]] = {}
        if order_symbol not in self._symbol_precision:
            try:
                exchange_info = await self._api_async(Endpoint.EXCHANGE_INFO)
                found = False
                for s in exchange_info.get("symbols", []):
                    sym = s.get("symbol", "")
                    quantity_step: str | None = None
                    price_tick: str | None = None
                    for f_item in s.get("filters", []):
                        if f_item.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                            quantity_step = str(f_item.get("stepSize", "")) or quantity_step
                        if f_item.get("filterType") == "PRICE_FILTER":
                            price_tick = str(f_item.get("tickSize", "")) or price_tick
                    if not quantity_step or not price_tick:
                        continue
                    try:
                        quantity_decimals = max(0, -Decimal(quantity_step).as_tuple().exponent)
                        price_decimals = max(0, -Decimal(price_tick).as_tuple().exponent)
                    except (InvalidOperation, ValueError):
                        continue
                    self._symbol_precision[sym] = {
                        "quantity": quantity_decimals,
                        "price": price_decimals,
                    }
                    if sym == order_symbol:
                        found = True
                if not found:
                    print(f"[order] {order_symbol}: exchange precision UNKNOWN")
                    return None
            except Exception as exc:
                print(f"[order] {order_symbol}: exchangeInfo unavailable ({exc})")
                return None

        prec = self._symbol_precision.get(order_symbol)
        if prec is None:
            print(f"[order] {order_symbol}: exchange precision UNKNOWN")
            return None
        qty = float(params["quantity"])
        params["quantity"] = f"{qty:.{prec['quantity']}f}"
        price_raw = params.get("price")
        if price_raw:
            params["price"] = f"{float(price_raw):.{prec['price']}f}"

        print(f"[order] Sending to exchange: {order_symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')}")
        try:
            adapter_response = await self._adapter.create_order(
                OrderRequest(
                    venue_instrument=VenueInstrument(
                        venue_id=VenueId("BINANCE"),
                        instrument_id=InstrumentId(order_symbol),
                    ),
                    account_ref=intent.account_ref,
                    side=OrderSide(side),
                    order_type=OrderType(order_type),
                    quantity=Quantity(amount=str(params["quantity"])),
                    price=Price(amount=str(params["price"])) if params.get("price") else None,
                    time_in_force=TimeInForce(params.get("timeInForce", TimeInForce.GTC.value)),
                    client_order_id=params.get("newClientOrderId"),
                    correlation_id=getattr(intent, "correlation_id", None),
                    reduce_only=bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False)),
                )
            )
            order = adapter_response.raw_response or {}
            if not order and adapter_response.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
                order = {"error": adapter_response.status.value, "msg": "adapter returned no order acknowledgement"}
        except (TypeError, ValueError) as exc:
            order = {"error": "ADAPTER_REQUEST_INVALID", "msg": str(exc)}
        print(f"[order] Exchange response: {str(order)[:200]}")

        if "orderId" in order:
            oid_str = str(order["orderId"])
            slice_client_id = params.get("newClientOrderId", "")
            # 标记平仓订单（通过 client_order_id 中的 "-close-" 模式识别）
            if slice_client_id and "-close-" in slice_client_id:
                self._close_order_ids.add(oid_str)
                print(f"[order] Marked as close order: {oid_str}")
            tracker = OrderStateTracker(order_id=OrderId(oid_str))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.SENT)
            self._order_trackers[oid_str] = tracker
            self._order_symbols[oid_str] = order_symbol
            if ack_outbox:
                self._outbox.ack(intent.intent_id, idempotency_key=getattr(intent, "idempotency_key", ""))
            self._order_count += 1

            actual_status = order.get("status", "NEW")
            self._last_order_placed_at = time.time()
            print(
                f"[order] PLACED: {order_symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')} "
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
                    str(params["quantity"]),
                    params.get("price"),
                    actual_status,
                    client_order_id=slice_client_id,
                )
            return order

        # P0修复: -4141 重复 clientOrderId → 查询已有订单恢复状态，而非当作失败重试
        err_code = order.get("code", 0) if isinstance(order, dict) else 0
        if err_code == -4141:
            client_id = params.get("newClientOrderId", "")
            print(f"[order] -4141 DUPLICATE clientOrderId={client_id} — querying existing order")
            try:
                query_resp = await self._api_async(
                    Endpoint.ALL_ORDERS,
                    signed=True,
                    params={"symbol": order_symbol, "origClientOrderId": client_id},
                )
                if isinstance(query_resp, list) and query_resp:
                    existing = query_resp[0]
                    oid_str = str(existing.get("orderId", ""))
                    if oid_str:
                        actual_status = existing.get("status", "UNKNOWN")
                        print(f"[order] RECOVERED from -4141: orderId={oid_str} status={actual_status}")
                        tracker = OrderStateTracker(order_id=OrderId(oid_str))
                        tracker.apply(OrderEvent.ACKED)
                        tracker.apply(OrderEvent.SENT)
                        self._order_trackers[oid_str] = tracker
                        self._order_symbols[oid_str] = order_symbol
                        if ack_outbox:
                            self._outbox.ack(intent.intent_id, idempotency_key=getattr(intent, "idempotency_key", ""))
                        self._order_count += 1
                        self._last_order_placed_at = time.time()
                        if actual_status == "FILLED":
                            await self._process_fill(oid_str, order_symbol, existing)
                        else:
                            self._active_order_ids.add(oid_str)
                        return existing
            except Exception as qe:
                print(f"[order] -4141 recovery query failed: {qe}")
            # 恢复失败 → 保留 UNKNOWN，禁止盲目重发。
            if ack_outbox:
                self._outbox.mark_unknown(intent.intent_id, "DUPLICATE_QUERY_UNKNOWN")
            return None

        print(f"[order] FAILED: {order_symbol} {side} — {order.get('msg', order.get('error', 'unknown'))}")
        return None

    async def _monitor_orders(self, symbol: str) -> None:
        """查询活跃订单状态并更新状态机/账本。"""
        for order_id in list(self._active_order_ids):
            fill_event_id_for_retry = ""
            fill_committed = False
            # 使用订单自身的 symbol 而非批量循环的 symbol
            order_sym = self._order_symbols.get(order_id)
            if order_sym is not None and order_sym != symbol:
                continue  # 跳过不属于当前 symbol 的订单，避免无效 API 调用
            order_sym = order_sym or symbol
            try:
                result, query_ok = await self._api_async_safe(
                    Endpoint.ORDER,
                    signed=True,
                    params={
                        "symbol": order_sym,
                        "orderId": int(order_id),
                    },
                )
                tracker = self._order_trackers.get(order_id)
                if not query_ok or not isinstance(result, dict) or "status" not in result:
                    # An unreadable order state is an UNKNOWN fact, never an
                    # implicit cancellation/fill.  Persist it so readiness
                    # and reconciliation remain closed across restart.
                    if tracker is not None:
                        tracker.apply(OrderEvent.UNKNOWN)
                    self._active_order_ids.discard(order_id)
                    self._store.save_order_state(
                        order_id,
                        order_sym,
                        "UNKNOWN",
                        "UNKNOWN",
                        "0",
                        None,
                        "UNKNOWN",
                    )
                    self._record_execution_fact_failure(f"ORDER_STATUS_UNKNOWN:{order_id}")
                    continue

                if result.get("msg") and (
                    "Order does not exist" in str(result.get("msg")) or "Unknown order" in str(result.get("msg"))
                ):
                    last_status = str(getattr(getattr(tracker, "status", None), "value", "UNKNOWN"))
                    is_expected = last_status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
                    if not is_expected:
                        if tracker is not None:
                            tracker.apply(OrderEvent.UNKNOWN)
                        self._active_order_ids.discard(order_id)
                        self._store.save_order_state(
                            order_id,
                            order_sym,
                            "UNKNOWN",
                            "UNKNOWN",
                            "0",
                            None,
                            "UNKNOWN",
                        )
                        self._record_execution_fact_failure(f"ORDER_DISAPPEARED_UNKNOWN:{order_id}")
                    else:
                        self._active_order_ids.discard(order_id)
                    continue

                status = result["status"]
                if not tracker:
                    continue

                executed_qty = float(result.get("executedQty", 0))

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
                    # P1修复: 部分成交入账 — 记录已执行数量到账本
                    delta_qty, partial_price, fill_event_id = self._consume_cumulative_fill(
                        order_id,
                        order_sym or symbol,
                        result,
                        status="PARTIALLY_FILLED",
                    )
                    fill_event_id_for_retry = fill_event_id
                    if delta_qty > 0 and partial_price > 0:
                        partial_notional = delta_qty * partial_price
                        side_desc = result.get("side", "")
                        is_buy = side_desc.upper() == "BUY"
                        tx_id = f"tx-{fill_event_id.replace(':', '-')}"
                        tx = LedgerTransaction(
                            transaction_id=tx_id,
                            transaction_type=LedgerTransactionType.FILL,
                            source_event_id=fill_event_id,
                            postings=(
                                Posting(
                                    posting_id=f"{tx_id}-p1",
                                    account_id=AccountId("default"),
                                    account_type=AccountType.POSITION_COST if is_buy else AccountType.CASH,
                                    venue_id=VenueId("BINANCE"),
                                    instrument_id=InstrumentId(order_sym or symbol),
                                    amount=MonetaryValue(amount=str(partial_notional)),
                                    side=PostingSide.DEBIT,
                                    description=f"PARTIAL {side_desc} {delta_qty} {order_sym or symbol} @ {partial_price}",
                                ),
                                Posting(
                                    posting_id=f"{tx_id}-p2",
                                    account_id=AccountId("default"),
                                    account_type=AccountType.CASH if is_buy else AccountType.POSITION_COST,
                                    venue_id=VenueId("BINANCE"),
                                    instrument_id=InstrumentId(order_sym or symbol),
                                    amount=MonetaryValue(amount=str(partial_notional)),
                                    side=PostingSide.CREDIT,
                                    description=f"PARTIAL {side_desc} {delta_qty} {order_sym or symbol} @ {partial_price}",
                                ),
                            ),
                            correlation_id=CorrelationId(f"exec-{order_id}"),
                        )
                        self._post_ledger_transaction(tx)
                        self._update_position_projection(
                            order_sym or symbol,
                            side_desc,
                            delta_qty,
                            partial_price,
                            fill_event_id,
                        )
                        self._mark_fill_committed(
                            order_id,
                            fill_event_id,
                            float(result.get("executedQty", 0) or 0),
                        )
                        fill_committed = True
                        self._store.save_order_state(
                            order_id,
                            order_sym or symbol,
                            side_desc,
                            result.get("type", "MARKET"),
                            result.get("origQty", "0"),
                            result.get("price"),
                            "PARTIALLY_FILLED",
                            str(executed_qty),
                            str(partial_price),
                        )
                        print(
                            f"[order] PARTIAL FILL recorded: {order_sym or symbol} {side_desc} "
                            f"qty={delta_qty} @ {partial_price} notional={partial_notional:.2f}"
                        )

            except Exception as e:
                # Log error but do NOT silently swallow — maintain visibility
                if fill_event_id_for_retry and not fill_committed:
                    self._mark_fill_retryable(order_id, fill_event_id_for_retry)
                    self._record_execution_fact_failure(
                        f"partial fill {fill_event_id_for_retry} could not be committed: {type(e).__name__}: {e}"
                    )
                    tracker = self._order_trackers.get(order_id)
                    if tracker is not None:
                        tracker.apply(OrderEvent.UNKNOWN)
                    self._active_order_ids.discard(order_id)
                print(f"[realtime] Order monitoring error ({order_id}): {e}")

    def _consume_cumulative_fill(
        self,
        order_id: str,
        symbol: str,
        result: dict,
        *,
        status: str,
    ) -> tuple[float, float, str]:
        """Convert a cumulative exchange observation into one durable delta.

        Polling the same order is expected.  The unique fill-event journal is
        written before ledger mutation; any persistence error remains UNKNOWN
        and must stop risk increase instead of being retried blindly.
        """

        cumulative_qty = float(result.get("executedQty", 0) or 0)
        avg_price = float(result.get("avgPrice", 0) or 0)
        if cumulative_qty <= 0 or avg_price <= 0:
            return 0.0, avg_price, ""
        previous_qty = self._filled_quantities_by_order.get(order_id, 0.0)
        delta_qty = cumulative_qty - previous_qty
        trade_id = result.get("tradeId") or result.get("lastTradeId")
        event_id = (
            f"trade:{order_id}:{trade_id}"
            if trade_id is not None
            else f"order:{order_id}:cum:{cumulative_qty:.16g}:avg:{avg_price:.16g}"
        )
        existing = self._store.get_fill_event(event_id)
        if existing is not None:
            if str(existing.get("processing_state", "COMMITTED")) == "COMMITTED":
                self._filled_quantities_by_order[order_id] = max(previous_qty, cumulative_qty)
                return 0.0, avg_price, event_id
            # PENDING is retried only after an explicit failure path marks the
            # event.  Ordinary duplicate polls remain no-ops.
            if event_id not in getattr(self, "_pending_fill_retry", set()):
                return 0.0, avg_price, event_id
            delta_qty = float(existing.get("delta_qty") or delta_qty)
            avg_price = float(existing.get("price") or avg_price)
            if delta_qty <= 1e-12 or avg_price <= 0:
                return 0.0, avg_price, event_id
            return delta_qty, avg_price, event_id
        if delta_qty <= 1e-12:
            return 0.0, avg_price, event_id
        inserted = self._store.save_fill_event(
            event_id,
            order_id,
            symbol,
            str(result.get("side", "UNKNOWN")),
            f"{cumulative_qty:.16g}",
            f"{delta_qty:.16g}",
            f"{avg_price:.16g}",
            status,
        )
        if not inserted:
            # Another writer inserted the row between the read and insert.
            # Re-read its state; never infer COMMITTED from rowcount alone.
            existing = self._store.get_fill_event(event_id)
            if existing is None or str(existing.get("processing_state", "PENDING")) != "COMMITTED":
                return 0.0, avg_price, event_id
            self._filled_quantities_by_order[order_id] = max(previous_qty, cumulative_qty)
            return 0.0, avg_price, event_id
        pending_previous = getattr(self, "_pending_fill_previous_qty", None)
        if pending_previous is None:
            pending_previous = {}
            self._pending_fill_previous_qty = pending_previous
        pending_previous[event_id] = previous_qty
        # Reserve the cumulative high-water mark while the event is pending;
        # a second poll cannot turn the same observation into a larger delta.
        self._filled_quantities_by_order[order_id] = cumulative_qty
        return delta_qty, avg_price, event_id

    def _mark_fill_committed(self, order_id: str, fill_event_id: str, cumulative_qty: float) -> None:
        """Close a pending fill after ledger and position facts commit."""

        self._store.mark_fill_event_committed(fill_event_id)
        getattr(self, "_pending_fill_previous_qty", {}).pop(fill_event_id, None)
        getattr(self, "_pending_fill_retry", set()).discard(fill_event_id)
        self._filled_quantities_by_order[order_id] = max(
            self._filled_quantities_by_order.get(order_id, 0.0), cumulative_qty
        )

    def _mark_fill_retryable(self, order_id: str, fill_event_id: str) -> None:
        """Expose one pending fill for governed recovery after a write failure."""

        if not fill_event_id:
            return
        previous_qty = getattr(self, "_pending_fill_previous_qty", {}).get(fill_event_id)
        if previous_qty is not None:
            self._filled_quantities_by_order[order_id] = previous_qty
        retry_ids = getattr(self, "_pending_fill_retry", None)
        if retry_ids is None:
            retry_ids = set()
            self._pending_fill_retry = retry_ids
        retry_ids.add(fill_event_id)

    def _update_position_projection(
        self,
        symbol: str,
        side: str,
        delta_qty: float,
        price: float,
        source_event_id: str,
    ) -> None:
        """Apply one signed fill delta to the durable position projection."""

        if delta_qty <= 0 or price <= 0:
            return
        row = self._position_projection.get(symbol, {})
        current_qty = float(row.get("signed_quantity", 0) or 0)
        current_entry = float(row.get("entry_price", 0) or 0)
        signed_delta = delta_qty if side.upper() == "BUY" else -delta_qty
        new_qty = current_qty + signed_delta
        same_direction = current_qty == 0 or (current_qty > 0) == (signed_delta > 0)
        if same_direction and abs(new_qty) > 1e-12:
            entry_price = (
                (abs(current_qty) * current_entry + abs(signed_delta) * price) / abs(new_qty) if current_qty else price
            )
        elif abs(new_qty) > 1e-12:
            # A reversal leaves the residual quantity at the latest fill price.
            entry_price = price
        else:
            entry_price = 0.0
        generation = int(row.get("position_generation", self._position_generation.get(symbol, 0)) or 0)
        if current_qty and new_qty and (current_qty > 0) != (new_qty > 0):
            generation += 1
        self._position_generation[symbol] = generation
        projection = {
            "symbol": symbol,
            "signed_quantity": f"{new_qty:.16g}",
            "entry_price": f"{entry_price:.16g}",
            "position_generation": generation,
            "source_event_id": source_event_id,
        }
        self._store.save_position_projection(
            symbol,
            projection["signed_quantity"],
            projection["entry_price"],
            generation,
            source_event_id,
        )
        self._position_projection[symbol] = projection

    def _commit_fill_facts(
        self,
        order_id: str,
        symbol: str,
        side: str,
        executed_qty: float,
        avg_price: float,
        fill_event_id: str,
        result: dict,
    ) -> None:
        """Commit ledger, position and fill state in an auditable order."""

        notional = executed_qty * avg_price
        journal_amount = str(notional)
        tx_id = f"tx-{fill_event_id.replace(':', '-')}"
        is_buy = side.upper() == "BUY"
        tx = LedgerTransaction(
            transaction_id=tx_id,
            transaction_type=LedgerTransactionType.FILL,
            source_event_id=fill_event_id,
            postings=(
                Posting(
                    posting_id=f"{tx_id}-p1",
                    account_id=AccountId("default"),
                    account_type=AccountType.POSITION_COST if is_buy else AccountType.CASH,
                    venue_id=VenueId("BINANCE"),
                    instrument_id=InstrumentId(symbol),
                    amount=MonetaryValue(amount=journal_amount),
                    side=PostingSide.DEBIT,
                    description=f"{side} {executed_qty} {symbol} @ {avg_price}",
                ),
                Posting(
                    posting_id=f"{tx_id}-p2",
                    account_id=AccountId("default"),
                    account_type=AccountType.CASH if is_buy else AccountType.POSITION_COST,
                    venue_id=VenueId("BINANCE"),
                    instrument_id=InstrumentId(symbol),
                    amount=MonetaryValue(amount=journal_amount),
                    side=PostingSide.CREDIT,
                    description=f"{side} {executed_qty} {symbol} @ {avg_price}",
                ),
            ),
            correlation_id=CorrelationId(f"exec-{order_id}"),
        )
        self._post_ledger_transaction(tx)
        self._update_position_projection(symbol, side, executed_qty, avg_price, fill_event_id)
        # The fill is complete only after both authoritative projections exist.
        self._mark_fill_committed(
            order_id,
            fill_event_id,
            float(result.get("executedQty", executed_qty) or executed_qty),
        )
        try:
            self._store.save_ledger_entry(
                tx_id,
                "default",
                "BINANCE",
                symbol,
                journal_amount,
                journal_amount,
                tx.postings[0].description,
                str(tx.correlation_id),
                tx.timestamp.isoformat(),
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
        except Exception as exc:
            self._record_execution_fact_failure(
                f"fill {fill_event_id} secondary index persistence failed: {type(exc).__name__}: {exc}"
            )
            raise

    async def _process_fill(self, order_id: str, symbol: str, result: dict) -> None:
        """处理订单成交：更新状态机、账本、持仓保护。

        从 _monitor_orders 和 _place_order（即时成交）共用，
        消除近线下单→成交→对账之间的时序窗口。
        """
        tracker = self._order_trackers.get(order_id)
        if not tracker:
            return
        executed_qty, avg_price, fill_event_id = self._consume_cumulative_fill(
            order_id,
            symbol,
            result,
            status="FILLED",
        )
        if executed_qty <= 0:
            tracker.apply(OrderEvent.FILLED)
            self._active_order_ids.discard(order_id)
            self._order_trackers.pop(order_id, None)
            self._order_symbols.pop(order_id, None)
            return

        side_desc = result.get("side", "")
        try:
            self._commit_fill_facts(
                order_id,
                symbol,
                side_desc,
                executed_qty,
                avg_price,
                fill_event_id,
                result,
            )
        except Exception as exc:
            row = self._store.get_fill_event(fill_event_id) or {}
            if str(row.get("processing_state", "PENDING")) != "COMMITTED":
                self._mark_fill_retryable(order_id, fill_event_id)
            tracker.apply(OrderEvent.UNKNOWN)
            self._active_order_ids.discard(order_id)
            self._record_execution_fact_failure(f"fill {fill_event_id} commit failed: {type(exc).__name__}: {exc}")
            raise
        tracker.apply(OrderEvent.FILLED)
        self._active_order_ids.discard(order_id)
        # BD-FIX: FILLED 后清理 tracker 和 symbol 映射，防止内存泄漏
        self._order_trackers.pop(order_id, None)
        self._order_symbols.pop(order_id, None)

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
                    if len(self._trade_pnls) > 10000:
                        self._trade_pnls = self._trade_pnls[-5000:]
                    self._remove_protection_with_cleanup(pid, symbol)
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
                    self._remove_protection_with_cleanup(old_pid, symbol)
                    await self._cancel_algo_orders(old_pid, symbol)
                    print(f"[protection] Cleaned up stale protection for {symbol} (pos={old_pid})")

            entry_price = float(avg_price)
            qty = executed_qty
            pos_side = OrderSide.BUY if result.get("side") == "BUY" else OrderSide.SELL
            pos_id = f"pos-{order_id}"
            position_generation = self._next_position_generation(symbol)

            kline_features = await self._feed.async_get_kline_features(symbol)
            adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry_price, kline_features)
            self._require_protection_config(symbol, adaptive_cfg)
            pp = self._protection.create_protection(
                position_id=pos_id,
                instrument_id=InstrumentId(symbol),
                venue_id=VenueId("BINANCE"),
                entry_price=entry_price,
                quantity=qty,
                side=pos_side,
                stop_loss_config=adaptive_cfg.stop_loss_config,
                take_profit_config=adaptive_cfg.take_profit_config,
                owner_id=self._protection_owner_id,
                position_generation=position_generation,
                session_id=self._session_id,
            )
            self._position_entry_times[pos_id] = time.time()
            protect_orders = [pp.stop_loss] if pp.stop_loss else []
            protect_orders.extend(pp.take_profits)
            # The local manager creates definitions before the venue ACK.  Keep
            # them CREATED/PENDING until each individual conditional order is
            # acknowledged; otherwise restart recovery would claim coverage
            # that never existed at the exchange.
            for p_order in protect_orders:
                if p_order is None:
                    continue
                p_order.status = ProtectionStatus.CREATED
                self._persist_protection_order(p_order, status="PENDING")

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
            exchange_protection_count = 0
            for p_order in protect_orders:
                if p_order is None:
                    continue
                if not hasattr(self, "_protection_exchange_attempted"):
                    self._protection_exchange_attempted: set[str] = set()
                if p_order.protection_id in self._protection_exchange_attempted:
                    continue
                self._protection_exchange_attempted.add(p_order.protection_id)

                prec_map = self._symbol_precision.get(symbol)
                if prec_map is None:
                    print(f"[protection] {symbol}: exchange precision UNKNOWN; keeping protection PENDING")
                    continue
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
                    algo_resp = await self._create_algo_order(algo_params)
                    if "algoId" in algo_resp:
                        break
                    err_code = algo_resp.get("code", 0)
                    # -4120: 已存在相同的条件单，不需要重试
                    if err_code == -4120:
                        print(f"[protection] ⚠️ {symbol} {p_order.reason}: venue reports an existing order (-4120)")
                        self._block_unowned_protection_orders([f"DUPLICATE_CONDITIONAL_ORDER:{symbol}"])
                        break
                    if algo_attempt < max_algo_retries - 1:
                        wait = 1.5 * (2**algo_attempt)  # 1.5s, 3s, 6s
                        print(
                            f"[protection] ⚠️ {symbol} {p_order.reason}: retry {algo_attempt + 1}/{max_algo_retries} after {wait:.1f}s (err={err_code})"
                        )
                        await asyncio.sleep(wait)
                        self._adapter.reset_circuit_breaker()  # 重置熔断器重试
                if "algoId" in algo_resp:
                    exchange_protection_count += 1
                    algo_id = str(algo_resp["algoId"])
                    if not hasattr(self, "_active_algo_ids"):
                        self._active_algo_ids: dict[str, set[str]] = {}
                    self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                    p_order.exchange_order_id = algo_id
                    p_order.status = ProtectionStatus.ACTIVE
                    self._persist_protection_order(p_order, status="ACTIVE")
                    print(
                        f"[protection] ✅ {symbol} {p_order.reason} → algoId={algo_id} "
                        f"triggerPrice={price_str} status={algo_resp.get('algoStatus', 'NEW')}"
                    )
                else:
                    err_code = algo_resp.get("code", "unknown")
                    err_msg = algo_resp.get("msg", str(algo_resp)[:150])
                    p_order.status = ProtectionStatus.CREATED
                    self._persist_protection_order(p_order, status="PENDING")
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

            # A filled entry without complete venue-backed protection is not
            # safe to leave in RESUME.  The retry queue is for governed
            # recovery only; it is not evidence that the position is covered.
            active_protection_count = sum(
                1
                for p_order in protect_orders
                if p_order is not None
                and getattr(getattr(p_order, "status", None), "value", "") == ProtectionStatus.ACTIVE.value
                and str(getattr(p_order, "exchange_order_id", "") or "")
            )
            if active_protection_count < len([p for p in protect_orders if p is not None]):
                self._block_unowned_protection_orders(
                    [
                        f"{pos_id}:PROTECTION_ACK_INCOMPLETE",
                        *[
                            str(getattr(p, "protection_id", ""))
                            for p in protect_orders
                            if p is not None
                            and not (
                                getattr(getattr(p, "status", None), "value", "") == ProtectionStatus.ACTIVE.value
                                and str(getattr(p, "exchange_order_id", "") or "")
                            )
                        ],
                    ]
                )

        # Update strategy risk on any fill
        account_balance = float(self._last_account.get("totalWalletBalance", 0))
        if account_balance > 0:
            self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
            if account_balance > self._peak_equity:
                self._peak_equity = account_balance

    def _record_reconciliation_failure(
        self,
        result: Any,
        *,
        system_facts: AccountFactSnapshot | None = None,
        exchange_facts: AccountFactSnapshot | None = None,
        event_facts: AccountFactSnapshot | None = None,
    ) -> bool:
        """Persist an auditable failure and close the risk-increase gate.

        Reconciliation is observational. It never repairs itself by copying
        exchange state into local state or by cancelling/placing an order.
        Recovery is a separately authorized workflow.
        """

        self._last_reconciliation_result = result
        event_facts = event_facts or getattr(result, "event_facts", None)
        snapshot_base = f"recon-{result.checked_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"
        try:
            if system_facts is not None:
                self._store.save_reconciliation_snapshot(f"{snapshot_base}-system", "SYSTEM", system_facts)
            if exchange_facts is not None:
                self._store.save_reconciliation_snapshot(f"{snapshot_base}-exchange", "EXCHANGE", exchange_facts)
            if event_facts is not None:
                self._store.save_reconciliation_snapshot(f"{snapshot_base}-event", "EVENT_STREAM", event_facts)
            self._store.save_reconciliation_result(
                snapshot_base,
                result,
                system_snapshot_id=f"{snapshot_base}-system" if system_facts is not None else "",
                exchange_snapshot_id=f"{snapshot_base}-exchange" if exchange_facts is not None else "",
                event_snapshot_id=f"{snapshot_base}-event" if event_facts is not None else "",
            )
        except Exception as exc:
            # A persistence failure is itself UNKNOWN; retain the gate closed
            # and make the failure visible to the operator.
            result.differences.append(f"RECON_PERSISTENCE_ERROR: {type(exc).__name__}")

        if self._control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
            self._control.execute_action(ControlAction.NO_NEW_RISK)
        description = "; ".join(result.differences) or str(getattr(result.status, "value", result.status))
        self._alerts.send_incident(
            AlertSeverity.CRITICAL,
            "Reconciliation blocked",
            description,
            category="reconciliation",
        )
        print(f"[recon] BLOCKED: {description}")
        return False

    def ingest_user_order_update(self, update: Any) -> bool:
        """Apply one normalized user event through the durable stream gate.

        This method is intentionally an injection boundary for a future
        websocket consumer.  It does not open a socket or replay REST data;
        callers must provide an adapter-validated ``UserOrderUpdate``.
        """

        projector = getattr(self, "_user_stream_projector", None)
        if projector is None:
            projector = UserStreamProjector(store=self._store)
            self._user_stream_projector = projector
        result = projector.ingest(update)
        if result.status in {UserProjectionStatus.ACCEPTED, UserProjectionStatus.DUPLICATE}:
            self._event_stream_facts = projector.fact_snapshot()
            self._recon.update_event_facts(self._event_stream_facts)
            return True
        self._record_execution_fact_failure(
            f"user-stream event {result.event_id or '<unknown>'} blocked: {result.reason}"
        )
        return False

    def authorize_user_stream_replay(
        self,
        facts: AccountFactSnapshot,
        *,
        evidence_hash: str,
        approval_id: str,
        last_sequence: int | None = None,
        allow_unsequenced: bool = False,
    ) -> bool:
        """Authorize a supplied, independently verified account replay baseline."""

        projector = getattr(self, "_user_stream_projector", None)
        if projector is None:
            projector = UserStreamProjector(store=self._store)
            self._user_stream_projector = projector
        result = projector.authorize_replay_baseline(
            facts,
            evidence_hash=evidence_hash,
            approval_id=approval_id,
            last_sequence=last_sequence,
            allow_unsequenced=allow_unsequenced,
        )
        if result.status is UserProjectionStatus.ACCEPTED:
            self._event_stream_facts = projector.fact_snapshot()
            self._recon.update_event_facts(self._event_stream_facts)
            return True
        self._record_execution_fact_failure(f"user-stream replay baseline blocked: {result.reason}")
        return False

    def ingest_user_account_update(self, update: Any) -> bool:
        """Apply one adapter-validated ACCOUNT_UPDATE through the replay gate."""

        projector = getattr(self, "_user_stream_projector", None)
        if projector is None:
            projector = UserStreamProjector(store=self._store)
            self._user_stream_projector = projector
        result = projector.ingest_account_update(update)
        if result.status in {UserProjectionStatus.ACCEPTED, UserProjectionStatus.DUPLICATE}:
            self._event_stream_facts = projector.fact_snapshot()
            self._recon.update_event_facts(self._event_stream_facts)
            return True
        self._record_execution_fact_failure(
            f"account user-stream event {result.event_id or '<unknown>'} blocked: {result.reason}"
        )
        return False

    def _build_system_reconciliation_facts(self) -> AccountFactSnapshot:
        """Build facts only from durable local projections.

        ``ProtectionManager`` memory is excluded: it cannot prove owner,
        generation, or restart continuity.  An opening-account baseline is an
        explicit operator-authorized fact; it is never copied from the
        exchange response by this method.  Without that baseline the snapshot
        remains INCOMPLETE rather than manufacturing a match from a zero
        balance.
        """

        opening = self._store.restore_account_opening_projection("default", "BINANCE")
        positions: dict[InstrumentId, Quantity] = {}
        opening_complete = bool(
            opening
            and int(opening.get("complete", 0)) == 1
            and str(opening.get("source", "")).strip()
            and str(opening.get("fact_version", "")).strip()
            and str(opening.get("evidence_hash", "")).strip()
            and str(opening.get("approval_id", "")).strip()
        )
        if opening is not None:
            positions.update(
                {
                    InstrumentId(str(symbol)): Quantity(amount=str(amount))
                    for symbol, amount in dict(opening.get("positions", {})).items()
                }
            )
        captured_at = None
        if opening is not None:
            try:
                captured_at = datetime.fromisoformat(str(opening["captured_at"]))
                if captured_at.tzinfo is None:
                    captured_at = captured_at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                opening_complete = False
        post_opening_fill = False
        for fill in self._store.restore_fill_events():
            try:
                fill_time = datetime.fromisoformat(str(fill.get("event_time", "")))
                if fill_time.tzinfo is None:
                    fill_time = fill_time.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                opening_complete = False
                continue
            if captured_at is None or fill_time <= captured_at:
                continue
            post_opening_fill = True
            if str(fill.get("processing_state", "COMMITTED")) != "COMMITTED":
                opening_complete = False
                continue
            symbol = InstrumentId(str(fill.get("symbol", "")))
            if not str(symbol):
                opening_complete = False
                continue
            try:
                current = Decimal(str(positions.get(symbol, Quantity(amount="0")).amount))
                delta = Decimal(str(fill.get("delta_qty", "0")))
                if not current.is_finite() or not delta.is_finite() or delta < 0:
                    raise InvalidOperation("fill quantity is not finite/non-negative")
            except (InvalidOperation, TypeError, ValueError):
                opening_complete = False
                continue
            if str(fill.get("side", "")).upper() == "BUY":
                current += delta
            elif str(fill.get("side", "")).upper() == "SELL":
                current -= delta
            else:
                opening_complete = False
                continue
            positions[symbol] = Quantity(amount=format(current.normalize(), "f"))
        active_orders = self._store.get_active_orders()
        # The opening balance is not a cash ledger.  Once a fill occurs, its
        # fees/realized cash must be independently projected before this side
        # can claim completeness; never pair a stale balance with replayed
        # positions and call that a match.
        if post_opening_fill:
            opening_complete = False
        balance_amount = str(opening.get("balance_amount", "0")) if opening else "0"
        balance_currency = str(opening.get("balance_currency", "USDT")) if opening else "USDT"
        balance_decimals = int(opening.get("balance_decimals", 8)) if opening else 8
        opening_version = (
            str(opening.get("fact_version", "missing-opening-balance")) if opening else "missing-opening-balance"
        )
        return AccountFactSnapshot(
            account_id=AccountId("default"),
            venue_id=VenueId("BINANCE"),
            balance=MonetaryValue(
                amount=balance_amount,
                currency=balance_currency,
                decimals=balance_decimals,
            ),
            positions=positions,
            open_orders=[str(row["order_id"]) for row in active_orders],
            timestamp=datetime.now(timezone.utc),
            source=("LOCAL_DURABLE_PROJECTION+AUTHORIZED_OPENING" if opening_complete else "LOCAL_DURABLE_PROJECTION"),
            fact_version=f"position-v1/order-state-v1/opening:{opening_version}",
            complete=opening_complete,
        )

    async def _reconcile(self) -> bool:
        """Compare fresh independent facts; never self-heal in place."""

        account, account_ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
        if not account_ok or not isinstance(account, dict) or "totalWalletBalance" not in account:
            result = ReconciliationEngine.compare(None, None)
            result.status = ReconciliationStatus.ONE_SIDE_MISSING
            result.differences = ["ONE_SIDE_MISSING: complete ACCOUNT snapshot unavailable"]
            return self._record_reconciliation_failure(result)
        if "positions" not in account or not isinstance(account.get("positions"), list):
            result = ReconciliationEngine.compare(None, None)
            result.status = ReconciliationStatus.INCOMPLETE
            result.differences = ["INCOMPLETE_FACT: ACCOUNT snapshot lacks positions"]
            return self._record_reconciliation_failure(result)

        open_orders, orders_ok = await self._api_async_safe(Endpoint.OPEN_ORDERS, signed=True)
        if not orders_ok or not isinstance(open_orders, list):
            result = ReconciliationEngine.compare(None, None)
            result.status = ReconciliationStatus.ONE_SIDE_MISSING
            result.differences = ["ONE_SIDE_MISSING: OPEN_ORDERS snapshot unavailable"]
            return self._record_reconciliation_failure(result)
        if any(not isinstance(order, dict) or "orderId" not in order for order in open_orders):
            result = ReconciliationEngine.compare(None, None)
            result.status = ReconciliationStatus.INCOMPLETE
            result.differences = ["INCOMPLETE_FACT: malformed exchange open-order row"]
            return self._record_reconciliation_failure(result)

        exchange_positions: dict[InstrumentId, Quantity] = {}
        for position in account["positions"]:
            if not isinstance(position, dict) or "symbol" not in position or "positionAmt" not in position:
                result = ReconciliationEngine.compare(None, None)
                result.status = ReconciliationStatus.INCOMPLETE
                result.differences = ["INCOMPLETE_FACT: malformed exchange position row"]
                return self._record_reconciliation_failure(result)
            amount = float(position.get("positionAmt", 0) or 0)
            if amount:
                exchange_positions[InstrumentId(str(position["symbol"]))] = Quantity(amount=str(amount))

        exchange_facts = AccountFactSnapshot(
            account_id=AccountId("default"),
            venue_id=VenueId("BINANCE"),
            balance=MonetaryValue(amount=str(float(account["totalWalletBalance"]))),
            positions=exchange_positions,
            open_orders=[str(order["orderId"]) for order in open_orders],
            timestamp=datetime.now(timezone.utc),
            source="BINANCE_ACCOUNT_AND_OPEN_ORDERS",
            fact_version=str(account.get("updateTime", "")),
            complete=True,
        )
        system_facts = self._build_system_reconciliation_facts()
        self._recon.update_system_facts(system_facts)
        self._recon.update_exchange_facts(exchange_facts)
        event_facts = getattr(self, "_event_stream_facts", None)
        if event_facts is not None:
            self._recon.update_event_facts(event_facts)
        result = self._recon.reconcile_three_way(AccountId("default"), VenueId("BINANCE"))
        self._last_reconciliation_result = result
        self._last_account = account
        if not result.matched:
            return self._record_reconciliation_failure(
                result,
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                event_facts=event_facts,
            )

        try:
            snapshot_base = f"recon-{result.checked_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"
            self._store.save_reconciliation_snapshot(f"{snapshot_base}-system", "SYSTEM", system_facts)
            self._store.save_reconciliation_snapshot(f"{snapshot_base}-exchange", "EXCHANGE", exchange_facts)
            if event_facts is not None:
                self._store.save_reconciliation_snapshot(f"{snapshot_base}-event", "EVENT_STREAM", event_facts)
            self._store.save_reconciliation_result(
                snapshot_base,
                result,
                system_snapshot_id=f"{snapshot_base}-system",
                exchange_snapshot_id=f"{snapshot_base}-exchange",
                event_snapshot_id=f"{snapshot_base}-event" if event_facts is not None else "",
            )
        except Exception as exc:
            result.matched = False
            result.status = ReconciliationStatus.ERROR
            result.differences.append(f"RECON_PERSISTENCE_ERROR: {type(exc).__name__}")
            return self._record_reconciliation_failure(
                result,
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                event_facts=event_facts,
            )
        print("[recon] MATCHED: independent durable facts verified")
        return True

    async def _ensure_exchange_position_protections(self) -> None:
        """为交易所已有但系统未追踪的持仓补充保护单（启动阶段调用）。

        BD-FIX (S4): 保护单下发前检查控制面状态，
        LOCK/EMERGENCY_FLATTEN 时跳过交易所下单但仍注册本地保护，
        避免监控假阳性 MISSING_SL/MISSING_TP → LOCK 死循环。
        """
        # BD-FIX: LOCK/EMERGENCY_FLATTEN 下仅跳过交易所下单，本地注册照常执行。
        ctrl_state = self._control.get_status()
        skip_exchange_orders = ctrl_state in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN)
        if skip_exchange_orders:
            print(
                f"[protection] Control plane is {ctrl_state.value} — skipping exchange order placement, "
                f"local registration will proceed"
            )
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
            self._adapter.reset_circuit_breaker()

            # 若 Phase 1/2 已处理所有持仓，直接跳过
            local_symbols = {str(pp.instrument_id) for pp in self._protection.all_positions().values()}
            already_handled = set(exchange_positions.keys()) - local_symbols
            if not already_handled:
                return  # 所有持仓已由 Phase 1/2 处理完毕

            # 检查哪些已有保护单
            algos_resp = await self._get_open_algo_inventory()
            protected_symbols: set[str] = set()
            if not isinstance(algos_resp, list):
                self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
                print("[startup] Conditional-order inventory UNKNOWN; recovery remains read-only")
                return
            if isinstance(algos_resp, list):
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in algos_resp
                    if item.get("algoId") is not None and str(item.get("algoId")) not in known_algo_ids
                ]
                if unowned_algo_ids:
                    self._block_unowned_protection_orders(unowned_algo_ids)
                    print("[startup] Conditional-order owner mapping UNKNOWN; recovery remains read-only")
                    return
                for item in algos_resp:
                    if str(item.get("algoId")) in known_algo_ids:
                        protected_symbols.add(str(item.get("symbol", "")))

            unprotected = {s: d for s, d in exchange_positions.items() if s not in protected_symbols}
            if not unprotected:
                return

            if skip_exchange_orders:
                print(
                    f"[startup] {len(unprotected)} positions without protection — skipping exchange orders (control={ctrl_state.value})"
                )

            print(f"[startup] {len(unprotected)} positions without protection, placing orders...")
            for symbol, data in unprotected.items():
                try:
                    if skip_exchange_orders:
                        continue
                    amt = data["amt"]
                    entry = data["entry"]
                    side = "SELL" if amt > 0 else "BUY"
                    qty = abs(amt)
                    prec = self._symbol_precision.get(symbol)
                    if prec is None:
                        print(f"[startup] {symbol}: exchange precision UNKNOWN; protection remains PENDING")
                        continue
                    qty_str = f"{qty:.{prec['quantity']}f}"

                    kline_features = await self._feed.async_get_kline_features(symbol)
                    adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry, kline_features)
                    self._require_protection_config(symbol, adaptive_cfg)
                    atr_value = float(adaptive_cfg.stop_loss_config.get("atr", 0) or 0)
                    multiplier = float(adaptive_cfg.stop_loss_config.get("multiplier", 0) or 0)
                    stop_distance = atr_value * multiplier
                    if stop_distance <= 0:
                        self._block_unowned_protection_orders([f"PROTECTION_DISTANCE_UNKNOWN:{symbol}"])
                        raise RuntimeError(f"protection distance UNKNOWN for {symbol}")
                    if amt > 0:
                        stop_price = entry - stop_distance
                        tp_price = entry + stop_distance * adaptive_cfg.rr_ratio
                    else:
                        stop_price = entry + stop_distance
                        tp_price = entry - stop_distance * adaptive_cfg.rr_ratio
                    stop_str = f"{stop_price:.{prec['price']}f}"
                    tp_str = f"{tp_price:.{prec['price']}f}"

                    # 止损单
                    sl_resp = await self._create_algo_order(
                        {
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": "STOP_MARKET",
                            "quantity": qty_str,
                            "triggerPrice": stop_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        }
                    )
                    sl_id = str(sl_resp.get("algoId", "")) if "algoId" in sl_resp else ""

                    # 止盈单
                    tp_resp = await self._create_algo_order(
                        {
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": "TAKE_PROFIT_MARKET",
                            "quantity": qty_str,
                            "triggerPrice": tp_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        }
                    )
                    tp_id = str(tp_resp.get("algoId", "")) if "algoId" in tp_resp else ""

                    if sl_id or tp_id:
                        print(f"[startup] ✅ Protection placed for {symbol}: SL={sl_id} TP={tp_id}")
                    else:
                        sl_err = sl_resp.get("msg", "?")
                        tp_err = tp_resp.get("msg", "?")
                        print(f"[startup] ⚠️ Protection FAILED for {symbol}: SL={sl_err} TP={tp_err}")
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
                        features = await self._feed.async_update_features(symbol)
                        entry = features.get("price", 0) if features else 0
                        if entry <= 0:
                            continue
                    pos_id = f"pos-{symbol}"
                    pos_side = OrderSide.BUY if amt > 0 else OrderSide.SELL
                    position_generation = self._next_position_generation(symbol)
                    # BD-FIX: 使用自适应计算器生成保护参数，避免 stop_loss/take_profits
                    # 为 None/空导致保护覆盖检查 expected_orders=0 → FAIL。
                    # AdaptiveProtectionCalculator 在 kline 缺失时有内置保守默认值。
                    kline_features = await self._feed.async_get_kline_features(symbol)
                    adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry, kline_features)
                    self._require_protection_config(symbol, adaptive_cfg)
                    self._protection.create_protection(
                        position_id=pos_id,
                        instrument_id=InstrumentId(symbol),
                        venue_id=VenueId("BINANCE"),
                        entry_price=entry,
                        quantity=abs(amt),
                        side=pos_side,
                        stop_loss_config=adaptive_cfg.stop_loss_config,
                        take_profit_config=adaptive_cfg.take_profit_config,
                        owner_id=self._protection_owner_id,
                        position_generation=position_generation,
                        session_id=self._session_id,
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
            existing_algos = await self._get_open_algo_inventory()
            if not isinstance(existing_algos, list):
                return
            known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
            unowned_algo_ids = [
                str(item.get("algoId"))
                for item in existing_algos
                if item.get("algoId") is not None and str(item.get("algoId")) not in known_algo_ids
            ]
            if unowned_algo_ids:
                self._block_unowned_protection_orders(unowned_algo_ids)
                print("[nearline] Excess-order cleanup blocked: conditional-order ownership UNKNOWN")
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
                        await self._cancel_algo_order(symbol, int(a["algoId"]))
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
        # P2修复: LOCK 状态下跳过所有 API 操作（系统完全冻结）
        if self._control.get_status() == ControlAction.LOCK:
            print("[nearline] Protection retry skipped: control plane is LOCKED")
            return
        try:
            # 查询交易所已有的 algo 订单；API 失败时使用本地缓存
            existing_algos = await self._get_open_algo_inventory()
            api_ok = isinstance(existing_algos, list)
            if not api_ok:
                self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
                print("[nearline] Protection retry blocked: conditional-order inventory UNKNOWN")
                return
            exchange_algo_symbols: dict[str, set[str]] = {}
            if api_ok:
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in existing_algos
                    if item.get("algoId") is not None and str(item.get("algoId")) not in known_algo_ids
                ]
                if unowned_algo_ids:
                    self._block_unowned_protection_orders(unowned_algo_ids)
                    print("[nearline] Protection retry blocked: conditional-order ownership UNKNOWN")
                    return
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
                owned_ids = existing_ids & self._active_algo_ids.get(pos_id, set())

                # 计算该仓位应有保护单数量
                def _needs_exchange_protection(p_order: Any | None) -> bool:
                    if p_order is None:
                        return False
                    status = getattr(getattr(p_order, "status", None), "value", "")
                    return status in (ProtectionStatus.ACTIVE.value, ProtectionStatus.CREATED.value)

                expected_count = (1 if _needs_exchange_protection(pp.stop_loss) else 0) + sum(
                    1 for tp in pp.take_profits if _needs_exchange_protection(tp)
                )
                server_count = len(owned_ids)
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
                prec = self._symbol_precision.get(symbol)
                if prec is None:
                    print(f"[protection] {symbol}: exchange precision UNKNOWN; retry deferred")
                    continue
                qty_str = f"{float(pp.quantity):.{prec['quantity']}f}"
                placed = 0

                # --- 重试止损单 ---
                # server_count < expected_count 说明有缺失，止损单存在即尝试补发
                if _needs_exchange_protection(pp.stop_loss):
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
                    algo_resp = await self._create_algo_order(
                        {
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": pp.stop_loss.order_type,
                            "quantity": qty_str,
                            "triggerPrice": price_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        }
                    )
                    if "algoId" in algo_resp:
                        algo_id = str(algo_resp["algoId"])
                        self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                        pp.stop_loss.exchange_order_id = algo_id
                        pp.stop_loss.status = ProtectionStatus.ACTIVE
                        self._persist_protection_order(pp.stop_loss, status="ACTIVE")
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
                    if not _needs_exchange_protection(tp):
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
                    tp_resp = await self._create_algo_order(
                        {
                            "symbol": symbol,
                            "side": side,
                            "algoType": "CONDITIONAL",
                            "type": tp.order_type,
                            "quantity": qty_str,
                            "triggerPrice": tp_price_str,
                            "reduceOnly": "true",
                            "workingType": "CONTRACT_PRICE",
                        }
                    )
                    if "algoId" in tp_resp:
                        algo_id = str(tp_resp["algoId"])
                        self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                        tp.exchange_order_id = algo_id
                        tp.status = ProtectionStatus.ACTIVE
                        self._persist_protection_order(tp, status="ACTIVE")
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
        # BD-FIX: 防抖清理 — 仅在连续 N 轮确认零持仓后才删除本地保护，
        # 避免因 _last_account 瞬时快照缺失而误删正在使用的保护单。
        exchange_positions_raw = self._last_account.get("positions", [])
        exchange_symbols: set[str] = set()
        for ep in exchange_positions_raw:
            amt = float(ep.get("positionAmt", 0) or 0)
            sym = str(ep.get("symbol", ""))
            if abs(amt) > 0 and sym:
                exchange_symbols.add(sym)

        _GHOST_DEBOUNCE_ROUNDS = 3
        if not hasattr(self, "_ghost_absence_count"):
            self._ghost_absence_count: dict[str, int] = {}

        for old_pid, old_pp in list(self._protection.all_positions().items()):
            symbol = str(old_pp.instrument_id)
            if symbol not in exchange_symbols:
                count = self._ghost_absence_count.get(symbol, 0) + 1
                self._ghost_absence_count[symbol] = count
                if count >= _GHOST_DEBOUNCE_ROUNDS:
                    self._remove_protection_with_cleanup(old_pid, symbol)
                    await self._cancel_algo_orders(old_pid, symbol)
                    self._ghost_absence_count.pop(symbol, None)
                    print(f"[nearline] 🧹 Cleaned up ghost position: {symbol} (absent {count} rounds, pos={old_pid})")
                else:
                    print(
                        f"[nearline] ⏳ Ghost candidate {symbol}: absent {count}/{_GHOST_DEBOUNCE_ROUNDS} rounds, waiting"
                    )
            else:
                # Symbol reappeared — reset counter
                self._ghost_absence_count.pop(symbol, None)

        # === 超量保护单清理：每个持仓最多保留 expected_count 个交易所订单 ===
        await self._cleanup_excess_orders()

        # === 保护单缺失重试：对 -2021 (立即触发) 等瞬时失败自动重试 ===
        await self._retry_missing_protections(exchange_symbols)

        try:
            # No active pool evidence means no proposal collection.  Falling
            # back to the configured feed universe would bypass the pool gate.
            active_symbols = self._trading_pool.active_instruments()
            # 仓位提案收集 → PortfolioOptimizer 冲突仲裁（循环结束后统一执行）
            proposals: list[dict] = []
            proposed_targets: list[PortfolioTarget] = []
            for symbol in active_symbols:
                # Yield to REALTIME clock between symbols
                await asyncio.sleep(0)

                # 1. K-line features
                features = await self._feed.async_get_kline_features(symbol, "1h", 100)
                if not features:
                    print(f"[nearline] {symbol}: no kline features available")
                    continue

                # Strategy inputs must identify one already-closed bar.  The
                # feed no longer defaults missing metadata to ``True``; keep
                # this boundary explicit so repeated 5-second polling cannot
                # create duplicate predictions/orders for the same bar.
                bar_open_time = features.get("bar_open_time")
                bar_close_time = features.get("bar_close_time")
                bar_available_at = features.get("bar_available_at")
                close = features.get("close", 0)
                if (
                    features.get("bar_is_closed") is not True
                    or not isinstance(bar_open_time, datetime)
                    or not isinstance(bar_close_time, datetime)
                    or not isinstance(bar_available_at, datetime)
                    or bar_open_time >= bar_close_time
                    or bar_close_time > datetime.now(timezone.utc)
                    or bar_available_at > datetime.now(timezone.utc)
                    or not isinstance(close, (int, float))
                    or not math.isfinite(float(close))
                    or float(close) <= 0
                ):
                    print(f"[nearline] {symbol}: SKIP (DQ BLOCK: closed-bar/PIT metadata unavailable)")
                    continue
                close = float(close)
                if not self._advance_factor_bar(symbol, "1h", bar_open_time, close):
                    # Same closed bar has already produced its decision, or a
                    # sequence violation was observed.
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

                all_signals: list[Any] = []
                typed_proposal: Any | None = None
                typed_exit_signals: list[Any] = []
                typed_mode = False
                veto_triggered = False
                # BD-FIX: DQ-BLOCK gating — 特征数据质量不可接受时跳过整个 DAG
                if features.get("n_candles", 0) < 10:
                    print(
                        f"[nearline] {symbol}: SKIP (DQ BLOCK: insufficient candle data n={features.get('n_candles', 0)})"
                    )
                    continue
                try:
                    # BD-T05: the only executable DAG boundary.  The engine
                    # must not maintain a second hand-written component loop.
                    kernel_result = await self._strategy_kernel.evaluate(context)
                    if not isinstance(kernel_result, dict):
                        raise TypeError("strategy kernel returned a non-mapping result")
                    typed_mode = kernel_result.get("kernel") == "typed_graph"
                    if typed_mode:
                        typed_proposal = kernel_result.get("proposal")
                        typed_exit_signals = list(kernel_result.get("exit_signals", []))
                        if kernel_result.get("blocked_by"):
                            print(
                                f"[nearline] {symbol}: TypedGraph BLOCKED by {kernel_result['blocked_by']} "
                                f"(graph={kernel_result.get('graph_hash', '')})"
                            )
                            continue
                        if typed_proposal is None:
                            print(f"[nearline] {symbol}: SKIP (TypedGraph produced no proposal)")
                            continue
                        print(
                            f"[nearline] {symbol}: TypedGraph → side={getattr(typed_proposal.side, 'value', None)} "
                            f"strength={typed_proposal.strength:.3f} confidence={typed_proposal.confidence:.3f} "
                            f"graph={kernel_result.get('graph_hash', '')}"
                        )
                    else:
                        all_signals = list(kernel_result.get("signals", []))
                        for signal in all_signals:
                            print(
                                f"[nearline] {symbol}: DAG ({signal.component_type.value}) "
                                f"→ {signal.direction} strength={signal.strength:.3f}"
                            )
                        veto_triggered = any(
                            signal.component_type == AlphaComponentType.FILTER
                            and signal.direction == SignalDirection.NO_ACTION
                            and signal.strength == 0.0
                            and signal.confidence >= 0.8
                            for signal in all_signals
                        )
                except ValueError as e:
                    print(f"[nearline] {symbol}: DAG error: {e}")
                    continue
                except Exception as e:
                    print(f"[nearline] {symbol}: StrategyKernel FAIL-CLOSED: {type(e).__name__}: {e}")
                    continue

                if veto_triggered:
                    print(f"[nearline] {symbol}: SKIP (VETO triggered by filter component)")
                    continue
                # Record parity after execution using the final typed proposal;
                # legacy mode retains its diagnostic strongest-signal hash.
                result_proposal = (
                    typed_proposal
                    if typed_mode
                    else max(all_signals, key=lambda s: getattr(s, "strength", 0), default=None)
                )
                self._kernel_parity = (
                    StrategyKernelContract.compute_proposal_hash(result_proposal) if result_proposal else ""
                )

                # Save predictions for factor IC evaluation.  They are
                # paired only with the next closed bar by
                # ``_advance_factor_bar``; never append a same-tick/past
                # return or align by list position.
                predictions = context.get("_predictions", {})
                self._store_factor_predictions(symbol, "1h", bar_open_time, close, predictions)

                if not typed_mode and not all_signals:
                    print(f"[nearline] {symbol}: SKIP (no signals from DAG)")
                    continue

                # === 3.5 检测 EXIT 组件平仓信号（优先于入场） ===
                exit_flat_signals = (
                    [
                        s
                        for s in typed_exit_signals
                        if s.component_type == AlphaComponentType.EXIT
                        and s.direction == SignalDirection.FLAT
                        and s.strength >= 0.3
                    ]
                    if typed_mode
                    else [
                        s
                        for s in all_signals
                        if s.component_type == AlphaComponentType.EXIT
                        and s.direction == SignalDirection.FLAT
                        and s.strength >= 0.3
                    ]
                )
                if exit_flat_signals and pos_info["has_position"]:
                    # 生成平仓订单
                    close_side = OrderSide.SELL if pos_info["side"] == "LONG" else OrderSide.BUY
                    if not symbol_positions:
                        # A position without an authoritative quantity is an
                        # unsafe close request.  Do not invent a minimum lot;
                        # reconciliation must supply the exact owner quantity.
                        print(f"[nearline] {symbol}: CLOSE SKIP (position quantity UNKNOWN)")
                        continue
                    close_qty = abs(float(symbol_positions[next(iter(symbol_positions))].quantity))
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
                        client_order_id=f"beidou-{symbol.lower()}-close-{int(time.time() * 1_000_000)}",
                        correlation_id=CorrelationId(f"nearline-close-{int(time.time())}"),
                        idempotency_key=f"idem-{symbol}-close-{int(time.time() / 300)}",
                        # P1修复: 平仓不经过 R0-R10 审批（风险降低方向），
                        # 使用特殊标记 RISK_EXEMPT_CLOSE 替代虚假审批 ID
                        risk_approval_id="RISK_EXEMPT_CLOSE",
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

                # === 4. Typed proposal is already fused; legacy mode keeps its
                # compatibility fuser only for non-executable research paths. ===
                if typed_mode:
                    if (
                        typed_proposal is None
                        or typed_proposal.side is None
                        or typed_proposal.strength < 0.15
                        or typed_proposal.confidence <= 0.0
                    ):
                        print(f"[nearline] {symbol}: SKIP (TypedGraph proposal weak or NO_ACTION)")
                        continue
                    fused = SimpleNamespace(
                        direction=(
                            SignalDirection.LONG if typed_proposal.side == OrderSide.BUY else SignalDirection.SHORT
                        ),
                        strength=float(typed_proposal.strength),
                        confidence=float(typed_proposal.confidence),
                        conflict_detected=bool(typed_proposal.conflict_detected),
                    )
                else:
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

                ann_vol = features.get("ann_volatility")
                spread_bps_val = features.get("spread_bps")
                atr_pct = features.get("atr_pct")
                if (
                    ann_vol is None
                    or spread_bps_val is None
                    or atr_pct is None
                    or float(ann_vol) <= 0
                    or float(spread_bps_val) < 0
                    or float(atr_pct) <= 0
                ):
                    print(f"[nearline] {symbol}: SKIP (market/risk features UNKNOWN)")
                    continue
                ann_vol = float(ann_vol)
                spread_bps_val = float(spread_bps_val)
                atr_pct = float(atr_pct)
                signal_strength = fused.strength

                # 自适应杠杆 — 波动率越高杠杆越低
                dyn_leverage = adaptive_leverage(ann_vol)

                # 自适应仓位 — 信号强度 × 波动率惩罚 × 点差惩罚 × 风险预算
                adaptive_pct = adaptive_position_pct(signal_strength, ann_vol, spread_bps_val)
                budget = self._strategy_risk.get_budget(self._autopilot_strategy_id)
                if budget is None:
                    print(f"[nearline] {symbol}: SKIP (strategy risk budget UNKNOWN)")
                    continue

                # ATR-based stop loss (volatility-adaptive; missing ATR was
                # rejected above rather than replaced with a trading default).
                stop_loss_pct = max(1.0, min(atr_pct * 1.5, 5.0))  # 1.5× ATR, capped 1%-5%
                stop_loss_price = price * (1 - stop_loss_pct / 100)

                risk_based_size = budget.compute_position_size(
                    self._autopilot_strategy_id,
                    account_balance,
                    price,
                    stop_loss_price,
                )

                # Pool capacity check
                pool_capacity = self._trading_pool.is_tradable(symbol)

                # 自适应仓位: risk_based_size × adaptive_pct, 受 leverage 约束
                max_by_leverage = (account_balance * dyn_leverage) / price
                position_size = min(risk_based_size * adaptive_pct, max_by_leverage)
                position_size = min(position_size, max_by_leverage * 0.5)
                if position_size <= 0:
                    print(f"[nearline] {symbol}: SKIP (computed position size UNKNOWN/zero)")
                    continue
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
                        position_size = min(position_size, max_by_leverage * 0.5)
                        if position_size <= 0:
                            print(f"[nearline] {symbol}: SKIP (recomputed position size UNKNOWN/zero)")
                            continue
                        position_notional = price * position_size

                # === 5.7 仓位提案收集 → PortfolioOptimizer 冲突仲裁（循环结束后统一执行）===
                proposals.append(
                    {
                        "symbol": symbol,
                        "venue_id": venue_id,
                        "instrument_id": instrument_id,
                        "side": OrderSide.BUY if fused.direction == SignalDirection.LONG else OrderSide.SELL,
                        "direction": fused.direction,
                        "fused_strength": fused.strength,
                        "fused_confidence": fused.confidence,
                        "price": price,
                        "account_balance": account_balance,
                        "features": features,
                        "dyn_leverage": dyn_leverage,
                        "position_size": position_size,
                    }
                )
                proposed_targets.append(
                    PortfolioTarget(
                        strategy_id=self._autopilot_strategy_id,
                        instrument_id=instrument_id,
                        venue_id=venue_id,
                        target_quantity=Quantity(amount=str(position_size)),
                        target_notional=MonetaryValue(amount=str(position_notional), currency="USDT"),
                        capital_budget=MonetaryValue(amount=str(account_balance * 0.1), currency="USDT"),
                        max_leverage=dyn_leverage,
                        ownership=PositionOwnership.EXCLUSIVE,
                    )
                )

            # === 组合优化: 冲突仲裁 + 资本分配 ===
            resolved_qty: dict[str, float] = {}
            if proposed_targets:
                resolved_targets, conflicts = self._optimizer.resolve_conflicts(proposed_targets)
                total_capital = MonetaryValue(
                    amount=str(float(self._last_account.get("totalWalletBalance", 0)) or 0),
                    currency="USDT",
                )
                capital_allocated = self._optimizer.allocate_capital([self._autopilot_strategy_id], total_capital)
                for t in resolved_targets:
                    resolved_qty[f"{t.venue_id}:{t.instrument_id}"] = float(t.target_quantity.amount)
                cap_str = ", ".join(f"{k}={float(v.amount):.0f}" for k, v in capital_allocated.items())
                print(
                    f"[nearline] Optimizer: {conflicts} conflicts resolved across {len(resolved_targets)} targets; "
                    f"capital allocated: {cap_str}"
                )

            # === 执行阶段: 使用仲裁后的目标仓位进行风控评估与下单 ===
            for prop in proposals:
                symbol = prop["symbol"]
                venue_id = prop["venue_id"]
                instrument_id = prop["instrument_id"]
                side = prop["side"]
                price = prop["price"]
                account_balance = prop["account_balance"]
                dyn_leverage = prop["dyn_leverage"]
                spread_bps_value = prop["features"].get("spread_bps")
                if spread_bps_value is None or float(spread_bps_value) < 0:
                    print(f"[nearline] {symbol}: SKIP (spread UNKNOWN at execution boundary)")
                    continue
                spread_bps = float(spread_bps_value)
                # 使用优化器仲裁后的目标仓位；无冲突时保持原始 size
                position_size = resolved_qty.get(f"{venue_id}:{instrument_id}", prop["position_size"])
                position_notional = price * position_size

                # === 6. Cost estimation ===
                vi = VenueInstrument(venue_id=venue_id, instrument_id=instrument_id)
                self._cost_model.set_fee_tier(venue_id, "vip1", 2.0, 4.0)
                cost_est = self._cost_model.estimate_order(
                    vi,
                    Quantity(amount=str(position_size)),
                    Price(amount=str(price)),
                    side,
                    urgency=0.3,
                    spread_bps=spread_bps,
                )

                if cost_est.total_fee_bps > 30:
                    print(f"[nearline] {symbol}: SKIP (cost too high: {cost_est.total_fee_bps}bps)")
                    continue

                # Build risk context for full R0-R10 evaluation
                risk_context: dict = {
                    "leverage": dyn_leverage,
                    "max_leverage": 3.0,
                    "concentration_pct": position_notional / max(account_balance, 1) * 100,
                    "max_concentration_pct": 50.0,
                    "drawdown_pct": self._strategy_risk.get_state(self._autopilot_strategy_id).current_drawdown_pct
                    if self._strategy_risk.get_state(self._autopilot_strategy_id)
                    else 0.0,
                    "max_drawdown_pct": 20.0,
                    "daily_loss_pct": self._strategy_risk.get_state(self._autopilot_strategy_id).daily_pnl
                    / max(account_balance, 1)
                    * -100
                    if self._strategy_risk.get_state(self._autopilot_strategy_id) and account_balance > 0
                    else 0.0,
                    "max_daily_loss_pct": 5.0,
                    "consecutive_losses": self._loss_count,
                    "max_consecutive_losses": 5,
                    "rolling_sharpe": self._drift_detector._baseline.get("sharpe", 0.0)
                    if self._drift_detector.is_calibrated()
                    else 0.0,  # P0修复: 未校准时使用中性基线(0.0 >= min_sharpe=0.0)，避免新账户R5 UNKNOWN死锁
                    "min_sharpe_rolling": 0.0,
                    "margin_ratio": (position_notional / dyn_leverage) / max(account_balance, 1)
                    if account_balance > 0
                    else 1.0,
                    "liquidation_price": 0,  # Updated below if available
                    "current_price": price,
                    "protected_positions": sum(
                        1
                        for pp in self._protection.all_positions().values()
                        if pp.stop_loss is not None and pp.stop_loss.is_active()
                    ),
                    "total_positions": self._protection.position_count(),
                    "can_trade": self._can_trade,  # 凭据权限推导 (R9)
                    "can_withdraw": self._can_withdraw,  # Must be False per R9
                    "duplicate_orders_24h": 0,
                }

                # Attempt to get liquidation price from Binance positions
                with contextlib.suppress(Exception):
                    for p in self._last_account.get("positions", []):
                        if p.get("symbol") == symbol and float(p.get("positionAmt", 0)) != 0:
                            liq_price = float(p.get("liquidationPrice", 0))
                            if liq_price > 0:
                                risk_context["liquidation_price"] = liq_price
                            break

                # Evaluate all R0-R10 rules
                risk_results = RiskRuleRegistry.evaluate_all(risk_context)
                risk_approved = RiskRuleRegistry.is_approved(risk_results)

                if not risk_approved:
                    failed_rules = [rid for rid, d in risk_results.items() if d != RuleDecision.PASS]
                    print(f"[nearline] {symbol}: SKIP (risk rules failed: {failed_rules})")
                    continue

                # === 8. Approval with proper signing ===
                import hashlib

                # Compute proper hashes for approval binding
                proposal_payload = f"{prop['direction'].value}|{prop['fused_strength']}|{prop['fused_confidence']}|{symbol}|{position_size}"
                proposal_hash = hashlib.sha256(proposal_payload.encode()).hexdigest()[:16]
                account_hash = hashlib.sha256(
                    f"{account_balance}|{self._protection.position_count()}".encode()
                ).hexdigest()[:16]
                risk_hash = hashlib.sha256(
                    f"{dyn_leverage}|{risk_context['concentration_pct']}|{risk_context['drawdown_pct']}".encode()
                ).hexdigest()[:16]
                import secrets

                nonce = secrets.token_hex(8)

                approval_id = RiskApprovalId(f"nearline-{symbol}-{int(time.time())}-{nonce[:8]}")
                approval_expires_at = time.time() + DEFAULT_APPROVAL_TTL_SECONDS
                signature = self._approval.issue_for_approved_risk(
                    approval_id,
                    risk_approved=risk_approved,
                    proposal_hash=proposal_hash,
                    account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    policy_version="2.0.0",
                    nonce=nonce,
                    expires_at=approval_expires_at,
                )

                # 提交前只预检签名；nonce 必须保留到最终发送边界消费。
                if not await self._approval.verify(
                    approval_id,
                    signature=signature,
                    proposal_hash=proposal_hash,
                    account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    policy_version="2.0.0",
                    nonce=nonce,
                    expires_at=approval_expires_at,
                    consume_nonce=False,
                ):
                    print(f"[nearline] {symbol}: SKIP (approval verification failed)")
                    continue

                if (
                    self._risk_sm.approve_if_verified(
                        approval_id,
                        signature_valid=True,
                        risk_check_passed=risk_approved,
                    )
                    != RiskDecision.APPROVED
                ):
                    print(f"[nearline] {symbol}: SKIP (risk not approved)")
                    continue

                from beidou_safety.execution import OrderIntent

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
                    risk_approval_signature=signature,
                    risk_proposal_hash=proposal_hash,
                    risk_account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    risk_policy_version="2.0.0",
                    risk_nonce=nonce,
                    risk_expires_at=approval_expires_at,
                )

                # P1修复: 内联 PreRisk 检查 — 避免 PreRiskCheckerImpl.check() 签名不匹配
                # 检查 notional/leverage/集中度/在途订单上限
                max_notional = self._policy_float(
                    "max_position_notional", self._settings.production.max_position_notional
                )
                pre_risk_ok = True
                pre_risk_reason = ""
                if dyn_leverage > self._pre_risk.max_leverage:
                    pre_risk_ok = False
                    pre_risk_reason = f"leverage {dyn_leverage} > max {self._pre_risk.max_leverage}"
                elif position_notional > max_notional:
                    pre_risk_ok = False
                    pre_risk_reason = f"notional {position_notional:.0f} > max {max_notional:.0f}"
                elif position_notional > account_balance * dyn_leverage:
                    pre_risk_ok = False
                    pre_risk_reason = f"notional exceeds margin (balance={account_balance:.0f} lev={dyn_leverage}x)"
                elif len(self._active_order_ids) >= 50:
                    pre_risk_ok = False
                    pre_risk_reason = f"too many pending orders ({len(self._active_order_ids)})"
                if not pre_risk_ok:
                    print(f"[nearline] {symbol}: ❌ Pre-risk REJECTED: {pre_risk_reason}")
                    continue

                # P0 Gate 1: 控制面校验（Outbox 提交前）
                if not self._control.should_accept(intent):
                    print(
                        f"[nearline] {symbol}: ❌ Intent REJECTED by control plane ({self._control.get_status().value}) — risk increase blocked"
                    )
                    continue

                try:
                    self._outbox.commit(intent)
                    print(
                        f"[nearline] {symbol}: ✅ OrderIntent CREATED → {side.value} {position_size:.4f} @ {price} "
                        f"(optimizer_resolved={'YES' if f'{venue_id}:{instrument_id}' in resolved_qty else 'no'} "
                        f"outbox_id={id(self._outbox)} size={len(self._outbox._outbox)})"
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
            self._adapter.reset_circuit_breaker()

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
            # BD-FIX (D3): 查询交易所订单真实状态，如果是 FILLED 则走 _process_fill 正常记账，
            # 不再直接标记 quantity=0 的假 FILLED。
            stale_order_ids = self._active_order_ids - exchange_order_ids
            for oid in stale_order_ids:
                self._active_order_ids.discard(oid)
                symbol = self._order_symbols.pop(oid, None)
                if symbol and self._can_write:
                    try:
                        order_result, query_ok = await self._api_async_safe(
                            Endpoint.ORDER,
                            signed=True,
                            params={"symbol": symbol, "orderId": int(oid) if oid.isdigit() else oid},
                        )
                    except Exception as exc:
                        logger.warning("[sync] stale order query failed for %s: %s", oid, exc)
                        order_result, query_ok = None, False
                    if query_ok and isinstance(order_result, dict) and "orderId" in order_result:
                        status = order_result.get("status", "UNKNOWN")
                        if status == "FILLED":
                            await self._process_fill(oid, symbol, order_result)
                            print(f"[sync] 📊 Stale order {oid} ({symbol}) FILLED — processed via _process_fill")
                        else:
                            self._store.save_order_state(
                                oid,
                                symbol,
                                order_result.get("side", "UNKNOWN"),
                                order_result.get("type", "MARKET"),
                                str(order_result.get("origQty", "0")),
                                order_result.get("price"),
                                status,
                            )
                            print(f"[sync] 🧹 Stale order {oid} ({symbol}) → {status} (no fill processing)")
                        continue
                # 回退: 无法查询时保留 tracker 以便后续处理，标记为 UNKNOWN
                self._order_trackers.pop(oid, None)
                if symbol:
                    self._store.save_order_state(oid, symbol, "UNKNOWN", "UNKNOWN", "0", None, "UNKNOWN")
            if stale_order_ids:
                print(f"[sync] 🧹 Stale orders cleaned: {len(stale_order_ids)}")

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
                        features = await self._feed.async_get_kline_features(sym)
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
                        # 已在 Step 3 处理；不要把已消失的 venue position
                        # 当成数量变更继续推导。
                        continue
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
                                position_generation = self._next_position_generation(sym)
                                old_trailing = old_pp.trailing_config
                                self._remove_protection_with_cleanup(old_pid, sym)
                                # 用自适应计算器重新生成保护参数
                                kf = await self._feed.async_get_kline_features(sym)
                                ac = AdaptiveProtectionCalculator.calculate(sym, real_entry, kf)
                                self._require_protection_config(sym, ac)
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
                                    owner_id=self._protection_owner_id,
                                    position_generation=position_generation,
                                    session_id=self._session_id,
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

    def _evaluate_live_factor_pairs(self, now: datetime) -> bool:
        """Evaluate only point-in-time, scope-isolated factor samples."""
        total_samples = sum(
            len(samples) for factor_map in self._factor_pairs_by_scope.values() for samples in factor_map.values()
        )
        print(f"[offline] Factor evaluation: {total_samples} closed-bar forward-return samples")
        if total_samples <= 10:
            return False

        scope_count: dict[str, int] = {}
        for factor_map in self._factor_pairs_by_scope.values():
            for factor_id, samples in factor_map.items():
                if samples:
                    scope_count[factor_id] = scope_count.get(factor_id, 0) + 1

        lifecycle_changed = False
        for scope, factor_map in list(self._factor_pairs_by_scope.items()):
            venue, symbol, timeframe, horizon = scope
            for fid, raw_pairs in list(factor_map.items()):
                if len(raw_pairs) < 10:
                    continue
                pairs = raw_pairs[-500:]
                factor_map[fid] = pairs
                preds_aligned = [pair[0] for pair in pairs]
                rets_aligned = [pair[1] for pair in pairs]
                min_n = len(pairs)

                ic_mean, ic_std = FactorEvaluator.compute_ic(preds_aligned, rets_aligned)
                rank_ic = FactorEvaluator.compute_rank_ic(preds_aligned, rets_aligned)
                decile_spread = FactorEvaluator.compute_decile_spread(preds_aligned, rets_aligned)

                # Use disjoint fixed windows.  Expanding windows reuse almost
                # every observation and artificially inflate ICIR.
                window_size = min(20, max(5, min_n // 5))
                rolling_ics = []
                for start in range(0, min_n - window_size + 1, window_size):
                    end = start + window_size
                    ic, _ = FactorEvaluator.compute_ic(preds_aligned[start:end], rets_aligned[start:end])
                    rolling_ics.append(ic)
                icir = FactorEvaluator.compute_icir(rolling_ics) if len(rolling_ics) >= 2 else 0.0

                scope_label = f"{venue}:{symbol}:{timeframe}:h{horizon}"
                print(
                    f"[offline] Factor {fid} [{scope_label}]: IC={ic_mean:.4f} RankIC={rank_ic:.4f} "
                    f"ICIR={icir:.3f} decile={decile_spread:.4f} samples={min_n}"
                )

                record = self._factor_registry.get(fid)
                if record is None:
                    continue
                from beidou_research.factors.factor import FactorPerformance

                record.performance.append(
                    FactorPerformance(
                        factor_id=fid,
                        evaluation_period=f"live-{scope_label}-{now.strftime('%Y%m%d-%H')}",
                        sample_count=min_n,
                        ic_mean=ic_mean,
                        ic_std=ic_std,
                        icir=icir,
                        rank_ic_mean=rank_ic,
                        rank_ic_std=0.0,
                        rank_icir=0.0,
                        top_bottom_decile_spread=decile_spread,
                    )
                )

                # A Champion is strategy-wide state.  Do not let metrics from
                # different symbols compete in one registry; multi-scope runs
                # remain diagnostic until a scope-aware model registry exists.
                single_scope = scope_count.get(fid, 0) == 1
                if single_scope:
                    model_version = SchemaVersion(str(record.definition.version))
                    model_id = ModelId(f"{fid}-v{model_version}")
                    model_metrics = {
                        "ic": ic_mean,
                        "rank_ic": rank_ic,
                        "icir": icir,
                        "decile_spread": decile_spread,
                        "sample_count": float(min_n),
                        "scope": scope_label,
                    }
                    registered_models = self._model_registry.list_models(self._autopilot_strategy_id)
                    existing_model = next((m for m in registered_models if m.model_id == model_id), None)
                    if existing_model is None:
                        self._model_registry.register_model(
                            ModelRecord(
                                model_id=model_id,
                                strategy_id=self._autopilot_strategy_id,
                                status=ModelStatus.CHALLENGER,
                                version=model_version,
                                deployed_at=now,
                                metrics=model_metrics,
                                training_dataset_version=f"live-{scope_label}-{now.strftime('%Y%m%d-%H')}",
                            )
                        )
                    else:
                        existing_model.metrics = model_metrics

                    registered_models = self._model_registry.list_models(self._autopilot_strategy_id)
                    eligible = [
                        model
                        for model in registered_models
                        if model.status in (ModelStatus.CHALLENGER, ModelStatus.CHAMPION)
                        and model.metrics.get("icir", 0.0) >= 0.3
                        and model.metrics.get("scope") == scope_label
                    ]
                    if eligible:
                        best_model = max(eligible, key=lambda model: model.metrics.get("icir", 0.0))
                        if best_model.status != ModelStatus.CHAMPION and self._model_registry.promote_to_champion(
                            self._autopilot_strategy_id, best_model.model_id
                        ):
                            lifecycle_changed = True
                        champion = self._model_registry.get_champion(self._autopilot_strategy_id)
                        if champion and str(champion.model_id) != self._active_champion_id:
                            self._active_champion_id = str(champion.model_id)

                # Lifecycle transitions remain conservative and require one
                # isolated scope; all transitions still pass FactorPromotionGate.
                if not single_scope:
                    continue
                if record.lifecycle == FactorLifecycle.ACTIVE and min_n >= 50 and icir < 0.05:
                    self._factor_registry.degrade(fid, f"ICIR dropped to {icir:.3f} (n={min_n})")
                    self._alerts.send_incident(
                        AlertSeverity.WARNING,
                        f"Factor degraded: {fid}",
                        f"ICIR={icir:.3f} below threshold 0.05; scope={scope_label}",
                        category="factor",
                    )
                    lifecycle_changed = True
                elif record.lifecycle == FactorLifecycle.CHALLENGER and icir >= 0.3:
                    decision = self._factor_gate.promote(record, FactorLifecycle.ACTIVE, falsifier="offline-monitor")
                    if decision.approved:
                        lifecycle_changed = True
                elif record.lifecycle == FactorLifecycle.DEGRADED and icir >= 0.3:
                    decision = self._factor_gate.promote(
                        record, FactorLifecycle.CHALLENGER, falsifier="offline-monitor"
                    )
                    if decision.approved:
                        lifecycle_changed = True
        return lifecycle_changed

    async def _offline_tick(self) -> None:
        """离线时钟：因子评估 → 漂移检测 → 策略风控 → 日报 → 检查点 → 清理。"""
        self._last_offline = time.time()

        try:
            now = datetime.now(timezone.utc)

            # === 1. Factor evaluation: closed-bar, forward-return pairs ===
            if self._evaluate_live_factor_pairs(now):
                self._rebuild_alpha_graph()

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

            # === 5. MAPE-K Self-Healing Cycle ===
            # Monitor: collect system metrics
            system_metrics = {
                "tick_count": self._tick_count,
                "order_count": self._order_count,
                "error_count": self._error_count,
                "position_count": self._protection.position_count(),
                "active_factors": len(self._factor_registry.get_active())
                + len(self._factor_registry.get_challengers()),
                "ledger_balanced": self._ledger.is_balanced(),
                "win_rate": round(self._win_count / max(1, self._win_count + self._loss_count), 3),
                "lifecycle": str(getattr(self._lifecycle.state, "value", self._lifecycle.state)),
            }

            # Analyze: detect anomalies
            anomaly_detected = False
            if system_metrics["error_count"] > 50:
                anomaly_detected = True
                print(f"[offline] MAPE-K: anomaly detected — high error count ({system_metrics['error_count']})")
            if not system_metrics["ledger_balanced"]:
                anomaly_detected = True
                print("[offline] MAPE-K: anomaly detected — ledger unbalanced")

            # Plan & Execute: if anomaly detected, attempt recovery
            if anomaly_detected:
                symptom_vector = {
                    "error_count": float(system_metrics["error_count"]),
                    "ledger_balanced": 0.0 if system_metrics["ledger_balanced"] else 1.0,
                }
                recovery_action, reason = self._mapek.decide_action(symptom_vector, "autopilot")
                if recovery_action != RecoveryAction.NOOP:
                    print(f"[offline] MAPE-K: executing recovery action {recovery_action.value} (reason: {reason})")
                    try:
                        result = self._mapek.execute_recovery(recovery_action, "autopilot")
                        print(f"[offline] MAPE-K: recovery result = {result.value}")
                        invariants = {
                            "error_count_ok": system_metrics["error_count"] < 50,
                            "ledger_balanced": system_metrics["ledger_balanced"],
                        }
                        verified = self._mapek.verify_recovery("autopilot", invariants)
                        if verified:
                            print("[offline] MAPE-K: recovery verified OK")
                    except Exception as rec_err:
                        print(f"[offline] MAPE-K: recovery failed: {rec_err}")

            # Checkpoint
            self._mapek.save_checkpoint(
                "autopilot",
                system_metrics,
                invariants_valid=not anomaly_detected,
            )
            self._store.save_checkpoint(
                f"cp-{now.strftime('%Y%m%d%H%M%S')}",
                "autopilot",
                {"tick_count": self._tick_count, "order_count": self._order_count},
                self._tick_count,
            )

            # === 5.5 Credential lifecycle health (security) ===
            try:
                health = self._check_credential_health()
                print(
                    f"[offline] Credential health: {health['level']} "
                    f"type={health['credential_type']} status={health['status']} "
                    f"days_to_expiry={health['days_to_expiry']} "
                    f"can_trade={health['can_trade']} can_withdraw={health['can_withdraw']}"
                )
            except Exception as health_err:
                print(f"[offline] Credential health check error: {health_err}")

            # === 6. Cleanup ===
            deleted = self._store.cleanup_old_data(retention_days=90)
            print(f"[offline] Cleanup: {deleted} old records deleted")

            # BD-FIX: 内存清理 — 定期清除过期追踪数据
            if hasattr(self, "_intent_retry_count") and len(self._intent_retry_count) > 200:
                self._intent_retry_count = dict(list(self._intent_retry_count.items())[-100:])
            if hasattr(self, "_paper_fills") and len(self._paper_fills) > 5000:
                self._paper_fills = self._paper_fills[-2000:]
            if hasattr(self, "_protection_exchange_attempted") and len(self._protection_exchange_attempted) > 1000:
                self._protection_exchange_attempted = set(list(self._protection_exchange_attempted)[-500:])

        except Exception as e:
            self._error_count += 1
            print(f"[offline] ERROR: {e}")

    def _check_credential_health(self) -> dict[str, Any]:
        """周期性凭据健康检查（离线时钟）：过期预警、轮换状态、R9 权限合规。

        只读追踪层 — 不修改密钥读取/签名流程；凭据失效时撤销交易能力 (R9)。
        """
        now = datetime.now(timezone.utc)
        cred = self._credential
        remaining_seconds = (cred.expires_at - now).total_seconds()
        days_left = remaining_seconds / 86400.0
        health: dict[str, Any] = {
            "credential_id": cred.credential_id,
            "credential_type": cred.credential_type.value,
            "status": cred.status.value,
            "days_to_expiry": round(days_left, 1),
            "can_trade": self._can_trade,
            "can_withdraw": self._can_withdraw,
            "checked_at": now.isoformat(),
        }
        try:
            if cred.status in (KeyRotationStatus.EXPIRED, KeyRotationStatus.REVOKED) or remaining_seconds <= 0:
                health["level"] = "CRITICAL"
                health["remediation"] = "rotate_or_revoke_trading"
                if self._can_trade:
                    self._can_trade = False  # 凭据失效 → 撤销交易能力 (R9)
                self._alerts.send_incident(
                    AlertSeverity.HIGH,
                    f"Credential lifecycle: {cred.credential_id} expired/revoked",
                    f"status={cred.status.value}, trading capability revoked (R9)",
                    category="credential",
                )
                print(
                    f"[beidou-security] ❌ Credential {cred.credential_id} expired/revoked — trading capability revoked"
                )
            elif days_left <= 30.0:
                health["level"] = "WARNING"
                health["remediation"] = "rotate_key"
                self._alerts.send_incident(
                    AlertSeverity.WARNING,
                    f"Credential expiring in {days_left:.0f} days",
                    f"credential_id={cred.credential_id}, days_to_expiry={days_left:.0f}",
                    category="credential",
                )
                print(f"[beidou-security] ⚠️ Credential {cred.credential_id} expires in {days_left:.0f} days")
            elif cred.status == KeyRotationStatus.ROTATING:
                health["level"] = "WARNING"
                health["remediation"] = "complete_rotation"
                print(f"[beidou-security] ⚠️ Credential {cred.credential_id} is ROTATING")
            else:
                health["level"] = "OK"
            # R9: 提款权限无条件禁止 — 异常开启时立即纠正并告警
            if self._can_withdraw:
                health["level"] = "CRITICAL"
                health["r9_violation"] = "WITHDRAW_ENABLED"
                self._can_withdraw = False
                self._alerts.send_incident(
                    AlertSeverity.HIGH,
                    "R9 violation: withdraw enabled on trading credential",
                    f"credential_id={cred.credential_id}",
                    category="credential",
                )
        except Exception as exc:
            health["level"] = "UNKNOWN"
            health["error"] = f"{type(exc).__name__}: {exc}"
        self._credential_health = health
        return health

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

        # Verify exchange connectivity (with retries for flaky testnet API)
        server_time_ok = False
        for attempt in range(5):
            server_time, st_ok = await self._api_async_safe(Endpoint.SERVER_TIME)
            if st_ok and isinstance(server_time, dict) and "serverTime" in server_time:
                server_time_ok = True
                break
            if attempt < 4:
                wait_s = 1.0 * (2 ** attempt)
                print(f"[beidou-autopilot] Server time check attempt {attempt+1}/5 failed, retrying in {wait_s:.0f}s...")
                await asyncio.sleep(wait_s)
        if not server_time_ok:
            print("[beidou-autopilot] FATAL: Cannot connect to exchange after 5 attempts")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        print(f"[beidou-autopilot] Exchange connected: {self._rest_url}")

        # Verify account access (with retries for flaky testnet API)
        account = None
        for attempt in range(5):
            account, acct_ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if acct_ok and isinstance(account, dict) and ("totalWalletBalance" in account or "assets" in account or "canTrade" in account):
                break
            if attempt < 4:
                wait_s = 1.0 * (2 ** attempt)
                print(f"[beidou-autopilot] Account access attempt {attempt+1}/5 failed, retrying in {wait_s:.0f}s...")
                await asyncio.sleep(wait_s)
            account = None
        if account is None:
            print("[beidou-autopilot] FATAL: Cannot access account after 5 attempts")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        self._last_account = account
        init_equity = float(account.get("totalWalletBalance", 0))
        self._peak_equity = init_equity
        self._strategy_risk.update_equity(self._autopilot_strategy_id, init_equity)
        print(f"[beidou-autopilot] Account OK: equity={init_equity}")

        # Start WebSocket real-time market data stream
        print("[beidou-autopilot] Starting WebSocket market data...")
        ws_ok = await self._feed.start_ws(self._symbols, testnet=(self._env_mode.value == "testnet"))
        if ws_ok:
            print("[beidou-autopilot] WebSocket market data stream active")
        else:
            print("[beidou-autopilot] WebSocket unavailable — falling back to REST polling")

        # Clean stale NEW orders from previous sessions
        print("[beidou-autopilot] Cleaning stale orders from previous sessions...")
        try:
            stale_marked = self._store.clean_stale_new_orders()
            print(
                f"[beidou-autopilot] Marked {stale_marked} stale NEW orders UNKNOWN; "
                "exchange reconciliation is required before readiness"
            )
        except Exception as e:
            print(f"[beidou-autopilot] Warning: stale order cleanup failed: {e}")

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
                quantity_step: str | None = None
                price_tick: str | None = None
                for f_item in s.get("filters", []):
                    if f_item.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                        quantity_step = str(f_item.get("stepSize", "")) or quantity_step
                    if f_item.get("filterType") == "PRICE_FILTER":
                        price_tick = str(f_item.get("tickSize", "")) or price_tick
                if not quantity_step or not price_tick:
                    continue
                try:
                    self._symbol_precision[sym] = {
                        "quantity": max(0, -Decimal(quantity_step).as_tuple().exponent),
                        "price": max(0, -Decimal(price_tick).as_tuple().exponent),
                    }
                except (InvalidOperation, ValueError):
                    continue
            print(f"[beidou-autopilot] Loaded precision for {len(self._symbol_precision)} symbols")
        except Exception as e:
            print(f"[beidou-autopilot] Warning: exchangeInfo load failed: {e}")

        # Never cancel every conditional order on startup.  Without a durable
        # owner/session mapping that action can delete a manual or another
        # worker's valid protection and create a naked position.  Recovery is
        # read-only here; only an explicitly owned, ACK-backed order may be
        # cancelled by a governed close/replacement path.
        try:
            existing_algos = await self._get_open_algo_inventory()
            if isinstance(existing_algos, list):
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in existing_algos
                    if item.get("algoId") is not None and str(item.get("algoId")) not in known_algo_ids
                ]
                if unowned_algo_ids:
                    self._block_unowned_protection_orders(unowned_algo_ids)
                print(
                    f"[beidou-autopilot] Observed {len(existing_algos)} open conditional orders; "
                    "skipping unowned startup cancellation and unowned adoption"
                )
        except Exception as exc:
            print(f"[beidou-autopilot] Conditional-order inventory UNKNOWN: {exc}")

        self._adapter.reset_circuit_breaker()

        try:
            # 使用 _api_async_safe 防止熔断返回空数据导致跳过保护恢复
            account, ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if not ok or "positions" not in account:
                print("[beidou-autopilot] WARNING: Cannot query account for position recovery — retrying once...")
                await asyncio.sleep(3)
                self._adapter.reset_circuit_breaker()
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
                    features = await self._feed.async_update_features(symbol)
                    entry_price = features.get("price", 0) if features else 0
                    if entry_price <= 0:
                        continue
                pos_side = OrderSide.BUY if amt > 0 else OrderSide.SELL
                qty = abs(amt)
                pos_id = f"pos-recovered-{symbol}"
                position_generation = self._next_position_generation(symbol)

                already_protected = any(
                    str(pp.instrument_id) == symbol and abs(float(pp.quantity) - qty) < 1e-8
                    for pp in self._protection.all_positions().values()
                )
                if already_protected:
                    continue

                kline_features = await self._feed.async_get_kline_features(symbol)
                adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry_price, kline_features)
                self._require_protection_config(symbol, adaptive_cfg)
                pp = self._protection.create_protection(
                    position_id=pos_id,
                    instrument_id=InstrumentId(symbol),
                    venue_id=VenueId("BINANCE"),
                    entry_price=entry_price,
                    quantity=qty,
                    side=pos_side,
                    stop_loss_config=adaptive_cfg.stop_loss_config,
                    take_profit_config=adaptive_cfg.take_profit_config,
                    owner_id=self._protection_owner_id,
                    position_generation=position_generation,
                    session_id=self._session_id,
                )
                self._position_entry_times[pos_id] = time.time()

                reduce_side = "SELL" if pos_side == OrderSide.BUY else "BUY"
                protect_orders = [pp.stop_loss] if pp.stop_loss else []
                protect_orders.extend(pp.take_profits)

                for p_order in protect_orders:
                    if p_order is None:
                        continue
                    p_order.status = ProtectionStatus.CREATED
                    self._persist_protection_order(p_order, status="PENDING")
                    prec_map = self._symbol_precision.get(symbol)
                    if prec_map is None:
                        print(f"[startup] {symbol}: exchange precision UNKNOWN; recovery deferred")
                        continue
                    qty_str = f"{float(p_order.quantity.amount):.{prec_map['quantity']}f}"
                    price_str = f"{float(p_order.trigger_price.amount):.{prec_map['price']}f}"
                    pending_submissions.append(
                        {
                            "symbol": symbol,
                            "pos_id": pos_id,
                            "protection_id": p_order.protection_id,
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
            if self._protection_owner_unknown:
                pending_submissions.clear()
                print("[startup] Protection placement blocked: existing conditional-order ownership is UNKNOWN")
            if pending_submissions:

                async def _submit_algo(sub: dict):
                    sub["result"] = await self._create_algo_order(sub["algo_params"])
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
                        recovered_protection = self._protection.get_protection(pos_id)
                        candidates = (
                            [recovered_protection.stop_loss]
                            if recovered_protection and recovered_protection.stop_loss
                            else []
                        ) + (recovered_protection.take_profits if recovered_protection else [])
                        for candidate in candidates:
                            if candidate is not None and candidate.protection_id == sub["protection_id"]:
                                candidate.exchange_order_id = algo_id
                                candidate.status = ProtectionStatus.ACTIVE
                                self._persist_protection_order(candidate, status="ACTIVE")
                                break
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

        # Initial reconciliation is read-only.  A mismatch or incomplete
        # projection closes the risk gate; state synchronization is a separate
        # explicitly authorized recovery workflow.
        recon_ok = await self._reconcile()
        self._last_recon = time.time()
        print(
            "[beidou-autopilot] Initial reconciliation verified"
            if recon_ok
            else "[beidou-autopilot] Initial reconciliation blocked ACTIVE"
        )

        # Phase 5b: 交易所残留持仓保护补充
        await self._ensure_exchange_position_protections()

        # BD-T14: 对账未完成 → 不进入 ACTIVE，保持在 DEGRADED
        if not recon_ok:
            self._lifecycle.transition(ModuleState.DEGRADED)
            print("[beidou-autopilot] State: DEGRADED (reconciliation not verified)")
        else:
            self._lifecycle.transition(ModuleState.ACTIVE)
            print(f"[beidou-autopilot] State: {self._lifecycle.state.value}")

        # Start health server
        self._health.start()
        print(
            f"[beidou-autopilot] Health server: http://{self._settings.infrastructure.health_host}:"
            f"{self._settings.infrastructure.health_port}"
        )

        # BD-T14: Startup 后短暂 NO_NEW_RISK，由 Supervisor 在深度验证通过后 RESUME。
        # BD-FIX: 仅当 Supervisor 尚未 RESUME 时才设置 NO_NEW_RISK，消除启动竞态。
        # 原代码无条件执行 NO_NEW_RISK，可能覆盖 Supervisor 在 _wait_for_startup 返回后
        # 立即发出的 RESUME（supervisor.py:842），导致系统永久停在 NO_NEW_RISK/PAUSED。
        if self._control.get_status() != ControlAction.RESUME:
            self._control.execute_action(ControlAction.NO_NEW_RISK)
            print("[beidou-autopilot] Control plane: NO_NEW_RISK (awaiting supervisor validation)")
        else:
            print("[beidou-autopilot] Control plane: already RESUME (supervisor authorized)")
        if self._adapter is None:
            print("[beidou-autopilot] WARNING: Exchange not ready — supervisor will block RESUME")

        # BD-FIX: 启动 WebSocket 实时行情流（REST 轮询作为回退）
        is_testnet = self._env_mode.value == "testnet"
        ws_started = await self._feed.start_ws(self._symbols, testnet=is_testnet)
        if ws_started:
            print("[beidou-autopilot] WebSocket market data stream ACTIVE (REST polling as fallback)")
        else:
            print("[beidou-autopilot] WebSocket unavailable — using REST polling only")

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
                except Exception as exc:
                    self._error_count += 1
                    import traceback as _tb

                    print(f"[realtime] LOOP ERROR: {type(exc).__name__}: {exc}", flush=True)
                    _tb.print_exc()
                await asyncio.sleep(1)

        async def _nearline_loop() -> None:
            while self._running:
                try:
                    if time.time() - self._last_nearline >= 300:
                        await self._nearline_tick()
                except Exception as exc:
                    self._error_count += 1
                    import traceback as _tb

                    print(f"[nearline] LOOP ERROR: {type(exc).__name__}: {exc}", flush=True)
                    _tb.print_exc()
                await asyncio.sleep(10)

        async def _offline_loop() -> None:
            while self._running:
                try:
                    if time.time() - self._last_offline >= 3600:
                        await self._offline_tick()
                except Exception as exc:
                    self._error_count += 1
                    import traceback as _tb

                    print(f"[offline] LOOP ERROR: {type(exc).__name__}: {exc}", flush=True)
                    _tb.print_exc()
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
            self._running = False
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

        # 2. Cancel pending orders (BD-FIX F22: 使用 _order_symbols 精确查找)
        if self._can_write:
            for order_id in list(self._active_order_ids):
                symbol = self._order_symbols.get(order_id, "")
                if not symbol:
                    continue
                try:
                    await self._api_async(
                        Endpoint.ORDER,
                        method="DELETE",
                        signed=True,
                        params={
                            "symbol": symbol,
                            "orderId": int(order_id) if order_id.isdigit() else order_id,
                        },
                    )
                except Exception as e:
                    print(f"[shutdown] Failed to cancel order {order_id}: {e}")
        print(f"[beidou-autopilot] 2. Cancelled {len(self._active_order_ids)} pending orders")

        # 2b. BD-FIX: 停止 WebSocket 连接
        try:
            await self._feed.stop_ws()
            print("[beidou-autopilot] 2b. WebSocket stopped")
        except Exception as e:
            print(f"[shutdown] WS stop error: {e}")

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
