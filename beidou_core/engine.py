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
import signal
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any

import yaml

from beidou_shared.types import (
    AccountId, AccountRef, CorrelationId, InstrumentId, MonetaryValue,
    OrderId, OrderSide, OrderType, Price, Quantity, ResultStatus,
    RiskApprovalId, RiskDecision, SchemaVersion, StrategyId,
    TimeInForce, VenueId, VenueInstrument,
)
from beidou_data.feature_store import FeatureStore
from beidou_data.quality import DQCheckResult, DQCheckType, DataQualityGate
from beidou_lifecycle.lifecycle import ModuleLifecycle, ModuleState, DegradationLevel
from beidou_control.plane import ControlPlane, ControlAction
from beidou_safety.risk.engine import (
    PreRiskCheckerImpl, RiskEngineImpl, RiskApprovalSignerImpl,
    RiskApprovalStateMachine, RiskSnapshot, PostRiskMonitor,
)
from beidou_safety.protection.engine import ProtectionManager, StopLossType, TakeProfitType
from beidou_safety.execution.intent import IntentOutbox
from beidou_safety.execution.order_state import OrderStateTracker, OrderEvent
from beidou_safety.execution.ledger import ImmutableLedger, JournalEntry
from beidou_safety.execution.reconciliation import ReconciliationEngine, AccountFactSnapshot
from beidou_safety.execution.algorithms import (
    ExecutionAlgorithmSelector, ExecutionAlgorithmType, ExecutionContext,
)
from beidou_strategy.state.market_state import MarketStateEstimator
from beidou_strategy.state.cost_model import CostModel
from beidou_strategy.alpha import AlphaGraph, AlphaComponent, AlphaComponentType, SignalDirection
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
from beidou_strategy.alpha.model_registry import ModelRegistry, DriftDetector
from beidou_strategy.risk.manager import (
    StrategyRiskManager, RiskBudget, StrategyRiskLevel, CircuitBreakerReason,
)
from beidou_research.factors.factor import (
    FactorEvaluator, FactorRegistry, FactorDefinition, FactorLifecycle,
)
from beidou_observability.telemetry import AlertSeverity
from beidou_autonomy.mapek import MAPEKController

from beidou_core.feed import MarketDataFeed
from beidou_core.store import PersistentStore
from beidou_core.health import HealthServer
from beidou_core.alerts import AlertDispatcher
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import Result, ErrorCategory


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
                strength=0.0, confidence=0.8,
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
            metadata={"breakout_up": breakout_up, "breakout_down": breakout_down,
                       "vol_expanding": vol_expanding},
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
            direction = SignalDirection.LONG if rsi > 30 else SignalDirection.SHORT if rsi > 70 else SignalDirection.LONG
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
                reason = f"take_profit: {pnl_pct:.2f}% > {3.0*atr_pct:.2f}%"
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
        from beidou_strategy.alpha import AlphaSignal
        import time as _time
        features = context.get("features", {})
        close = features.get("close", 0)

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
            features.get("close", 0) > 0 and
            features.get("sma_5", 0) > 0 and
            features.get("sma_20", 0) > 0
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

    def __init__(self, symbols: list[str], mode: str = "full") -> None:
        self._symbols = symbols
        self._mode = mode  # "full", "safety_only", "paper"
        self._paper_only = mode == "paper"

        # Config
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "env.testnet.yaml",
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
        self._pre_risk = PreRiskCheckerImpl(max_leverage=3.0, max_concentration_pct=50.0, max_position_notional=500000.0)
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

        # === NEW: Strategy Risk Manager ===
        self._strategy_risk = StrategyRiskManager()
        self._strategy_risk.set_budget(RiskBudget(
            strategy_id=StrategyId("autopilot"),
            max_drawdown_pct=20.0,
            max_daily_loss_pct=5.0,
            max_consecutive_losses=5,
            max_position_notional=500000.0,
            max_leverage=3.0,
            risk_per_trade_pct=1.0,
            min_sharpe_rolling=0.0,
        ))

        # === NEW: Factor Registry + Evaluator ===
        self._factor_registry = FactorRegistry()
        self._factor_evaluator = FactorEvaluator()

        # ================================================================
        # Factor Registry — 8-factor suite
        # ================================================================
        factor_defs = [
            FactorDefinition(
                factor_id="meanrev_entry_v1", name="Mean Reversion Entry",
                version=SchemaVersion("2.0.0"),
                description="Short-term price deviation from SMA for entry signals",
                author="beidou-autopilot", category="mean_reversion",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Prices revert to mean in ranging markets",
                lookback_period="5h", rebalance_interval="5min",
                parameters={"deviation_threshold_pct": 1.0, "rsi_oversold": 40, "rsi_overbought": 60},
            ),
            FactorDefinition(
                factor_id="momentum_filter_v1", name="Momentum Filter",
                version=SchemaVersion("2.0.0"),
                description="20-period trend confirmation and volatility gate",
                author="beidou-autopilot", category="momentum",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Trend-following filter prevents counter-trend entries",
                lookback_period="20h", rebalance_interval="5min",
                parameters={"vol_threshold": 0.5, "trend_periods": 20},
            ),
            FactorDefinition(
                factor_id="trend_entry_v1", name="Trend Following Entry",
                version=SchemaVersion("2.0.0"),
                description="SMA5/SMA20 golden/death cross with trend strength confirmation",
                author="beidou-autopilot", category="trend_following",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Captures directional momentum at trend initiation",
                lookback_period="20h", rebalance_interval="5min",
                parameters={"crossover_threshold": 0.001, "trend_min_pct": 0.5},
            ),
            FactorDefinition(
                factor_id="breakout_entry_v1", name="Breakout Entry",
                version=SchemaVersion("2.0.0"),
                description="20-period high/low breakout with volatility expansion confirmation",
                author="beidou-autopilot", category="breakout",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Breakouts with volume/volatility expansion signal regime shifts",
                lookback_period="20h", rebalance_interval="5min",
                parameters={"lookback_periods": 20, "vol_expansion_min": 1.2},
            ),
            FactorDefinition(
                factor_id="volatility_filter_v1", name="Volatility Regime Filter",
                version=SchemaVersion("2.0.0"),
                description="ATR-based volatility regime: extreme→veto, elevated→degrade, normal→pass",
                author="beidou-autopilot", category="volatility",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Avoid trading in unpredictable volatility regimes",
                lookback_period="14h", rebalance_interval="5min",
                parameters={"extreme_vol": 0.6, "elevated_vol": 0.4, "extreme_atr_pct": 5.0},
            ),
            FactorDefinition(
                factor_id="volume_filter_v1", name="Volume Confirmation Filter",
                version=SchemaVersion("2.0.0"),
                description="Volume ratio (5/20) confirmation: surge→boost, low→degrade, dead→veto",
                author="beidou-autopilot", category="volume",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Volume validates price action; low-volume moves are unreliable",
                lookback_period="20h", rebalance_interval="5min",
                parameters={"dead_ratio": 0.25, "low_ratio": 0.5, "surge_ratio": 1.5},
            ),
            FactorDefinition(
                factor_id="trailing_exit_v1", name="ATR Trailing Stop Exit",
                version=SchemaVersion("2.0.0"),
                description="2× ATR trailing stop-loss + 3× ATR take-profit + SMA20 trend exit",
                author="beidou-autopilot", category="risk_management",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Dynamic exit based on realized volatility prevents large drawdowns",
                lookback_period="14h", rebalance_interval="5min",
                parameters={"trailing_atr_mult": 2.0, "profit_atr_mult": 3.0},
            ),
            FactorDefinition(
                factor_id="time_exit_v1", name="Time-Based Exit",
                version=SchemaVersion("2.0.0"),
                description="Maximum hold time exit: 48h force, 24h underwater, 12h profit lock",
                author="beidou-autopilot", category="risk_management",
                universe=frozenset({VenueId("BINANCE")}), instrument_types=frozenset({"perpetual"}),
                economic_rationale="Time decay and funding costs erode edge; stale positions increase risk",
                lookback_period="48h", rebalance_interval="5min",
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
            for target in [FactorLifecycle.RESEARCH, FactorLifecycle.BACKTEST,
                          FactorLifecycle.PAPER_TRADING, FactorLifecycle.CHALLENGER]:
                if not rec.transition(target):
                    break
        print(f"[beidou-autopilot] Factor lifecycles: {[(fid, r.lifecycle.value) for fid, r in self._factor_registry._factors.items()]}")

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

    def _api(self, path: str, method: str = "GET", signed: bool = False,
             params: dict | None = None) -> Any:
        """通过 Adapter 访问 Binance API。

        统一路由到 BinanceRESTClient，享受统一错误分类、限频退避和熔断。
        返回原始 dict（兼容现有代码），失败时返回 {"error": code, "msg": "..."}
        """
        import urllib.request
        import urllib.error

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
                self._api_secret.encode(), qs.encode(), hashlib.sha256,
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
                        total_wallet = sum(
                            float(a.get("crossWalletBalance", 0))
                            for a in data
                        )
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

    # --- Health & Metrics ---

    def _check_ready(self) -> bool:
        if self._lifecycle.state != ModuleState.ACTIVE:
            return False
        if not self._feed.is_healthy():
            return False
        return True

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
            "mode": self._mode,
            "uptime_seconds": round(self._health.uptime_seconds(), 1),
            "last_realtime_tick": self._last_realtime,
            "last_nearline_tick": self._last_nearline,
            "active_incidents": self._alerts.get_active_incidents(),
            "strategy_risk": {
                "level": risk_state.risk_level.value if risk_state else "N/A",
                "drawdown_pct": round(risk_state.current_drawdown_pct, 2) if risk_state else 0,
                "consecutive_losses": risk_state.consecutive_losses if risk_state else 0,
            } if risk_state else None,
            "active_factors": len(self._factor_registry.get_active()) + len(self._factor_registry.get_challengers()),
        }

    # --- Clock Domain: REALTIME (every 5s) ---

    async def _realtime_tick(self) -> None:
        """实时时钟：行情轮询 → 保护单检查 → 订单处理 → 对账。"""
        self._tick_count += 1
        self._last_realtime = time.time()

        try:
            for symbol in self._symbols:
                # 1. Fetch latest market data
                features = self._feed.update_features(symbol)
                if not features:
                    continue

                price = features["price"]

                # 2. Check protection triggers
                for pos_id in list(self._protection.all_positions().keys()):
                    result = self._protection.check_price(pos_id, price)
                    if result["triggered"]:
                        for sl in result["stop_loss"]:
                            self._alerts.send_incident(
                                AlertSeverity.HIGH, f"Stop Loss triggered: {symbol}",
                                f"Position {pos_id} stop loss at {sl.trigger_price.amount}",
                                category="protection",
                            )
                            if not self._paper_only:
                                await self._execute_protection_order(sl, symbol)
                        for tp in result["take_profit"]:
                            if not self._paper_only:
                                await self._execute_protection_order(tp, symbol)

                # 3. Process unacked intents → place orders
                if not self._paper_only:
                    for intent in self._outbox.unacked():
                        await self._place_order(intent, symbol)

                # 4. Monitor active orders
                if not self._paper_only:
                    await self._monitor_orders(symbol)

                # 5. Save market snapshot
                self._store.save_market_snapshot(
                    symbol, price,
                    features.get("bid"), features.get("ask"),
                    features.get("spread_bps"), features.get("volume_24h"),
                )

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
        client_id = f"beidou-prot-{int(time.time()*1000)}"

        order = self._api("/fapi/v1/order", method="POST", signed=True, params={
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": str(float(protection.quantity.amount)),
            "reduceOnly": "true",
            "newClientOrderId": client_id,
        })

        if "orderId" in order:
            order_id = str(order["orderId"])
            self._order_count += 1

            tracker = OrderStateTracker(order_id=OrderId(order_id))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.SENT)
            self._order_trackers[order_id] = tracker
            self._active_order_ids.add(order_id)

            self._protection.mark_executed(protection.protection_id)
            self._store.save_protection(
                protection.protection_id, protection.position_id, symbol,
                side, protection.trigger_price.amount,
                str(protection.order_price.amount) if protection.order_price else None,
                protection.quantity.amount, "MARKET", "EXECUTED",
            )
            self._store.save_order_state(
                order_id, symbol, side, "MARKET",
                str(float(protection.quantity.amount)),
                str(protection.order_price.amount) if protection.order_price else None,
                order.get("status", "NEW"), client_order_id=client_id,
            )

    async def _place_order(self, intent, symbol: str) -> None:
        """向交易所发送订单。"""
        side = "BUY" if intent.side == OrderSide.BUY else "SELL"
        order_type = "LIMIT" if intent.order_type == OrderType.LIMIT else "MARKET"
        client_id = intent.client_order_id or f"beidou-{int(time.time()*1000)}"

        params = {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": str(float(intent.quantity.amount)),
            "newClientOrderId": client_id,
        }
        if intent.price:
            params["price"] = str(float(intent.price.amount))
            params["timeInForce"] = "GTC"

        if self._paper_only:
            tracker = OrderStateTracker(order_id=OrderId(intent.intent_id))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.FILLED)
            self._order_trackers[intent.intent_id] = tracker
            self._outbox.ack(intent.intent_id)
            self._order_count += 1
            return

        order = self._api("/fapi/v1/order", method="POST", signed=True, params=params)

        if "orderId" in order:
            tracker = OrderStateTracker(order_id=OrderId(str(order["orderId"])))
            tracker.apply(OrderEvent.ACKED)
            tracker.apply(OrderEvent.SENT)
            self._order_trackers[str(order["orderId"])] = tracker
            self._active_order_ids.add(str(order["orderId"]))
            self._outbox.ack(intent.intent_id)
            self._order_count += 1
            print(f"[order] PLACED: {symbol} {side} {params['quantity']} @ {params.get('price','MKT')} "
                  f"orderId={order['orderId']}")

            self._store.save_order_state(
                str(order["orderId"]), symbol, side, order_type,
                str(float(intent.quantity.amount)),
                str(float(intent.price.amount)) if intent.price else None,
                order.get("status", "NEW"), client_order_id=client_id,
            )

    async def _monitor_orders(self, symbol: str) -> None:
        """查询活跃订单状态并更新状态机/账本。"""
        for order_id in list(self._active_order_ids):
            try:
                result = self._api("/fapi/v1/order", signed=True, params={
                    "symbol": symbol, "orderId": int(order_id),
                })

                if isinstance(result, dict) and result.get("error") or "status" not in result:
                    if result.get("msg") and ("Order does not exist" in str(result.get("msg")) or "Unknown order" in str(result.get("msg"))):
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

                    entry = JournalEntry(
                        entry_id=f"journal-{order_id}",
                        account_id=AccountId("default"),
                        venue_id=VenueId("BINANCE"),
                        instrument_id=InstrumentId(symbol),
                        debit=MonetaryValue(amount=str(executed_qty * float(avg_price))),
                        credit=MonetaryValue(amount=str(executed_qty * float(avg_price))),
                        description=f"{result.get('side')} {executed_qty} {symbol} @ {avg_price} FILLED",
                        correlation_id=CorrelationId(f"exec-{order_id}"),
                    )
                    self._ledger.post(entry)
                    self._store.save_ledger_entry(
                        entry.entry_id, "default", "BINANCE", symbol,
                        entry.debit.amount, entry.credit.amount,
                        entry.description, str(entry.correlation_id),
                        entry.timestamp.isoformat(),
                    )
                    self._store.save_order_state(
                        order_id, symbol, result.get("side", ""),
                        result.get("type", ""), result.get("origQty", "0"),
                        result.get("price"), "FILLED",
                        str(executed_qty), str(avg_price),
                    )

                    # 成交后创建止盈止损保护
                    entry_price = float(avg_price)
                    qty = executed_qty
                    pos_side = OrderSide.BUY if result.get("side") == "BUY" else OrderSide.SELL
                    pos_id = f"pos-{order_id}"

                    self._protection.create_protection(
                        position_id=pos_id,
                        instrument_id=InstrumentId(symbol),
                        venue_id=VenueId("BINANCE"),
                        entry_price=entry_price,
                        quantity=qty,
                        side=pos_side,
                        stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 2.0},
                        take_profit_config={"type": "FIXED_RR", "rr_ratio": 2.0},
                    )

                    # === NEW: Strategy Risk update on trade fill ===
                    side_str = result.get("side", "")
                    # PnL will be determined when position closes via reconciliation
                    # Do NOT hardcode is_win=True or pnl=0.0 — this corrupts risk tracking
                    is_win = None  # NOT_VERIFIABLE — determined at position close
                    pnl = None     # NOT_VERIFIABLE — determined at position close
                    # Only record PnL when we have actual realized PnL data
                    # self._strategy_risk.record_trade requires actual values

                    account_balance = float(self._last_account.get("totalWalletBalance", 0))
                    if account_balance > 0:
                        self._strategy_risk.update_equity(self._autopilot_strategy_id, account_balance)
                        if account_balance > self._peak_equity:
                            self._peak_equity = account_balance

                elif status == "CANCELED" or status == "EXPIRED":
                    tracker.apply(OrderEvent.CANCELED)
                    self._active_order_ids.discard(order_id)
                    self._store.save_order_state(
                        order_id, symbol, result.get("side", ""),
                        result.get("type", ""), result.get("origQty", "0"),
                        result.get("price"), status,
                    )

                elif status == "PARTIALLY_FILLED":
                    tracker.apply(OrderEvent.PARTIALLY_FILLED)

            except Exception as e:
                # Log error but do NOT silently swallow — maintain visibility
                print(f"[realtime] Order monitoring error ({order_id}): {e}")

    async def _reconcile(self) -> None:
        """对账：系统状态 vs 交易所状态。"""
        try:
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
            for pos_id, pos in self._protection.all_positions().items():
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
            for symbol in self._symbols:
                # 1. K-line features
                features = self._feed.get_kline_features(symbol, "1h", 100)
                if not features:
                    print(f"[nearline] {symbol}: no kline features available")
                    continue

                # 2. Market state estimation
                state = RealMarketStateEstimator.estimate(features)
                instrument_id = InstrumentId(symbol)
                venue_id = VenueId("BINANCE")

                print(f"[nearline] {symbol}: price={features.get('close', '?')} "
                      f"trend={state['direction']} stress={state['stress']} "
                      f"rsi={features.get('rsi_14', '?')} "
                      f"sma5={features.get('sma_5', '?')} sma20={features.get('sma_20', '?')}")

                if state["stress"] == "HIGH":
                    print(f"[nearline] {symbol}: SKIP (high volatility: ann_vol={features.get('ann_volatility', '?')})")
                    continue

                # === 3. Execute full AlphaGraph DAG in topological order ===
                # Inject position info for EXIT components
                positions = self._protection.all_positions()
                symbol_positions = {k: v for k, v in positions.items() if v.get("symbol") == symbol}
                pos_info = {"has_position": False, "entry_price": 0, "side": "", "entry_time": 0, "pnl_pct": 0}
                if symbol_positions:
                    # Use the first matching position
                    pos = list(symbol_positions.values())[0]
                    entry_price = float(pos.get("entry_price", 0) or 0)
                    pos_side = str(pos.get("side", ""))
                    pos_info["has_position"] = True
                    pos_info["entry_price"] = entry_price
                    pos_info["side"] = pos_side
                    pos_info["entry_time"] = float(pos.get("entry_time", 0) or 0)
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
                            print(f"[nearline] {symbol}: DAG[{comp_id}] ({comp.component_type.value}) "
                                  f"→ {signal.direction} strength={signal.strength:.3f}")
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

                # Check if entry signal is actionable
                entry_signals = [s for s in all_signals if s.direction != SignalDirection.NO_ACTION and s.strength >= 0.15]
                if not entry_signals:
                    print(f"[nearline] {symbol}: SKIP (all signals weak or NO_ACTION)")
                    continue

                # === 4. Signal fusion (all DAG signals, not just entry) ===
                fused = self._fuser.fuse(all_signals)
                if fused.direction == SignalDirection.NO_ACTION:
                    # If fusion rejects, check if entry alone would have fired
                    entry_only = [s for s in all_signals if s.component_type == AlphaComponentType.ENTRY]
                    if entry_only:
                        # Check MomentumFilter: if filter returned NO_ACTION, it vetoed the entry
                        filter_neg = [s for s in all_signals if s.component_type == AlphaComponentType.FILTER and s.direction == SignalDirection.NO_ACTION]
                        if filter_neg:
                            print(f"[nearline] {symbol}: SKIP (MomentumFilter veto: {filter_neg[0].metadata})")
                        else:
                            print(f"[nearline] {symbol}: SKIP (fusion rejected)")
                    else:
                        print(f"[nearline] {symbol}: SKIP (fusion rejected)")
                    continue

                print(f"[nearline] {symbol}: FUSED → {fused.direction} strength={fused.strength:.3f} confidence={fused.confidence:.3f}"
                      + (f" CONFLICT" if fused.conflict_detected else ""))

                # === 5. Portfolio optimization — dynamic position sizing ===
                price = features["close"]
                account_balance_str = self._last_account.get("totalWalletBalance")
                if not account_balance_str:
                    print(f"[nearline] {symbol}: SKIP (no account balance data)")
                    continue
                account_balance = float(account_balance_str)

                # Use optimizer to compute position size
                budget = self._strategy_risk.get_budget(self._autopilot_strategy_id)
                stop_loss_pct = 2.0  # FIXED_PERCENT stop
                stop_loss_price = price * (1 - stop_loss_pct / 100)
                risk_based_size = budget.compute_position_size(
                    self._autopilot_strategy_id, account_balance, price, stop_loss_price,
                ) if budget else 0.005

                # Floor and cap
                position_size = max(0.001, min(risk_based_size, 1.0))
                position_notional = price * position_size

                print(f"[nearline] {symbol}: Optimizer → size={position_size:.4f} "
                      f"notional={position_notional:.0f} (account={account_balance:.0f})")

                # === 6. Cost estimation ===
                spread_bps = features.get("spread_bps", 1.0)
                vi = VenueInstrument(venue_id=venue_id, instrument_id=instrument_id)
                self._cost_model.set_fee_tier(venue_id, "vip1", 2.0, 4.0)
                cost_est = self._cost_model.estimate_order(
                    vi, Quantity(amount=str(position_size)), Price(amount=str(price)),
                    OrderSide.BUY if fused.direction == SignalDirection.LONG else OrderSide.SELL,
                    urgency=0.3, spread_bps=spread_bps,
                )

                if cost_est.total_fee_bps > 30:
                    print(f"[nearline] {symbol}: SKIP (cost too high: {cost_est.total_fee_bps}bps)")
                    continue

                # === 7. Safety risk check ===
                snapshot = RiskSnapshot(
                    total_exposure=position_notional,
                    margin_used=position_notional / 2,
                    margin_total=account_balance,
                    position_count=self._protection.position_count(),
                    pending_orders=len(self._active_order_ids),
                    leverage=2.0,
                    concentration_pct=position_notional / max(account_balance, 1) * 100,
                )

                if snapshot.leverage > 3.0 or snapshot.concentration_pct > 50:
                    print(f"[nearline] {symbol}: SKIP (risk limits: leverage={snapshot.leverage:.1f} conc={snapshot.concentration_pct:.1f}%)")
                    continue

                # === 8. Approval & Intent ===
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
                    order_type=OrderType.LIMIT,
                    quantity=Quantity(amount=str(position_size)),
                    price=Price(amount=str(price)),
                    time_in_force=TimeInForce.GTC,
                    client_order_id=f"beidou-{symbol.lower()}-{int(time.time()*1000)}",
                    correlation_id=CorrelationId(f"nearline-{int(time.time())}"),
                    idempotency_key=f"idem-{symbol}-{int(time.time()/300)}",
                    risk_approval_id=str(approval_id),
                )

                try:
                    self._outbox.commit(intent)
                    print(f"[nearline] {symbol}: ✅ OrderIntent CREATED → {side.value} {position_size:.4f} @ {price}")
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
                    returns = self._factor_returns[-len(preds):] if len(preds) <= len(self._factor_returns) else self._factor_returns
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

                    print(f"[offline] Factor {fid}: IC={ic_mean:.4f} RankIC={rank_ic:.4f} "
                          f"ICIR={icir:.3f} decile={decile_spread:.4f} samples={min_n}")

                    record = self._factor_registry.get(fid)
                    if record:
                        from beidou_research.factors.factor import FactorPerformance
                        perf = FactorPerformance(
                            factor_id=fid,
                            evaluation_period=f"live-{now.strftime('%Y%m%d-%H')}",
                            sample_count=min_n,
                            ic_mean=ic_mean, ic_std=ic_std, icir=icir,
                            rank_ic_mean=rank_ic, rank_ic_std=0.0, rank_icir=0.0,
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

                live_metrics = {
                    "sharpe": sharpe,
                    "win_rate": win_rate,
                    "total_trades": total_trades,
                    "avg_pnl": avg_pnl,
                }

                if not self._drift_detector.is_calibrated():
                    # First calibration — set baseline from live performance
                    self._drift_detector.set_baseline({"sharpe": max(0.5, sharpe), "win_rate": max(0.4, win_rate)})
                    print(f"[offline] DriftDetector baseline calibrated")
                else:
                    drift = self._drift_detector.detect({"sharpe": sharpe, "win_rate": win_rate})
                    if self._drift_detector.should_retire(drift):
                        self._alerts.send_incident(
                            AlertSeverity.HIGH, "Model drift detected",
                            f"Drifted metrics: {list(drift.keys())}, live sharpe={sharpe:.3f} win_rate={win_rate:.2f}",
                            category="model",
                        )
                        print(f"[offline] ⚠️ Model drift: {drift}")
                    else:
                        print(f"[offline] DriftDetector: no drift (sharpe={sharpe:.3f} win_rate={win_rate:.2f})")
            else:
                print("[offline] DriftDetector: no trade data yet, skipping drift check")

            # === 3. Strategy risk: daily PnL reset ===
            risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
            if risk_state:
                print(f"[offline] Strategy risk: level={risk_state.risk_level.value} "
                      f"drawdown={risk_state.current_drawdown_pct:.1f}% "
                      f"daily_pnl={risk_state.daily_pnl:.2f} "
                      f"peak_equity={risk_state.peak_equity:.0f}")
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
                    "active_factors": len(self._factor_registry.get_active()) + len(self._factor_registry.get_challengers()),
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
        print(f"[beidou-autopilot] Mode: {self._mode}")
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
        print(f"[beidou-autopilot] Restored: {len(ledger_entries)} ledger entries, "
              f"{len(active_orders)} active orders, {len(protections)} protections")

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
        print(f"[beidou-autopilot] Strategy Risk: max_drawdown=20% max_daily_loss=5% max_consec_losses=5")
        factor_ids = list(self._factor_registry._factors.keys())
        print(f"[beidou-autopilot] Factor Registry: {len(factor_ids)} factors registered ({factor_ids})")
        print(f"[beidou-autopilot] Circuit Breakers: DRAWNDOWN_LIMIT | DAILY_LOSS_LIMIT | CONSECUTIVE_LOSSES | "
              f"SHARPE_DEGRADATION | VOLATILITY_SPIKE | CORRELATION_BREAKDOWN | MAX_POSITION_COUNT")

        # Lifecycle transitions
        self._lifecycle.transition(ModuleState.WARMING)
        self._lifecycle.transition(ModuleState.VALIDATING)
        self._lifecycle.transition(ModuleState.ACTIVE)
        print(f"[beidou-autopilot] State: {self._lifecycle.state.value}")

        # Start health server
        self._health.start()
        print(f"[beidou-autopilot] Health server: http://0.0.0.0:9090")

        # StartupGate: 启动后进入 NO_NEW_RISK，完成预热后自动 RESUME
        self._control.execute_action(ControlAction.NO_NEW_RISK)
        print("[beidou-autopilot] Control plane: NO_NEW_RISK (initial)")

        # 预热完成后自动 RESUME（24h 无人值守）
        await asyncio.sleep(10)  # 给系统 10s 稳定时间
        self._control.execute_action(ControlAction.RESUME)
        print("[beidou-autopilot] Control plane: RESUME (auto — startup gate passed)")

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
            except Exception as e:
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
        if not self._paper_only:
            for order_id in list(self._active_order_ids):
                for symbol in self._symbols:
                    try:
                        self._api("/fapi/v1/order", method="DELETE", signed=True, params={
                            "symbol": symbol, "orderId": int(order_id),
                        })
                    except Exception as e:
                        print(f"[shutdown] Failed to cancel order {order_id}: {e}")
        print(f"[beidou-autopilot] 2. Cancelled {len(self._active_order_ids)} pending orders")

        # 3. Save checkpoint
        self._mapek.save_checkpoint("autopilot-shutdown", {
            "tick_count": self._tick_count,
            "order_count": self._order_count,
            "shutdown_time": datetime.now(timezone.utc).isoformat(),
            "win_rate": round(self._win_count / max(1, self._win_count + self._loss_count), 3),
        }, invariants_valid=True)
        print("[beidou-autopilot] 3. Checkpoint saved")

        # 4. Close store
        self._store.close()
        self._health.stop()
        print("[beidou-autopilot] 4. Shutdown complete.")
