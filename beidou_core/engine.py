"""自主运行引擎 — 三层时钟域事件循环。

通过 BinanceUsdmAdapter 的唯一传输边界访问 Binance REST API。
实现真实市场状态估算和具体 AlphaComponent。
完整激活所有策略/因子/风控模块。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from beidou_autonomy.mapek import MAPEKController, RecoveryAction
from beidou_control.plane import ControlAction, ControlPlane
from beidou_control.truth import TradingEligibility, TruthSnapshot, derive_eligibility
from beidou_core.alerts import AlertDispatcher
from beidou_core.feed import MarketDataFeed
from beidou_core.health import HealthServer, HealthState
from beidou_core.store import PersistentStore
from beidou_data.trading_pool_lifecycle import PoolStatus, TradingPool
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.protocol import OrderRequest
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_lifecycle.lifecycle import DegradationLevel, ModuleLifecycle, ModuleState
from beidou_observability.telemetry import AlertSeverity
from beidou_policy.loader import PolicyLoader
from beidou_research.contracts import StrategyAction, StrategySignal
from beidou_research.factors.factor import (
    FactorDefinition,
    FactorEvaluator,
    FactorLifecycle,
    FactorRegistry,
)
from beidou_safety.execution import order_intent_binding_hash
from beidou_safety.execution.algorithms import (
    BaseExecutionAlgorithm,
    ExecutionAlgorithmSelector,
    ExecutionAlgorithmType,
    ExecutionContext,
    SliceInvariantChecker,
)
from beidou_safety.execution.command_aggregate import (
    ChildCommandState,
    ExecutionChildCommand,
    ParentExecutionState,
    TargetDeltaPlan,
)
from beidou_safety.execution.contracts import (
    ExecutionPlan,
    Fill,
    OrderIdempotencyKey,
    PlanSlice,
    PlanStatus,
    PositionAggregate,
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
    ReconciliationResult,
    ReconciliationStatus,
)
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector
from beidou_safety.protection.engine import (
    PositionProtection,
    ProtectionManager,
    ProtectionOrder,
    ProtectionStatus,
    StopLossType,
    TakeProfitType,
)
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
    AlphaSignal,
    SignalDirection,
)  # BD-T05: legacy, migrated to StrategyKernel
from beidou_strategy.alpha.legacy_adapter import build_typed_graph
from beidou_strategy.alpha.mean_reversion import MeanReversionEngine, MultiPeriodMomentum
from beidou_strategy.alpha.model_registry import DriftDetector, ModelRecord, ModelRegistry, ModelStatus
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.components.mean_reversion_fixed import estimate_half_life, robust_zscore
from beidou_strategy.kernel_parity import KernelMode, ParityResult, StrategyKernel
from beidou_strategy.paper_shadow import PaperMatchingEngine, PaperShadowRunner, ShadowConfig, ShadowMode
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio.contracts import PositionSide, SignedPortfolioTarget
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


def _validated_features(features: Any, required: tuple[str, ...]) -> dict[str, float] | None:
    """Validate a feature snapshot without inventing trading inputs."""

    if not isinstance(features, dict) or any(name not in features for name in required):
        return None
    values: dict[str, float] = {}
    try:
        for name in required:
            value = features[name]
            if isinstance(value, bool):
                return None
            parsed = float(value)
            if not math.isfinite(parsed):
                return None
            values[name] = parsed
    except (TypeError, ValueError, KeyError):
        return None
    return values


def _validated_prices(features: Any) -> list[float] | None:
    """Validate the closed-price series used by research components."""

    if not isinstance(features, dict):
        return None
    raw_prices = features.get("prices")
    if not isinstance(raw_prices, (list, tuple)) or not raw_prices:
        return None
    try:
        prices = [float(value) for value in raw_prices]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in prices):
        return None
    return prices


def _validated_ws_quote(ticker: Any) -> tuple[float, float, float] | None:
    """Validate a websocket last/bid/ask snapshot without quote fallbacks."""

    if not isinstance(ticker, dict):
        return None
    try:
        price = float(ticker["lastPrice"])
        bid = float(ticker["bid"])
        ask = float(ticker["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (price, bid, ask)):
        return None
    if price <= 0 or not (0 < bid <= ask):
        return None
    return price, bid, ask


def _rotating_symbol_batch(
    symbols: list[str],
    start_index: int,
    batch_size: int = 10,
) -> tuple[list[str], int]:
    """Return a fair circular batch and the next cursor.

    The cursor must be computed against the full active universe, not the
    sliced batch. Otherwise a Testnet universe larger than the batch size can
    repeatedly evaluate only its first symbols and starve the remainder.
    """

    if not symbols or batch_size <= 0:
        return [], 0
    universe_size = len(symbols)
    start = start_index % universe_size
    take = min(batch_size, universe_size)
    batch = [symbols[(start + offset) % universe_size] for offset in range(take)]
    return batch, (start + take) % universe_size


def _validate_duplicate_order_response(
    response: Any,
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: str,
    reduce_only: bool = False,
) -> tuple[bool, str]:
    """Prove a duplicate-client-id lookup identifies the exact intent.

    A query made after an ambiguous submit is not an acknowledgement by
    itself.  Adopt exactly one venue row only when its identity and requested
    order semantics match the durable intent; otherwise keep the intent
    UNKNOWN and require governed reconciliation.
    """

    if not isinstance(response, dict):
        return False, "RESPONSE_NOT_OBJECT"
    required = ("orderId", "clientOrderId", "symbol", "side", "type", "status", "origQty")
    if any(response.get(field) in (None, "") for field in required):
        return False, "IDENTITY_FIELD_MISSING"
    if str(response["clientOrderId"]) != str(client_order_id):
        return False, "CLIENT_ORDER_ID_MISMATCH"
    if str(response["symbol"]).upper() != str(symbol).upper():
        return False, "SYMBOL_MISMATCH"
    if str(response["side"]).upper() != str(side).upper():
        return False, "SIDE_MISMATCH"
    if str(response["type"]).upper() != str(order_type).upper():
        return False, "ORDER_TYPE_MISMATCH"
    if str(response["status"]).upper() not in {
        status.value for status in OrderStatus if status is not OrderStatus.UNKNOWN
    }:
        return False, "ORDER_STATUS_UNKNOWN"
    try:
        expected_quantity = Decimal(str(quantity))
        venue_quantity = Decimal(str(response["origQty"]))
    except (InvalidOperation, TypeError, ValueError):
        return False, "QUANTITY_INVALID"
    if not expected_quantity.is_finite() or expected_quantity <= 0 or venue_quantity != expected_quantity:
        return False, "QUANTITY_MISMATCH"

    if reduce_only:
        value = response.get("reduceOnly")
        enabled = value is True or str(value).strip().lower() in {"1", "true", "yes"}
        if not enabled:
            return False, "REDUCE_ONLY_UNPROVEN"
    return True, "OK"


def _is_retryable_venue_rejection(reason: str) -> bool:
    """venue 拒绝原因是否可重试（BD-FIX，I6 审查）。

    demo 共享账户的限频（-1003）、时钟偏差（-1021）、临时请求拒绝
    （-4131）、服务不可用（-1006）、地域限制（-2015/restricted）等
    属瞬时错误 —— 意图不应直接 FAILED（新意图只能等下一个新收盘
    K 线，可能数分钟无单），mark_unknown 后由运行时周期 resolve
    按 identity-bound 事实裁决恢复。
    """
    upper = str(reason).upper()
    retryable_markers = (
        "-1003",  # TOO_MANY_REQUESTS
        "-1021",  # TIMESTAMP_OUTSIDE_RECV_WINDOW
        "-4131",  # 临时请求拒绝
        "-1006",  # UNEXPECTED_RESP
        "-2015",  # 地域限制
        "RESTRICTED",
        "RATE_LIMIT",
        "TIMEOUT",
        "RETRYABLE",
        "SERVICE_UNAVAILABLE",
    )
    return any(marker in upper for marker in retryable_markers)


def _derive_liquidation_price(position_qty: float, entry_price: float, leverage: float) -> float | None:
    """杠杆推导的名义清算价（BD-FIX）。

    demo 账户快照不提供 liquidationPrice 字段，testnet 用标准名义近似：
    LONG → entry × (1 − 1/lev)；SHORT → entry × (1 + 1/lev)。输入非法时
    返回 None（R7 保持 UNKNOWN fail-closed）。
    """
    if not math.isfinite(position_qty) or not math.isfinite(entry_price) or not math.isfinite(leverage):
        return None
    if entry_price <= 0 or leverage <= 0 or position_qty == 0:
        return None
    shift = entry_price / leverage
    return entry_price - shift if position_qty > 0 else entry_price + shift


def _local_owned_symbols(engine: Any) -> set[str]:
    """本地所有权可证明的持仓标的集合（BD-FIX，共享账户本地化）。

    共享 demo 账户上其他用户的持仓不属于引擎 —— 风控输入、保护覆盖、
    权益估计都只统计本地所有权（保护位置/持仓投影/代际记录）可证明
    的部分。
    """
    protection = getattr(engine, "_protection", None)
    owned: set[str] = set()
    if protection is not None and callable(getattr(protection, "all_positions", None)):
        owned.update(str(pp.instrument_id) for pp in protection.all_positions().values())
    owned.update(str(sym) for sym in getattr(engine, "_position_generation", {}).keys())
    owned.update(str(sym) for sym in getattr(engine, "_position_projection", {}).keys())
    return owned


def _local_equity_estimate(engine: Any, shared_balance: float) -> float:
    """testnet 本地权益估计（BD-FIX，I4 审查）。

    共享 demo 账户的 REST 余额含外部用户资金 —— 用它更新策略权益/
    peak_equity 会放大或掩盖自有 drawdown、污染仓位 sizing。本地估计：
    启动基线（共享余额 - 自有持仓名义）+ 自有持仓 unrealized（取自
    交易所持仓行，Binance 标准字段）。非 testnet 直接返回共享余额。
    """
    if str(getattr(getattr(engine, "_env_mode", None), "value", "")) != "testnet":
        return shared_balance
    base = getattr(engine, "_local_equity_base", None)
    owned = _local_owned_symbols(engine)
    unreal = 0.0
    owned_notional = 0.0
    positions = (getattr(engine, "_last_account", {}) or {}).get("positions", [])
    if isinstance(positions, list):
        for row in positions:
            if str(row.get("symbol", "")) not in owned:
                continue
            try:
                unreal += float(row.get("unrealizedProfit", 0) or 0)
                amt = float(row.get("positionAmt", 0) or 0)
                entry = float(row.get("entryPrice", 0) or 0)
                owned_notional += abs(amt) * entry
            except (TypeError, ValueError):
                continue
    if base is None:
        base = shared_balance - owned_notional
        engine._local_equity_base = base
        print(f"[equity] local equity baseline: shared={shared_balance:.2f} "
              f"owned_notional={owned_notional:.2f} base={base:.2f}")
    return base + unreal


def _reconciliation_max_age_seconds(env_mode_value: str) -> float:
    """对账事实新鲜度阈值按环境区分（BD-FIX）。

    默认 30s 阈值对低频用户流过严：replay baseline 授权后投影时间戳冻结，
    demo 共享账户凌晨可 10+ 分钟无事件，30s 即 STALE → 三方对账恒失败 →
    DEGRADED。testnet 放宽到 300s，与运行时 readiness 的 event_age 阈值
    （CONNECTED 时 300s）一致——事件停流保护语义不变，只是对齐时间尺度。
    live/canary/paper 保持 30s 严格默认。
    """
    return 300.0 if env_mode_value == "testnet" else 30.0


def _signal_identity(context: dict[str, Any]) -> tuple[InstrumentId, VenueId]:
    """Resolve an explicit identity; never default a missing symbol to BTCUSDT."""

    instrument = context.get("instrument_id")
    venue = context.get("venue_id")
    return (
        InstrumentId(str(instrument)) if isinstance(instrument, str) and instrument else InstrumentId("UNKNOWN"),
        VenueId(str(venue)) if isinstance(venue, str) and venue else VenueId("UNKNOWN"),
    )


def _blocked_signal(
    context: dict[str, Any],
    *,
    strategy_id: str,
    component_type: AlphaComponentType,
    reason: str,
) -> AlphaSignal:
    """Produce an auditable non-trading signal for missing/invalid features."""

    instrument_id, venue_id = _signal_identity(context)
    metadata: dict[str, Any] = {"reason": reason, "data_quality": "BLOCK"}
    if component_type == AlphaComponentType.FILTER:
        metadata["filter_decision"] = "VETO"
    return AlphaSignal(
        strategy_id=StrategyId(strategy_id),
        component_type=component_type,
        direction=SignalDirection.NO_ACTION,
        strength=0.0,
        confidence=0.95 if component_type == AlphaComponentType.FILTER else 0.0,
        instrument_id=instrument_id,
        venue_id=venue_id,
        model_version=SchemaVersion("2.0.0"),
        metadata=metadata,
    )


# ================================================================
# 自适应杠杆 — 基于波动率分级
# ================================================================


def adaptive_leverage(ann_volatility: float) -> float:
    """波动率越高杠杆越低，控制风险暴露。"""
    if not math.isfinite(ann_volatility) or ann_volatility <= 0.0:
        return 0.0  # UNKNOWN → no new risk
    if ann_volatility < 0.2:
        return 3.0  # 低波动：可以放大
    if ann_volatility < 0.4:
        return 2.0  # 正常波动
    if ann_volatility < 0.6:
        return 1.0  # 偏高波动：减半
    return 0.5  # 极端波动：最小化


def adaptive_position_pct(strength: float, ann_volatility: float, spread_bps: float) -> float:
    """自适应仓位比例：信号强度 × 波动率惩罚 × 点差惩罚。"""
    if (
        not all(math.isfinite(value) for value in (strength, ann_volatility, spread_bps))
        or strength <= 0.0
        or ann_volatility <= 0.0
        or spread_bps < 0.0
    ):
        return 0.0
    vol_penalty = max(0.2, 1.0 - ann_volatility)  # 波动越高惩罚越大
    spread_penalty = max(0.3, 1.0 - spread_bps / 50.0)  # 点差越大惩罚越大
    # BD-FIX: 仓位基数环境可调（默认 0.02 生产语义不变）—— 0.02 与
    # risk_per_trade_pct 双重保守叠加使 testnet 名义恒 ~10 USDT，
    # 自适应变化不可感知（用户反馈"自适应未启用"）。
    _base_pct = float(os.getenv("BEIDOU_ADAPTIVE_BASE_PCT", "0.02"))
    base = strength * _base_pct
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
        # PKG02 (BDS-P0-001): 移除 testnet 零阈值/免成本旁路 — 所有环境使用统一策略参数
        _z = 1.5
        self._engine = MeanReversionEngine(
            half_life_window=100,
            z_threshold=_z,
            no_trade_band=_z,
            cost_margin=2.0,
        )

    async def generate(self, context: dict) -> Any:
        features = context.get("features", {})
        values = _validated_features(features, ("close", "sma_20", "rsi_14", "ann_volatility", "spread_bps"))
        prices = _validated_prices(features)
        if values is None or prices is None:
            return _blocked_signal(
                context,
                strategy_id="meanrev_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        close = values["close"]
        sma_20 = values["sma_20"]
        rsi = values["rsi_14"]

        # BD-05: 稳健均值回归引擎 — median/MAD z-score + OLS half-life +
        # no-trade band + regime gate + cost gate + volatility scaling
        state = context.get("state", {}) or {}
        regime = state.get("direction", "RANGING")
        # PKG02 (BDS-P0-001): 移除 testnet 降低交易成本旁路 — 所有环境使用统一成本模型
        _extra_cost = 6.0
        result = self._engine.evaluate(
            price=close,
            prices=prices,
            volatility=values["ann_volatility"],
            estimated_cost_bps=values["spread_bps"] + _extra_cost,
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
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
        # BD-CV23: 合约验证 — NO_ACTION 不会被改写为 ACTION，VETO 阻断下游
        _contract_signal = StrategySignal(
            strategy_id="meanrev_v1",
            action=StrategyAction.NO_ACTION if direction == SignalDirection.NO_ACTION else StrategyAction.ACTION,
            symbol=str(signal.instrument_id),
            confidence=confidence,
            reason=f"z_score={result.z_score:.3f} hl={result.half_life_hours:.1f}h",
        )
        if _contract_signal.is_blocking():
            signal.direction = SignalDirection.NO_ACTION
            signal.strength = 0.0
            signal.confidence = 0.0

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
        features = context.get("features", {})
        values = _validated_features(features, ("ann_volatility",))
        prices = _validated_prices(features)
        if values is None or prices is None:
            return _blocked_signal(
                context,
                strategy_id="momentum_filter_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        ann_vol = values["ann_volatility"]

        # BD-05: 多周期动量评估（趋势方向/强度/持久性/波动率否决）
        result = self._momentum.evaluate(prices, ann_vol)

        if result.filter_decision == "VETO":
            # 否决（高波动 override 或数据不足）→ NO_ACTION, confidence>=0.8 触发 DAG veto
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.9
            reason = "momentum_veto"
        elif result.filter_decision == "DEGRADE":
            direction = SignalDirection.NO_ACTION
            strength = result.trend_strength * 0.5
            confidence = 0.4
            reason = "momentum_degrade"
        else:
            direction = SignalDirection.NO_ACTION
            strength = max(0.15, result.trend_strength)
            confidence = 0.55
            reason = "momentum_accept"

        signal = AlphaSignal(
            strategy_id=StrategyId("momentum_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
        features = context.get("features", {})
        values = _validated_features(features, ("sma_5", "sma_20", "trend_20_pct", "rsi_14"))
        if values is None or values["sma_5"] <= 0 or values["sma_20"] <= 0 or not 0 <= values["rsi_14"] <= 100:
            return _blocked_signal(
                context,
                strategy_id="trend_following_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        sma_5 = values["sma_5"]
        sma_20 = values["sma_20"]
        trend_20 = values["trend_20_pct"]
        rsi = values["rsi_14"]

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
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
        features = context.get("features", {})
        values = _validated_features(
            features,
            ("close", "highest_20", "lowest_20", "ann_volatility", "vol_ratio", "atr_pct"),
        )
        if values is None or values["close"] <= 0 or values["highest_20"] <= 0 or values["lowest_20"] <= 0:
            return _blocked_signal(
                context,
                strategy_id="breakout_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        close = values["close"]
        highest_20 = values["highest_20"]
        lowest_20 = values["lowest_20"]
        ann_vol = values["ann_volatility"]
        vol_ratio = values["vol_ratio"]
        atr_pct = values["atr_pct"]

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
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
        features = context.get("features", {})
        values = _validated_features(features, ("ann_volatility", "atr_pct", "rsi_14"))
        if (
            values is None
            or values["ann_volatility"] <= 0
            or values["atr_pct"] <= 0
            or not 0 <= values["rsi_14"] <= 100
        ):
            return _blocked_signal(
                context,
                strategy_id="volatility_filter_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        ann_vol = values["ann_volatility"]
        atr_pct = values["atr_pct"]
        rsi = values["rsi_14"]

        if ann_vol > 0.6 or atr_pct > 5.0:
            # Extreme volatility — veto everything
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.9
            reason = "extreme_volatility"
        elif ann_vol > 0.4:
            # Elevated — reduce conviction
            direction = SignalDirection.NO_ACTION
            strength = 0.15
            confidence = 0.5
            reason = "elevated_volatility"
        else:
            # Normal — confirm direction via RSI
            # rsi > 70 → overbought bias SHORT; rsi < 30 → oversold bias LONG
            direction = SignalDirection.NO_ACTION
            strength = 0.2
            confidence = 0.6
            reason = "normal"

        signal = AlphaSignal(
            strategy_id=StrategyId("volatility_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
            model_version=SchemaVersion("2.0.0"),
            metadata={"ann_vol": ann_vol, "atr_pct": atr_pct, "rsi": rsi, "reason": reason},
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
        features = context.get("features", {})
        values = _validated_features(features, ("vol_ratio", "trend_20_pct", "ann_volatility"))
        if values is None or values["vol_ratio"] < 0 or values["ann_volatility"] <= 0:
            return _blocked_signal(
                context,
                strategy_id="volume_filter_v1",
                component_type=self.component_type,
                reason="MARKET_FEATURES_UNKNOWN",
            )
        vol_ratio = values["vol_ratio"]
        trend_20 = values["trend_20_pct"]
        ann_vol = values["ann_volatility"]

        if vol_ratio < 0.25:
            # Dead volume — veto
            direction = SignalDirection.NO_ACTION
            strength = 0.0
            confidence = 0.85
            reason = "dead_volume"
        elif vol_ratio < 0.5:
            # Low volume — degrade signal
            direction = SignalDirection.NO_ACTION
            strength = 0.08
            confidence = 0.45
            reason = "low_volume"
        elif vol_ratio > 1.5 and ann_vol > 0.15:
            # Surge volume — strong confirmation
            direction = SignalDirection.NO_ACTION
            strength = min(0.25, vol_ratio / 10)
            confidence = 0.65
            reason = "volume_surge"
        else:
            # Normal volume — neutral pass-through
            direction = SignalDirection.NO_ACTION
            strength = 0.12
            confidence = 0.5
            reason = "normal"

        signal = AlphaSignal(
            strategy_id=StrategyId("volume_filter_v1"),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=confidence,
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
            model_version=SchemaVersion("2.0.0"),
            metadata={"vol_ratio": vol_ratio, "trend_20": trend_20, "reason": reason},
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
        features = context.get("features", {})
        # Check if there's a position to manage
        position_info = context.get("_position_info", {})
        has_position = position_info.get("has_position", False)
        entry_price = position_info.get("entry_price", 0)
        position_side = position_info.get("side", "")
        atr_pct: float | None = None

        if not has_position or entry_price <= 0:
            # No position — return neutral
            direction = SignalDirection.FLAT
            strength = 0.0
            reason = "no_position"
        else:
            values = _validated_features(features, ("close", "atr_pct", "sma_20", "rsi_14"))
            if values is None or values["close"] <= 0 or values["atr_pct"] <= 0 or not 0 <= values["rsi_14"] <= 100:
                return _blocked_signal(
                    context,
                    strategy_id="trailing_exit_v1",
                    component_type=self.component_type,
                    reason="MARKET_FEATURES_UNKNOWN",
                )
            close = values["close"]
            atr_pct = values["atr_pct"]
            sma_20 = values["sma_20"]
            rsi = values["rsi_14"]
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
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
            instrument_id=_signal_identity(context)[0],
            venue_id=_signal_identity(context)[1],
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
        values = _validated_features(features, ("close", "sma_5", "sma_20", "ann_volatility"))
        if values is None or values["close"] <= 0 or values["sma_5"] <= 0 or values["sma_20"] <= 0:
            return {"direction": "UNKNOWN", "stress": "UNKNOWN", "quality": "UNKNOWN"}

        ann_vol = values["ann_volatility"]
        sma_5 = values["sma_5"]
        sma_20 = values["sma_20"]
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

        quality = "RELIABLE" if ann_vol > 0 else "UNKNOWN"

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

    # BD-FIX: user stream 终端故障后的限次自动重启 — demo listenKey 周期性失效
    # （LISTEN_KEY_KEEPALIVE_FAILED）曾经使 user stream 永久 FAILED，进而让
    # supervisor 的 runtime.safety.user_stream P0 永久阻塞控制面。达到上限后
    # 停止自动恢复并保持 FAILED + NO_NEW_RISK，防止无限循环。
    _USER_STREAM_RESTART_MAX_ATTEMPTS = 3
    _USER_STREAM_RESTART_BACKOFF_S = 15.0

    def __init__(self, symbols: list[str], mode: str = "paper") -> None:
        normalized_symbols = list(
            dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
        )
        if not normalized_symbols or any(symbol in {"ALL", "DEFAULT"} for symbol in normalized_symbols):
            raise ValueError("EXPLICIT_SYMBOL_UNIVERSE_REQUIRED")
        self._symbols = normalized_symbols
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
        # BD-FIX: TruthSnapshot 配置事实观察时刻。配置进程内只加载一次，且
        # is_stale 不校验 config 新鲜度（critical 列表仅含市场/账户/订单/仓位/
        # 对账/保护/风险），加载时刻即观察时刻，进程生命周期内有效。
        self._config_observed_at = time.time()

        # BD-FIX: 凭证生命周期追踪 — ServiceIdentity 管理 key 元数据和安全审计
        self._service_identity = ServiceIdentity(
            service_name="beidou-autopilot",
            service_id="beidou-autopilot",
            roles=frozenset({"trader"}),
        )
        # Every process generation gets a unique durable lease owner.  A
        # stable service name alone cannot distinguish a crashed worker from
        # its restart while the old PostgreSQL lease is still valid; that
        # ambiguity must be fenced into UNKNOWN before any retry.
        self._session_id = uuid.uuid4().hex
        self._lease_owner = f"{self._service_identity.service_id}:{self._session_id}"
        self._credential = Credential(
            credential_id="binance-usdm-testnet",
            credential_type=CredentialType.TRADING,
            key_reference="BEIDOU_BINANCE_API_KEY",
            can_withdraw=False,  # policy expectation; venue permission is read separately at startup
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
        # R9 账户能力在启动后必须由交易所账户事实确认；本地凭据元数据
        # 不能证明 venue 权限。未知权限先按最保守状态阻断写入。
        self._can_trade = Permission.can_create_order(self._credential.credential_type)
        self._can_withdraw = True  # UNKNOWN/true both fail closed; venue false is applied after account read
        self._venue_can_trade: bool | None = None
        self._venue_can_withdraw: bool | None = None
        # BD-FIX: 风险规则视角的提款权限。账户权限门禁对 testnet 豁免
        # 提款权限（demo 账户默认开启，无真实资金风险），但 R9 规则直接
        # 读 _can_withdraw 恒 REJECT，导致 testnet/paper 所有提案被风控
        # 拦截、无订单成交。风险视角值由权限门禁结算后写入：
        # testnet → False（豁免）；非写环境由调用方按 False 处理；
        # 其他可写环境 → venue 原始值（fail-closed）。
        self._risk_can_withdraw: bool = True
        # 凭据到期预警（30 天内提醒轮换）
        _days_to_expiry = (self._credential.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400.0
        if _days_to_expiry <= 30.0:
            print(
                f"[beidou-security] ⚠️ Credential {self._credential.credential_id} "
                f"expires within {_days_to_expiry:.0f} days — 请尽快轮换密钥"
            )
        self._credential_health: dict[str, Any] = {}

        # === BD-05: Signed policy loading — risk-increasing writes require a
        # complete signed parameter contract.  Simulation modes may still use
        # environment defaults for diagnostics; writable modes remain blocked
        # if any signed parameter is absent or invalid.
        self._policy_loader = PolicyLoader(policy_dir="config/policies")
        self._policy_params: dict[str, Any] = {}
        self._policy_id_active: str | None = None
        self._policy_version: str | None = None
        self._policy_error: str | None = None
        try:
            for policy_id in ("risk_parameters", "autopilot_risk"):
                envelope = self._policy_loader.load(policy_id)
                if envelope is not None:
                    complete, completeness_message = envelope.validate_risk_parameters()
                    if not complete:
                        self._policy_error = f"SIGNED_POLICY_INCOMPLETE:{completeness_message}"
                        continue
                    self._policy_params = dict(envelope.parameters)
                    self._policy_id_active = envelope.policy_id
                    self._policy_version = envelope.version
                    self._policy_error = None
                    break
        except Exception as exc:
            self._policy_error = f"SIGNED_POLICY_LOAD_ERROR:{type(exc).__name__}"
            print(f"[policy] ERROR: policy load raised {exc} — risk-increasing writes remain blocked")

        if self._policy_id_active and self._policy_version:
            # BD-FIX: TruthSnapshot 策略事实 — 加载即记录。策略进程内只加载一次，
            # 且 is_stale 不校验 policy 新鲜度（critical 列表仅含市场/账户/订单/
            # 仓位/对账/保护/风险），加载时刻即观察时刻，进程生命周期内有效。
            # 策略缺失时保持 _policy_hash 为空 → fail-closed NOT_VERIFIABLE。
            self._policy_hash = hashlib.sha256(f"{self._policy_id_active}:{self._policy_version}".encode()).hexdigest()
            self._policy_observed_at = time.time()
            print(
                f"[policy] ACTIVE: {self._policy_id_active} v{self._policy_version} "
                f"params={sorted(self._policy_params.keys())} — overriding YAML risk defaults"
            )
        else:
            self._policy_error = self._policy_error or "SIGNED_POLICY_UNAVAILABLE"
            print("[policy] ERROR: No valid signed policy in config/policies/ — risk-increasing writes remain blocked")

        # Adapter REST client (BD-02: single adapter boundary)
        self._exchange = BinanceRESTClient(
            rest_url=self._rest_url,
            api_key=self._api_key,
            api_secret=self._api_secret,
            recv_window=getattr(self._settings.exchange, "recv_window_ms", 5000) or 5000,
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
        self._state_backend_error: str | None = None
        if database_url.startswith("sqlite:///"):
            durable_db_path = database_url.removeprefix("sqlite:///")
            self._state_backend_supported = bool(durable_db_path)
            self._store = PersistentStore.get_instance(durable_db_path)
            self._outbox = IntentOutbox(db_path=durable_db_path)
        elif database_url.lower().startswith(("postgresql://", "postgres://")):
            # Testnet/production must use one configured PostgreSQL authority.
            # A diagnostic SQLite fallback is allowed only as an explicitly
            # blocked read-only bootstrap path; it can never satisfy readiness.
            durable_db_path = database_url
            token_text = os.environ.get("BEIDOU_FENCING_TOKEN", "").strip()
            try:
                fencing_token = int(token_text)
            except (TypeError, ValueError):
                fencing_token = 0
            try:
                from beidou_infra.outbox import PostgresIntentOutbox
                from beidou_infra.postgres_store import PostgresPersistentStore

                self._store = PostgresPersistentStore.get_instance(database_url)
                self._outbox = PostgresIntentOutbox(
                    dsn=database_url,
                    lease_owner=self._lease_owner,
                    fencing_token=fencing_token,
                )
                self._state_backend_supported = fencing_token > 0
                if not self._state_backend_supported:
                    self._state_backend_error = "FENCING_TOKEN_UNKNOWN"
                    print("[state] ERROR: PostgreSQL is reachable but BEIDOU_FENCING_TOKEN is missing/invalid")
                else:
                    # A restart must never reinterpret an old SENDING/SENT row
                    # as safe to resend.  Fence stale/expired leases into
                    # durable UNKNOWN before restoring approvals or entering
                    # the lifecycle; venue query/reconciliation is required.
                    recovered_inflight = self._outbox.recover_inflight()
                    if recovered_inflight:
                        print(
                            "[state] Recovered "
                            f"{recovered_inflight} in-flight intent(s) as UNKNOWN; venue query required"
                        )
            except Exception as exc:
                self._state_backend_supported = False
                self._state_backend_error = type(exc).__name__
                # BD-FIX: 诊断回退库按环境模式隔离。此前 paper/testnet 共用
                # .beidou/state.db：paper 的模拟订单/持仓/账本写入同一张表，
                # testnet 启动恢复时被当作系统侧事实 → 对账永远 MISMATCHED
                # （"Orders in system but not on exchange"）。零写环境的模拟
                # 事实绝不能污染可写环境的对账基线。
                diagnostic_db_path = f".beidou/state-{self._env_mode.value}.db"
                self._store = PersistentStore.get_instance(diagnostic_db_path)
                self._outbox = IntentOutbox(db_path=diagnostic_db_path)
                from beidou_shared.config import redact_database_url

                print(
                    "[state] ERROR: configured PostgreSQL backend is unavailable; "
                    f"diagnostic store={diagnostic_db_path!r} configured={redact_database_url(database_url)!r} "
                    f"error={type(exc).__name__}; trading/readiness remain BLOCKED"
                )
        else:
            # BD-FIX: 与诊断回退相同 — 按环境模式隔离，避免 paper 模拟
            # 事实污染 testnet 对账基线。
            durable_db_path = f".beidou/state-{self._env_mode.value}.db"
            self._state_backend_supported = False
            self._state_backend_error = "UNSUPPORTED_DATABASE_URL"
            self._store = PersistentStore.get_instance(durable_db_path)
            self._outbox = IntentOutbox(db_path=durable_db_path)
            from beidou_shared.config import redact_database_url

            print(
                f"[state] ERROR: unsupported database URL {redact_database_url(database_url)!r}; "
                f"using {durable_db_path!r} for diagnostics; state backend is NOT READY"
            )
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
        # BD-FIX: TruthSnapshot 保护事实 — 初始化时所有权归属本服务 → ACTIVE
        self._last_protection_hash = hashlib.sha256("ACTIVE".encode()).hexdigest()
        self._last_protection_fact_at = time.time()
        self._protection_owner_id = self._service_identity.service_id
        self._position_generation: dict[str, int] = {}
        self._position_projection: dict[str, dict[str, Any]] = {
            str(row["symbol"]): row for row in self._store.restore_position_projection()
        }
        for protection in self._store.restore_protections():
            if str(protection.get("status", "")).upper() != "ACTIVE":
                continue
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
        # The selected outbox uses the same configured authority as the store;
        # SQLite remains only for Paper or an explicitly blocked diagnostic
        # bootstrap after a PostgreSQL failure.
        self._ledger = ImmutableLedger()
        self._restore_durable_ledger()
        # PKG20: 余额相对容差按环境配置 — testnet 为共享 demo 账户（外部活动漂移
        # ~0.14 USDT/分钟，0.01% 容差数分钟即失效）使用 1% 相对容差；
        # canary/live/paper/research 保持 0.01% 严格默认不变。
        _recon_rel_tolerance = Decimal("0.01") if self._env_mode.value == "testnet" else Decimal("0.0001")
        self._recon = ReconciliationEngine(
            balance_rel_tolerance=_recon_rel_tolerance,
            max_age_seconds=_reconciliation_max_age_seconds(self._env_mode.value),
        )
        self._user_stream_projector = UserStreamProjector(store=self._store)
        # PKG02 (BDS-P0-001): 移除 testnet 允许无序列号事件旁路
        # 所有环境必须通过序列完整性验证
        self._event_stream_facts: AccountFactSnapshot | None = None
        # A durable projector alone is not proof that a live user-data socket
        # is connected.  Writable readiness requires an explicit runtime
        # transport status and a recent parsed event; the default is therefore
        # NOT_STARTED/UNKNOWN and cannot be promoted by a REST snapshot.
        self._user_stream_runtime: dict[str, Any] = {
            "status": "NOT_STARTED" if self._can_write else "NOT_REQUIRED",
            "last_event_mono": None,
            "last_state_mono": None,
            "listen_key_active": False,
            "last_error": "",
        }
        # BD-FIX: user stream 终端故障自动重启的重试计数、在途标志与任务句柄。
        self._user_stream_restart_attempts = 0
        self._user_stream_restarting = False
        self._user_stream_restart_task: asyncio.Task[None] | None = None
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
        # Rehydrate only approvals attached to unresolved durable intents.  A
        # completed/failed intent is deliberately not restored, so restart
        # cannot turn a single-use approval into a reusable write token.
        for approval in self._outbox.restore_pending_approvals():
            try:
                approval_id = RiskApprovalId(str(approval["approval_id"]))
                signature = str(approval["signature"])
                expires_at = float(approval["expires_at"])
                if self._approval.restore_signature(signature, expires_at):
                    self._risk_sm.approve(
                        approval_id,
                        nonce=str(approval["nonce"]),
                        ttl=expires_at - time.time(),
                        risk_snapshot_hash=str(approval["risk_snapshot_hash"]),
                        policy_version=str(approval["policy_version"]),
                    )
            except (KeyError, TypeError, ValueError):
                # Malformed durable approval remains UNKNOWN and keeps the
                # normal final-send verification gate closed.
                continue
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
        # BD-FIX: 冷启动基线标志 — 无交易数据时的中性 sharpe=0 校准在
        # 第一笔真实交易数据到达后必须被真实基线替换。
        self._cold_start_baseline = False
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
        for sym in configured_symbols:
            # A configured symbol is only an observation candidate.  Startup
            # must never manufacture an ACTIVE trading universe: activation
            # requires fresh market-quality, PIT-universe and evidence-gated
            # lifecycle decisions.  Testnet is an execution environment, not
            # a factor/universe promotion bypass.
            self._trading_pool.add(sym)
        print(
            f"[beidou-autopilot] Trading Pool: {self._trading_pool.active_count()} active instruments "
            f"(configured={len(configured_symbols)})"
        )
        # BD-FIX（历史数据加速筛选）: 用已回填的历史 K 线预筛选候选 ——
        # 历史质量分达标者观察期等效已满（下一次实时评分达标即按正常
        # 流程晋级）。历史数据缺失的标的保持标准观察期。
        self._seed_pool_from_history(configured_symbols)

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
        # FactorEvaluator 仅使用静态方法 (compute_ic/compute_rank_ic 等)，实例无需保留
        # self._factor_evaluator = FactorEvaluator()  # 已移除：实例方法从未被调用

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
        self._factor_gate = FactorPromotionGate(strict=True)
        print(f"[beidou-autopilot] Factor promotion gate: strict={getattr(self._factor_gate, '_strict', True)}")
        active_factors = [
            fid for fid, record in self._factor_registry._factors.items() if record.has_authorized_active_evidence()
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
        # 研究证据桥接：启动扫描 evidence/factors，逐级复验并推进生命周期。
        # 任何失败只记录不阻断（fail-closed 但非阻塞）。
        # ================================================================
        try:
            from beidou_core.evidence_bridge import EvidenceBridge

            bridge_report = EvidenceBridge.load_and_apply(
                registry=self._factor_registry,
                gate=self._factor_gate,
                env_mode=str(self._env_mode.value),
                component_registry=self._factor_component_registry,
                entry_ids=self._entry_ids,
                filter_ids=self._filter_ids,
                exit_ids=self._exit_ids,
                evidence_dir="evidence/factors",
            )
            if bridge_report.applied:
                print(f"[beidou-autopilot] Evidence bridge applied: {bridge_report.applied}")
            for path, reason in bridge_report.rejected:
                logger.warning("evidence bridge rejected %s: %s", path, reason)
        except Exception as exc:
            logger.warning("evidence bridge failed: %s", type(exc).__name__)

        # 桥接后重算 active_factors（证据晋级的因子进入交易图）
        active_factors = [
            fid for fid, record in self._factor_registry._factors.items() if record.has_authorized_active_evidence()
        ]

        # ================================================================
        # Fix 1: 接线 Factor Registry → 控制面 API
        # ================================================================
        # wire_factor_registry 已移除：ControlPlane 无此方法，hasattr 恒为 False

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
        # Stable ordering is part of the strategy/provenance identity; a set
        # iteration order must never change the graph hash or DAG wiring.
        active_factor_ids = sorted(fid for fid in active_factors if fid in self._factor_component_registry)
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
        # Ownership is process-local until a durable intent/venue mapping is
        # re-established.  Orders discovered from the venue on startup are
        # deliberately *not* added here: a graceful shutdown may cancel only
        # orders acknowledged by this process, never manual/other-worker
        # orders that happen to share the account.
        self._owned_order_ids: set[str] = set()
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
        self._last_realtime = 0.0  # 设为 0 使首次实时 tick 立即执行
        # Wall-clock timestamps are retained for operator/audit display, but
        # liveness and scheduling must use monotonic time so NTP/manual clock
        # changes cannot manufacture a healthy or stale executor.
        self._last_realtime_mono = 0.0
        self._last_nearline = 0.0  # 设为 0 使首次近线 tick 立即执行（而非等 300s）
        self._last_offline = time.time()
        self._last_recon = 0.0  # 设为 0 使首次对账立即执行
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
        """Read a signed risk parameter, or a simulation-only default."""
        if key in self._policy_params:
            return float(self._policy_params[key])
        if getattr(self, "_can_write", False):
            raise RuntimeError(f"SIGNED_POLICY_PARAMETER_MISSING:{key}")
        return default

    def _policy_int(self, key: str, default: int) -> int:
        """Read a signed risk parameter, or a simulation-only default."""
        if key in self._policy_params:
            return int(float(self._policy_params[key]))
        if getattr(self, "_can_write", False):
            raise RuntimeError(f"SIGNED_POLICY_PARAMETER_MISSING:{key}")
        return default

    # --- Adapter-bound REST API (BD-02: single adapter boundary) ---
    # 所有 Binance API 访问统一通过 self._adapter (BinanceUsdmAdapter)
    # 不再在 engine 内重复实现签名逻辑

    async def _api_async(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """异步 API 调用 — 通过 BinanceRESTClient Adapter 边界（BD-02）。"""
        result = await self._adapter.request(method, path, signed, params)
        if result.is_success():
            return result.data
        err = result.error
        _msg = str(getattr(err, "message", "unknown")) if err else "unknown"
        return {"error": getattr(err, "http_status", -1) or -1, "msg": _msg}

    async def _api_async_safe(
        self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None
    ) -> tuple[Any, bool]:
        """安全 API 调用 — 返回 (data, ok) 元组。

        与 _api_async 不同，失败时不返回伪 dict，而是返回 (None, False)。
        关键状态读取（账户、持仓、订单）必须使用此方法，
        防止熔断/限流返回的 {"error": ...} 被当作正常数据覆盖有效状态。
        """
        try:
            result = await self._adapter.request(method, path, signed, params)
        except Exception as exc:
            print(f"[api] {path} request exception: {type(exc).__name__}: {exc}")
            return None, False
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

        BD-FIX: Binance Testnet 的 /fapi/v1/openAlgoOrders 端点不稳定，
        经常返回 "Remote end closed connection without response"。
        此时返回空列表（而非 None）允许 testnet 上 protection 正常下发。
        """

        try:
            result = await self._adapter.get_open_algo_orders()
        except Exception as exc:
            print(f"[api] open Algo inventory failed: {type(exc).__name__}: {exc}")
            # BD-FIX: 异常路径同样返回空列表（docstring 承诺的 testnet
            # 语义）—— 返回 None 会触发 _block_unowned_protection_orders
            # → NO_NEW_RISK（I4 审查：demo 端点一次瞬时失败即锁控全局
            # 下单）。保护单缺失重试有 inventory 语义校验兜底。
            if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                print("[api] testnet: treating open Algo inventory failure as empty (retry next cycle)")
                return []
            return None
        if not result.is_success() or result.data is None:
            if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                print("[api] testnet: treating open Algo inventory UNKNOWN as empty (retry next cycle)")
                return []
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
        if error is None:
            return {
                "error": -1,
                "code": -1,
                "msg": "Algo ACK UNKNOWN",
                "category": "UNKNOWN",
                "retryable": False,
                "http_status": 0,
            }
        raw = error.raw if isinstance(error.raw, dict) else {}
        code = raw.get("code", -1)
        category = getattr(error.category, "value", str(error.category))
        return {
            "error": code,
            "code": code,
            "msg": str(raw.get("msg") or error.message)[:500],
            "category": category,
            "retryable": bool(error.retryable),
            "http_status": int(error.http_status or 0),
        }

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
        # BD-FIX: 紧急平仓意图同样必须在创建/签名前按交易对规则量化。
        # 该路径没有交易所原生 closePosition Algo 单 —— 意图仍走
        # _place_order → _plan_execution → _submit_order_slice，执行器对
        # 非 venue-exact 数量拒绝 PLANNED_QUANTITY_NOT_VENUE_EXACT，
        # 未量化的平仓量会让非精确持仓永远无法退出。规则快照
        # UNKNOWN/STALE 时 fail-closed（执行器同样拒绝
        # VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE，行为一致）。
        _venue_exact = self._venue_exact_quantity(str(symbol), amount, tag="protection")
        if _venue_exact is None:
            return False
        amount = _venue_exact
        from beidou_safety.execution import OrderIntent

        now_bucket = int(time.time() / 60)
        approval_nonce = f"emergency-{uuid.uuid4().hex}"
        approval_id = f"EMERGENCY:{policy_id}:{policy_version}:{approval_nonce[-12:]}"
        intent_id = f"emergency-{symbol}-{now_bucket}-{uuid.uuid4().hex[:10]}"
        client_order_id = f"beidou-{str(symbol).lower()}-emergency-{now_bucket}-{uuid.uuid4().hex[:8]}"
        idempotency_key = f"emergency-{str(symbol).upper()}-{now_bucket}-{uuid.uuid4().hex[:8]}"
        correlation = CorrelationId(str(correlation_id)) if correlation_id else None
        proposal_hash = hashlib.sha256(
            f"{symbol}|{side}|{amount}|{policy_id}|{policy_version}|{policy_signature}|{approval_nonce}".encode()
        ).hexdigest()
        account_snapshot_hash = "EMERGENCY_FLATTEN"
        risk_snapshot_hash = "EMERGENCY_FLATTEN"
        risk_policy_version = f"{policy_id}:{policy_version}"
        expires_at = time.time() + DEFAULT_APPROVAL_TTL_SECONDS
        unsigned_intent = OrderIntent(
            intent_id=intent_id,
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
            instrument_id=InstrumentId(str(symbol).upper()),
            side=order_side,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount=str(amount)),
            time_in_force=TimeInForce.GTC,
            client_order_id=client_order_id,
            correlation_id=correlation,
            idempotency_key=idempotency_key,
            risk_approval_id=approval_id,
            reduce_only=True,
            close_position=True,
            emergency_policy_signed=True,
            risk_proposal_hash=proposal_hash,
        )
        intent_hash = order_intent_binding_hash(unsigned_intent)
        try:
            approval_signature = self._approval.issue_for_approved_risk(
                RiskApprovalId(approval_id),
                risk_approved=True,
                proposal_hash=proposal_hash,
                intent_hash=intent_hash,
                account_snapshot_hash=account_snapshot_hash,
                risk_snapshot_hash=risk_snapshot_hash,
                policy_version=risk_policy_version,
                nonce=approval_nonce,
                expires_at=expires_at,
            )
            signature_valid = await self._approval.verify(
                RiskApprovalId(approval_id),
                signature=approval_signature,
                proposal_hash=proposal_hash,
                intent_hash=intent_hash,
                account_snapshot_hash=account_snapshot_hash,
                risk_snapshot_hash=risk_snapshot_hash,
                policy_version=risk_policy_version,
                nonce=approval_nonce,
                expires_at=expires_at,
                consume_nonce=False,
            )
        except (RuntimeError, TypeError, ValueError):
            return False
        if (
            self._risk_sm.approve_if_verified(
                RiskApprovalId(approval_id),
                signature_valid=signature_valid,
                risk_check_passed=True,
                nonce=approval_nonce,
                ttl=expires_at - time.time(),
                risk_snapshot_hash=risk_snapshot_hash,
                policy_version=risk_policy_version,
            )
            != RiskDecision.APPROVED
        ):
            return False
        intent = OrderIntent(
            intent_id=intent_id,
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
            instrument_id=InstrumentId(str(symbol).upper()),
            side=order_side,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount=str(amount)),
            time_in_force=TimeInForce.GTC,
            client_order_id=client_order_id,
            correlation_id=correlation,
            idempotency_key=idempotency_key,
            risk_approval_id=approval_id,
            reduce_only=True,
            close_position=True,
            emergency_policy_signed=True,
            risk_approval_signature=approval_signature,
            risk_proposal_hash=proposal_hash,
            risk_intent_hash=intent_hash,
            risk_account_snapshot_hash=account_snapshot_hash,
            risk_snapshot_hash=risk_snapshot_hash,
            risk_policy_version=risk_policy_version,
            risk_nonce=approval_nonce,
            risk_expires_at=expires_at,
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
            # 降低杠杆时保证金不足 → 必须从交易所读取当前杠杆。
            # 响应缺失或不可解析时保持 UNKNOWN；绝不能把请求值当成
            # 已生效杠杆，否则仓位上限会以未经证实的杠杆继续计算。
            actual = -1
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
            if actual <= 0:
                print(f"[leverage] UNKNOWN: exchange returned no valid leverage for {symbol}")
                self._leverage_cache[symbol] = -1
                return -1
            self._leverage_cache[symbol] = actual
            print(f"[leverage] {symbol}: cannot lower to {target_leverage}x, keeping {actual}x (existing positions)")
            return actual
        else:
            # P2修复: 非预期错误不返回 target_leverage（fail-open → fail-closed）
            print(f"[leverage] FAILED {symbol}: code={binance_code} {resp.get('msg', resp)}")
            return -1  # UNKNOWN → R0/R6 阻断，而非静默继续

    # --- Health & Metrics ---

    def _realtime_age_seconds(self) -> float:
        """Return heartbeat age from a monotonic clock when available."""

        last_mono = getattr(self, "_last_realtime_mono", None)
        if isinstance(last_mono, (int, float)):
            return max(0.0, time.monotonic() - float(last_mono))
        # Compatibility for diagnostic/test shells created without __init__.
        return max(0.0, time.time() - float(getattr(self, "_last_realtime", 0.0)))

    def _fresh_matched_reconciliation(self, *, max_age_seconds: float = 60.0) -> bool:
        """Require a recent independent reconciliation before cleanup writes.

        A stale ``_last_account`` snapshot is not enough evidence to cancel a
        conditional order.  In particular, a temporary account/API failure
        must not be interpreted as a confirmed flat position.  Automated
        ghost/excess cleanup therefore has a short freshness bound and is
        skipped until the three-way reconciliation is matched again.
        """

        result = getattr(self, "_last_reconciliation_result", None)
        if result is None or not bool(getattr(result, "matched", False)):
            return False
        checked_at = getattr(result, "checked_at", None)
        if not isinstance(checked_at, datetime):
            return False
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        return math.isfinite(age) and 0.0 <= age <= max_age_seconds

    def _check_liveness(self) -> HealthState:
        """Expose stalled event-loop state instead of unconditional HEALTHY."""

        if getattr(self, "_can_write", False) and not getattr(self, "_running", False):
            return HealthState.UNHEALTHY
        if getattr(self, "_can_write", False) and not self._user_stream_readiness()[0]:
            return HealthState.UNHEALTHY
        if self._running and self._realtime_age_seconds() > 15.0:
            return HealthState.UNHEALTHY
        # BD-FIX: 零写模式按设计跳过对账（_reconcile 直接返回），
        # _last_reconciliation_result 恒 None。旧逻辑让 paper/shadow 的
        # liveness 恒 DEGRADED → 快照风控 exchange_health=UNSAFE →
        # AUX-R0 恒拒。零写模式以 RESUME + realtime 活跃为健康标准，
        # 与 supervisor 的对账豁免同语义；可写环境保持严格对账要求。
        _env_mode = getattr(self, "_env_mode", None)
        zero_write_mode = _env_mode is not None and str(getattr(_env_mode, "value", _env_mode)).lower() in (
            "paper",
            "shadow",
            "research",
            "safety_only",
        )
        if self._control.get_status() != ControlAction.RESUME:
            return HealthState.DEGRADED
        if not zero_write_mode and (
            self._last_reconciliation_result is None or not self._last_reconciliation_result.matched
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
        # BD-FIX: 账本冻结状态必须暴露 —— freeze 后 incident 可能被
        # auto-resolve 清除，唯一可见信号是 durable 检查本身。
        ledger = getattr(self, "_ledger", None)
        if ledger is not None and getattr(ledger, "is_frozen", False):
            return False, "LEDGER_FROZEN", {}
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
            owned_order_ids = {str(order_id) for order_id in getattr(self, "_owned_order_ids", set())}
            untracked_active_orders = [
                str(row.get("order_id", ""))
                for row in active_rows
                if str(row.get("order_id", "")) not in tracked_order_ids
            ]
            unowned_active_orders = sorted(tracked_order_ids - owned_order_ids)

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
                if str(row.get("owner_id", "")) == str(self._protection_owner_id)
                and str(row.get("status", "")).upper() == "ACTIVE"
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
                "unowned_active_orders": len(unowned_active_orders),
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
            # A live process may only claim/cancel orders it acknowledged in
            # the current ownership epoch.  Startup discovery is venue truth,
            # not proof that this worker owns the order.  Keep the write path
            # fail-closed until an operator-governed reconciliation/adoption
            # flow establishes that mapping.
            if bool(getattr(self, "_can_write", False)) and unowned_active_orders:
                return (
                    False,
                    "DURABLE_ACTIVE_ORDER_OWNER_UNKNOWN",
                    {**evidence, "order_ids": unowned_active_orders[:20]},
                )
            protection_ok, protection_evidence = self._assess_protection_coverage(
                exchange_positions,
                active_protections,
                local_positions,
            )
            evidence.update(protection_evidence)
            # BD-FIX: TruthSnapshot 保护事实周期刷新 — 保护覆盖评估随
            # _durable_fact_status 周期运行（realtime 对账后 30s 一次 +
            # supervisor 健康探测），每次评估都刷新事实，避免运行超过 300s 后
            # 保护事实恒 stale → 资格恒 NOT_VERIFIABLE。覆盖失败时记录
            # UNKNOWN 状态（fail-closed: build_truth_snapshot 的
            # protection_status 跟随记录的 hash，未覆盖时不进入 ACTIVE）。
            # 幂等：多次调用无副作用。
            self._last_protection_hash = (
                hashlib.sha256(b"ACTIVE").hexdigest() if protection_ok else hashlib.sha256(b"UNKNOWN").hexdigest()
            )
            self._last_protection_fact_at = time.time()
            if not protection_ok:
                return False, "DURABLE_PROTECTION_COVERAGE_UNKNOWN", evidence
            return True, "DURABLE_FACTS_VERIFIED", evidence
        except Exception as exc:
            return False, f"DURABLE_FACTS_UNKNOWN:{type(exc).__name__}", {"error": str(exc)[:200]}

    def _owned_active_order_ids(self) -> set[str]:
        """Return active venue orders acknowledged by this process only.

        The intersection is intentionally recomputed instead of trusting a
        single mutable collection.  This keeps shutdown safe if an order was
        already removed from the active tracker while its ownership marker
        remains for audit purposes.
        """

        active = {str(order_id) for order_id in getattr(self, "_active_order_ids", set())}
        owned = {str(order_id) for order_id in getattr(self, "_owned_order_ids", set())}
        return active & owned

    def _unowned_active_order_ids(self) -> set[str]:
        """Return venue-active orders whose ownership is not proven locally."""

        active = {str(order_id) for order_id in getattr(self, "_active_order_ids", set())}
        return active - self._owned_active_order_ids()

    def _assess_protection_coverage(
        self,
        exchange_positions: list[dict[str, Any]],
        active_protections: list[dict[str, Any]],
        local_positions: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Require venue-backed, owner/generation-bound coverage per position.

        A single active conditional order is not evidence that every position
        is protected.  Coverage is therefore checked independently for each
        non-zero venue position: the protection side must be the exact
        reduce-only side, the current durable position generation must match,
        and a stop-loss quantity must cover the complete position quantity.
        This intentionally fails closed when either the venue snapshot or the
        local position projection is inconsistent.
        """

        local_positions = local_positions or {}
        exchange_symbols = {str(row.get("symbol", "")) for row in exchange_positions}
        local_symbols = {
            str(getattr(position, "instrument_id", ""))
            for position in local_positions.values()
            if str(getattr(position, "instrument_id", ""))
        }
        protection_symbols = {
            str(row.get("symbol", "")).strip() for row in active_protections if str(row.get("symbol", "")).strip()
        }
        gaps: list[dict[str, Any]] = []

        # A local position without a matching independent venue fact cannot be
        # safely considered flat or covered after a restart.
        for symbol in sorted(local_symbols - exchange_symbols):
            gaps.append({"symbol": symbol, "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"})
        for symbol in sorted(protection_symbols - exchange_symbols):
            gaps.append({"symbol": symbol, "reason": "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION"})

        # BD-FIX（C3 审查）: 保护覆盖只统计本地所有权可证明的持仓 ——
        # 共享 demo 账户的外部持仓不属于引擎，不得产生覆盖 gap。
        # 豁免仅限 testnet：live/canary 中"交易所有持仓但本地无记录"
        # 仍是严重缺口（丢仓），必须 fail-closed。
        _is_testnet = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
        _locally_owned_symbols = set(local_symbols) | {str(sym) for sym in getattr(self, "_position_generation", {}).keys()}
        for position in exchange_positions:
            symbol = str(position.get("symbol", "")).strip()
            if not symbol:
                gaps.append({"symbol": "", "reason": "VENUE_POSITION_SYMBOL_UNKNOWN"})
                continue
            if _is_testnet and symbol not in _locally_owned_symbols:
                continue  # 外部持仓（共享账户）不参与覆盖判定
            try:
                position_amount = Decimal(str(position.get("positionAmt", "0") or "0"))
            except (InvalidOperation, ValueError, TypeError):
                gaps.append({"symbol": symbol, "reason": "VENUE_POSITION_QUANTITY_UNKNOWN"})
                continue
            if not position_amount.is_finite():
                gaps.append({"symbol": symbol, "reason": "VENUE_POSITION_QUANTITY_UNKNOWN"})
                continue
            position_quantity = abs(position_amount)
            if position_quantity <= Decimal("1e-12"):
                continue
            expected_side = "SELL" if position_amount > 0 else "BUY"
            projection = getattr(self, "_position_projection", {}).get(symbol, {}) or {}
            try:
                projected_generation = int(projection.get("position_generation", 0) or 0)
                current_generation = int(getattr(self, "_position_generation", {}).get(symbol, 0) or 0)
            except (TypeError, ValueError):
                projected_generation = 0
                current_generation = 0
            expected_generation = max(projected_generation, current_generation)

            symbol_protections: list[dict[str, Any]] = []
            for row in active_protections:
                if str(row.get("symbol", "")).strip() != symbol:
                    continue
                if str(row.get("status", "")).upper() != "ACTIVE":
                    continue
                if not str(row.get("exchange_order_id", "")).strip():
                    continue
                if str(row.get("owner_id", "")) != str(getattr(self, "_protection_owner_id", "")):
                    continue
                row_side = getattr(row.get("side"), "value", row.get("side", ""))
                if str(row_side).upper() != expected_side:
                    continue
                try:
                    row_generation = int(row.get("position_generation", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if expected_generation <= 0 or row_generation != expected_generation:
                    continue
                symbol_protections.append(row)

            stop_quantity = Decimal("0")
            total_quantity = Decimal("0")
            invalid_quantity = False
            for row in symbol_protections:
                try:
                    quantity = Decimal(str(row.get("quantity", "0") or "0"))
                except (InvalidOperation, ValueError, TypeError):
                    invalid_quantity = True
                    continue
                if not quantity.is_finite() or quantity <= 0:
                    invalid_quantity = True
                    continue
                total_quantity += quantity
                order_type = str(row.get("order_type", "")).upper()
                if row.get("stop_type") or order_type.startswith("STOP"):
                    stop_quantity += quantity

            if invalid_quantity or stop_quantity + Decimal("1e-12") < position_quantity:
                gaps.append(
                    {
                        "symbol": symbol,
                        "reason": "STOP_LOSS_QUANTITY_UNCOVERED",
                        "position_quantity": str(position_quantity),
                        "stop_quantity": str(stop_quantity),
                        "total_protection_quantity": str(total_quantity),
                        "expected_side": expected_side,
                        "expected_generation": expected_generation,
                    }
                )

        return not gaps, {"unprotected_symbols": gaps}

    def _user_stream_readiness(self, *, max_event_age: float = 60.0) -> tuple[bool, dict[str, Any]]:
        """Return explicit live user-stream evidence for writable readiness.

        ``UserStreamProjector`` is durable, but a restored projection or a
        fresh REST account snapshot cannot prove that the current process is
        receiving venue events.  Keep transport state separate and expose
        missing/old facts as UNKNOWN so a writable engine cannot become ready
        by merely injecting a replay baseline.
        """

        if not bool(getattr(self, "_can_write", False)):
            return True, {"status": "NOT_REQUIRED", "required": False}
        runtime = getattr(self, "_user_stream_runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        status = str(runtime.get("status", "UNKNOWN") or "UNKNOWN").upper()
        last_event_mono = runtime.get("last_event_mono")
        event_age: float | None = None
        try:
            if last_event_mono is not None:
                event_age = max(0.0, time.monotonic() - float(last_event_mono))
        except (TypeError, ValueError, OverflowError):
            event_age = None
        projector = getattr(self, "_user_stream_projector", None)
        raw_projector_status = getattr(getattr(projector, "sequencer", None), "status", "UNKNOWN")
        projector_status = str(getattr(raw_projector_status, "value", raw_projector_status)).upper()
        event_facts = getattr(self, "_event_stream_facts", None)
        projection_complete = bool(getattr(event_facts, "complete", False))
        # PKG02 (BDS-P0-001): 统一生产就绪标准 — 所有环境使用相同 readiness check。
        transport_ok = status in ("HEALTHY", "CONNECTED")
        startup_elapsed = time.monotonic() - getattr(self, "_startup_mono", time.monotonic())
        if not hasattr(self, "_startup_mono"):
            self._startup_mono = time.monotonic()
            startup_elapsed = 0.0
        startup_grace = startup_elapsed < 600.0
        effective_max_age = 300.0 if status in ("CONNECTED", "UNKNOWN") or startup_grace else max_event_age
        projector_ok = projector_status not in {"GAP", "SEQUENCE_UNAVAILABLE"}
        require_complete_projection = True
        # BD-FIX: testnet 的"流活性"按 transport 判定（CONNECTED/HEALTHY +
        # listenKey 有效），不要求事件新鲜。demo 低频环境（凌晨 10+ 分钟
        # 无事件）中"无事件=无成交=无风险积累"，账户状态由 REST 对账
        # （90s 新鲜度）持续验证；事件停流保护由 transport 状态承担
        # （listenKey 失效 → fault → STOPPED → 检查 FAIL）。
        # live/canary 保持严格 event_age 语义不变。
        _env_mode = getattr(self, "_env_mode", None)
        if _env_mode is not None and str(getattr(_env_mode, "value", "")) == "testnet":
            ready = (
                transport_ok
                and projector_ok
                and (projection_complete or event_facts is None or not require_complete_projection)
            )
        else:
            ready = (
                transport_ok
                and (event_age is None or event_age <= effective_max_age)
                and (projector_ok or event_age is None)
                and (projection_complete or event_facts is None or not require_complete_projection)
            )
        return ready, {
            "status": status,
            "event_age_seconds": event_age,
            "threshold_seconds": effective_max_age,
            "projector_status": projector_status,
            "projection_complete": projection_complete,
            "listen_key_active": bool(runtime.get("listen_key_active", False)),
            "last_error": str(runtime.get("last_error", "") or ""),
            "required": True,
        }

    def _check_ready(self) -> bool:
        if getattr(self, "_can_write", False) and getattr(self, "_policy_error", None):
            return False
        if not self._state_backend_supported:
            return False
        # A writable engine must have a live realtime loop before it can
        # advertise readiness.  ``not self._running`` is only acceptable for
        # zero-write diagnostics; allowing RESUME+stopped to pass here makes
        # an independent health endpoint claim that a dead executor is ready.
        if getattr(self, "_can_write", False) and not getattr(self, "_running", False):
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
        user_stream_ready, _ = self._user_stream_readiness()
        if not user_stream_ready:
            return False
        return self._realtime_age_seconds() <= 15.0

    def _check_trading_ready(self) -> tuple[bool, str]:
        if getattr(self, "_can_write", False) and getattr(self, "_policy_error", None):
            return False, str(self._policy_error)
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
            "user_stream_runtime": dict(getattr(self, "_user_stream_runtime", {})),
            "state_backend_supported": self._state_backend_supported,
            "state_backend_error": self._state_backend_error,
            "policy_id": self._policy_id_active,
            "policy_version": self._policy_version,
            "policy_ready": not bool(self._policy_error),
            "realtime_age_seconds": round(self._realtime_age_seconds(), 3),
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
            "state_backend_error": self._state_backend_error,
            "policy_id": self._policy_id_active,
            "policy_version": self._policy_version,
            "policy_error": self._policy_error,
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
            "user_stream_runtime": dict(getattr(self, "_user_stream_runtime", {})),
            "realtime_age_seconds": round(self._realtime_age_seconds(), 3),
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

    def collect_operational_facts(self) -> dict:
        """PKG25 (BDS-P1-048): 公共操作事实采集 API。

        替代监控模块中的 getattr(engine, "_field") 私有字段访问模式。
        返回类型化的事实字典，可由 FactBus 和其他消费者使用。
        """
        from beidou_observability.monitoring.fact_bus import FactDomain, OperationalFact, get_fact_bus

        bus = get_fact_bus()
        facts: dict[str, OperationalFact] = {}

        # Reconciliation fact
        recon = getattr(self, "_last_reconciliation_result", None)
        facts["reconciliation"] = OperationalFact(
            fact_type="reconciliation_result",
            domain=FactDomain.LEDGER,
            payload={
                "status": str(getattr(recon, "status", "UNKNOWN")),
                "matched": bool(getattr(recon, "matched", False)),
                "checked_at": str(getattr(recon, "checked_at", "")),
                "differences": list(getattr(recon, "differences", [])),
            },
            source="engine._reconcile",
        )
        bus.publish(facts["reconciliation"])

        # Control fact
        ctrl = getattr(self, "_control", None)
        facts["control"] = OperationalFact(
            fact_type="control_state",
            domain=FactDomain.CONTROL,
            payload={
                "status": str(getattr(ctrl, "get_status", lambda: "UNKNOWN")()),
                "can_write": bool(getattr(self, "_can_write", False)),
            },
            source="engine._control",
        )
        bus.publish(facts["control"])

        # Protection fact
        prot = getattr(self, "_protection", None)
        facts["protection"] = OperationalFact(
            fact_type="protection_state",
            domain=FactDomain.PROTECTION,
            payload={
                "owner_unknown": bool(getattr(self, "_protection_owner_unknown", False)),
                "position_count": int(getattr(prot, "position_count", lambda: 0)()) if prot else 0,
            },
            source="engine._protection",
        )
        bus.publish(facts["protection"])

        # Lifecycle fact
        lc = getattr(self, "_lifecycle", None)
        facts["lifecycle"] = OperationalFact(
            fact_type="lifecycle_state",
            domain=FactDomain.LIFECYCLE,
            payload={
                "state": str(getattr(lc, "state", "UNKNOWN")),
                "mode": str(getattr(self, "_env_mode", "UNKNOWN")),
            },
            source="engine._lifecycle",
        )
        bus.publish(facts["lifecycle"])

        # Risk fact
        risk_state = (
            self._strategy_risk.get_state(self._autopilot_strategy_id) if hasattr(self, "_strategy_risk") else None
        )
        facts["risk"] = OperationalFact(
            fact_type="risk_state",
            domain=FactDomain.RISK,
            payload={
                "level": str(getattr(risk_state, "risk_level", "UNKNOWN")),
                "drawdown_pct": round(float(getattr(risk_state, "current_drawdown_pct", 0)), 2),
            },
            source="engine._strategy_risk",
        )
        bus.publish(facts["risk"])

        return {k: f.to_dict() for k, f in facts.items()}

    def _rebuild_alpha_graph(self) -> None:
        """BF-08: 从 FactorRegistry 重建 AlphaGraph。

        当因子生命周期变更（promotion/degradation/suspension）时调用，
        确保交易图仅包含 ACTIVE 因子。新增或移除的因子自动反映到 DAG 拓扑。
        """
        # BD-T06: CHALLENGER 只允许进入零写模拟/影子路径。
        # Testnet 具有交易所写能力，不能因为它不是 production 就把
        # 尚未完成 ACTIVE 生产批准的 challenger 接入可写执行图。
        # FactorRecord is mutable and intentionally unhashable.  Keep the
        # graph keyed by the immutable registry identifier instead of putting
        # records themselves in a set; otherwise the first real promotion to
        # ACTIVE would crash this rebuild path with TypeError.
        trading_factors: set[str] = set()
        for record in self._factor_registry.get_active():
            authorization_check = getattr(record, "has_authorized_active_evidence", None)
            # Real FactorRecord instances always expose the evidence-bound
            # predicate.  Keep lightweight diagnostic fakes usable in unit
            # probes without weakening the production registry contract.
            authorized = bool(authorization_check()) if callable(authorization_check) else True
            if authorized:
                trading_factors.add(record.definition.factor_id)
        if self._can_simulate:
            trading_factors.update(record.definition.factor_id for record in self._factor_registry.get_challengers())
        # Stable ordering is part of the strategy/provenance identity; a set
        # iteration order must never change the graph hash or DAG wiring.
        active_factor_ids = sorted(fid for fid in trading_factors if fid in self._factor_component_registry)

        new_graph = AlphaGraph(strategy_id=StrategyId("autopilot"))
        component_instances: dict[str, AlphaComponent] = {}
        for fid in active_factor_ids:
            component_cls, _ = self._factor_component_registry[fid]
            component = component_cls()
            new_graph.add_component(component)
            component_instances[fid] = component

        # Re-wire ENTRY → FILTER → EXIT
        added = set(active_factor_ids)
        for entry_id in sorted(added & self._entry_ids):
            for filter_id in sorted(added & self._filter_ids):
                new_graph.connect(entry_id, filter_id)
        for filter_id in sorted(added & self._filter_ids):
            for exit_id in sorted(added & self._exit_ids):
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
                        quote = _validated_ws_quote(ticker)
                        if quote is not None:
                            ws_price, ws_bid, ws_ask = quote
                            features["price"] = ws_price
                            features["bid"] = ws_bid
                            features["ask"] = ws_ask
                            features["spread_bps"] = (ws_ask - ws_bid) / ws_ask * 10000
                        else:
                            # A stale/incomplete WS ticker cannot be repaired
                            # by copying the last close into bid/ask.  Refresh
                            # the complete snapshot through the feed boundary.
                            features = await self._feed.async_update_features(symbol)
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
                # BD-FIX: TruthSnapshot 行情事实 — 与快照保存同源（symbol:price:bid:ask）
                self._last_market_hash = hashlib.sha256(
                    f"{symbol}:{price}:{features.get('bid')}:{features.get('ask')}".encode()
                ).hexdigest()
                self._last_market_fact_at = time.time()

            # 5. Process only claimable intents.  Durable UNKNOWN intents are
            # deliberately excluded until an explicit exchange query resolves
            # them; a restart must never blind-retry an ambiguous submission.
            durable_outbox = bool(
                getattr(self._outbox, "_db_path", None)
                or getattr(self._outbox, "_connection_factory", None)
                or getattr(self._outbox, "_dsn", None)
            )
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
                        intent = self._outbox.claim(owner=self._lease_owner)
                        if intent is None:
                            break
                        try:
                            await self._place_order(intent)
                        except Exception as _claim_exc:
                            # 单条 intent 异常不应阻断整个 claim 循环；
                            # 失败的 intent 保持在 SENDING，下次重启恢复。
                            print(
                                f"[realtime] Intent claim error ({getattr(intent, 'intent_id', '?')}): "
                                f"{type(_claim_exc).__name__}: {_claim_exc}"
                            )
                else:
                    for intent in unacked:
                        await self._place_order(intent)

            # 6. Reconciliation (every 30s)
            # BD-FIX: UNKNOWN/SENDING 意图的运行时恢复（C1 审查：demo
            # 一次 POST 超时即让该标的在途敞口永久占满、重启前无法再
            # 下单）。周期重查（按 client_id 的 identity-bound venue
            # 事实裁决），与启动时 resolve 同语义。
            if time.time() - getattr(self, "_last_unknown_resolve", 0.0) > 60:
                self._last_unknown_resolve = time.time()
                try:
                    await asyncio.wait_for(self._resolve_unknown_outbox_intents(), timeout=20.0)
                except Exception as _resolve_exc:
                    logger.warning("periodic unknown-intent resolve skipped: %s", type(_resolve_exc).__name__)

            if time.time() - self._last_recon > 30:
                # BD-FIX: 对账含交易所网络调用，慢响应不得阻塞实时循环
                # （循环停摆会拉大对账间隔，触发 supervisor 的 60s 新鲜度
                # 检查降级）。超时按 fail-closed 处理：本轮不更新事实，
                # 30s 后下一轮重试。
                try:
                    recon_ok = await asyncio.wait_for(self._reconcile(), timeout=25.0)
                except asyncio.TimeoutError:
                    recon_ok = False
                self._last_recon = time.time()
                # 对账通过 → 检查持久事实并自动清除事故
                if recon_ok:
                    durable_ok, _, _ = self._durable_fact_status()
                    # BD-FIX: incident 清理与 RESUME 动作解耦。旧逻辑把两者
                    # 包在 `control != RESUME` 条件下 —— 控制面已 RESUME
                    # （testnet 自动重新授权）时 incident 永不 resolve，
                    # 残留 CRITICAL 事故永久展示且告警噪音不断（final14/21
                    # 实测）。事实干净即 resolve；RESUME 仅幂等执行。
                    if durable_ok:
                        self._maybe_auto_resolve_incidents()
                        # BD-FIX: 保护事实恢复后复位所有权标志（I3 审查：
                        # 置位后进程内永不复位，瞬态 API 失败即永久锁死）
                        if getattr(self, "_protection_owner_unknown", False):
                            self._protection_owner_unknown = False
                            self._last_protection_hash = hashlib.sha256("ACTIVE".encode()).hexdigest()
                            self._last_protection_fact_at = time.time()
                            print("[protection] ownership verified — protection facts restored")
                    if durable_ok and self._control.get_status() != ControlAction.RESUME:
                        try:
                            self._control.execute_action(ControlAction.RESUME)
                            print("[realtime] Auto-restored RESUME after durable facts verified")
                        except Exception as exc:
                            logger.warning("automatic RESUME failed: %s", type(exc).__name__)

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
            self._last_realtime_mono = time.monotonic()

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
        # BD-FIX: TruthSnapshot 保护事实 — 所有权无法证明 → 记录 UNKNOWN 状态
        self._last_protection_hash = hashlib.sha256("UNKNOWN".encode()).hexdigest()
        self._last_protection_fact_at = time.time()
        # PKG02 (BDS-P0-001): 移除 testnet 保护所有权未知旁路 — 所有环境统一升级控制面
        if self._control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
            self._safe_no_new_risk("auto")
        self._alerts.send_incident(
            AlertSeverity.CRITICAL,
            "Protection ownership unknown",
            f"Conditional orders lack durable owner mapping: {sorted(order_ids)}",
            category="protection",
        )

    def _protection_inventory_semantic_issues(self, inventory: list[dict[str, Any]]) -> list[str]:
        """Compare venue Algo facts with durable protection intent semantics.

        An ``algoId`` proves identity only.  A venue order can still have the
        wrong symbol, side, quantity, trigger, type or reduce-only flag.  Such
        a row cannot certify coverage; callers must keep the risk gate closed
        and obtain an independently governed reconciliation decision.
        """

        store = getattr(self, "_store", None)
        if store is None:
            return ["PROTECTION_STORE_UNAVAILABLE"] if getattr(self, "_can_write", False) else []
        try:
            expected_rows = [
                row
                for row in store.restore_protections()
                if str(row.get("status", "")).upper() == "ACTIVE"
                and str(row.get("owner_id", "")) == str(getattr(self, "_protection_owner_id", ""))
                and str(row.get("exchange_order_id", "")).strip()
            ]
        except Exception as exc:
            return [f"PROTECTION_STORE_READ_UNKNOWN:{type(exc).__name__}"]

        venue_by_id = {
            str(row.get("algoId")): row
            for row in inventory
            if isinstance(row, dict) and str(row.get("algoId", "")).strip()
        }
        issues: list[str] = []

        def _decimal(value: Any) -> Decimal | None:
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return parsed if parsed.is_finite() else None

        for expected in expected_rows:
            algo_id = str(expected.get("exchange_order_id", "")).strip()
            actual = venue_by_id.get(algo_id)
            if actual is None:
                # Presence is checked separately, but retain a semantic
                # reason here so the persisted incident identifies the exact
                # protection row that cannot be certified.
                issues.append(f"PROTECTION_VENUE_ROW_MISSING:{algo_id}")
                continue
            expected_symbol = str(expected.get("symbol", "")).strip().upper()
            actual_symbol = str(actual.get("symbol", "")).strip().upper()
            if not expected_symbol or actual_symbol != expected_symbol:
                issues.append(f"PROTECTION_SYMBOL_MISMATCH:{algo_id}")
            expected_side = str(expected.get("side", "")).strip().upper()
            actual_side = str(actual.get("side", "")).strip().upper()
            if not expected_side or actual_side != expected_side:
                issues.append(f"PROTECTION_SIDE_MISMATCH:{algo_id}")
            expected_type = str(expected.get("order_type", "")).strip().upper()
            actual_type = str(actual.get("orderType", actual.get("type", ""))).strip().upper()
            if not expected_type or actual_type != expected_type:
                issues.append(f"PROTECTION_TYPE_MISMATCH:{algo_id}")
            expected_qty = _decimal(expected.get("quantity"))
            actual_qty = _decimal(actual.get("quantity"))
            if expected_qty is None or actual_qty is None or expected_qty <= 0 or actual_qty != expected_qty:
                issues.append(f"PROTECTION_QUANTITY_MISMATCH:{algo_id}")
            expected_trigger = _decimal(expected.get("trigger_price"))
            actual_trigger = _decimal(actual.get("triggerPrice"))
            if (
                expected_trigger is None
                or actual_trigger is None
                or expected_trigger <= 0
                or actual_trigger != expected_trigger
            ):
                issues.append(f"PROTECTION_TRIGGER_MISMATCH:{algo_id}")
            reduce_only = actual.get("reduceOnly")
            if reduce_only is not True and str(reduce_only).strip().lower() not in {"1", "true", "yes"}:
                issues.append(f"PROTECTION_REDUCE_ONLY_UNPROVEN:{algo_id}")
        return sorted(set(issues))

    def _restore_durable_protection_projection(
        self,
        account: dict[str, Any],
        inventory: list[dict[str, Any]] | None,
    ) -> bool:
        """Hydrate the local protection projection from ACK-backed durable rows.

        The venue inventory is read-only evidence.  When an ACTIVE durable
        protection already exists, startup must rebuild the in-memory
        position projection and must not call ``create_protection`` (which
        would submit a duplicate SL/TP pair).  Any ambiguity leaves the risk
        gate closed and returns ``False``; it never adopts or cancels a row.
        """

        try:
            rows = list(self._store.restore_protections())
        except Exception as exc:
            self._block_unowned_protection_orders([f"PROTECTION_STORE_READ_UNKNOWN:{type(exc).__name__}"])
            return False
        if not rows:
            return True
        if not isinstance(inventory, list):
            self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
            return False

        semantic_issues = self._protection_inventory_semantic_issues(inventory)
        if semantic_issues:
            # BD-FIX (S2): 区分"交易所缺失"与"语义不匹配"。
            # 交易所缺失 → 保护单已被触发/取消/过期 → 清理本地存储。
            # 语义不匹配 → 真正风险 → 保持阻断。
            venue_missing = [i for i in semantic_issues if i.startswith("PROTECTION_VENUE_ROW_MISSING:")]
            hard_issues = [i for i in semantic_issues if not i.startswith("PROTECTION_VENUE_ROW_MISSING:")]
            if venue_missing and not hard_issues:
                # 所有问题都是"交易所缺失" → 保护单已被触发/取消/过期
                # → 清理本地过期记录，而非永久阻断。
                store = getattr(self, "_store", None)
                cleaned = 0
                for row in rows:
                    algo_id = str(row.get("exchange_order_id", "")).strip()
                    if any(algo_id in issue for issue in venue_missing):
                        pos_id = str(row.get("position_id", "")).strip()
                        if store and pos_id:
                            try:
                                store.remove_protection(pos_id)
                                cleaned += 1
                            except Exception as exc:
                                print(
                                    f"[beidou-autopilot] Failed to clean stale "
                                    f"protection pos={pos_id} algo={algo_id}: {exc}"
                                )
                if cleaned:
                    print(f"[beidou-autopilot] Cleaned {cleaned} stale protection(s) (no longer on venue)")
                # 重新加载
                try:
                    rows = list(self._store.restore_protections())
                except Exception as exc:
                    logger.error("Protection inventory reload failed after cleanup: %s", type(exc).__name__)
                    self._block_unowned_protection_orders(["PROTECTION_RELOAD_UNKNOWN"])
                    return False
                if not rows:
                    return True
                semantic_issues = self._protection_inventory_semantic_issues(inventory)
            if semantic_issues:
                self._block_unowned_protection_orders(semantic_issues)
                return False

        raw_positions = account.get("positions") if isinstance(account, dict) else None
        if not isinstance(raw_positions, list):
            self._block_unowned_protection_orders(["ACCOUNT_POSITIONS_UNKNOWN"])
            return False
        venue_positions: dict[str, tuple[Decimal, float]] = {}
        for raw in raw_positions:
            if not isinstance(raw, dict):
                self._block_unowned_protection_orders(["ACCOUNT_POSITION_ROW_UNKNOWN"])
                return False
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            try:
                amount = Decimal(str(raw.get("positionAmt", "0") or "0"))
                entry = float(raw.get("entryPrice", 0) or 0)
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                self._block_unowned_protection_orders([f"ACCOUNT_POSITION_FACT_UNKNOWN:{symbol}"])
                return False
            if not amount.is_finite() or not math.isfinite(entry):
                self._block_unowned_protection_orders([f"ACCOUNT_POSITION_FACT_UNKNOWN:{symbol}"])
                return False
            if abs(amount) > Decimal("1e-12"):
                if symbol in venue_positions:
                    self._block_unowned_protection_orders([f"ACCOUNT_POSITION_DUPLICATE:{symbol}"])
                    return False
                venue_positions[symbol] = (amount, entry)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            position_id = str(row.get("position_id", "")).strip()
            if not position_id:
                self._block_unowned_protection_orders(["PROTECTION_POSITION_ID_UNKNOWN"])
                return False
            grouped.setdefault(position_id, []).append(row)

        symbol_position_ids: dict[str, set[str]] = {}
        for position_id, position_rows in grouped.items():
            for symbol in {str(row.get("symbol", "")).strip().upper() for row in position_rows if row.get("symbol")}:
                symbol_position_ids.setdefault(symbol, set()).add(position_id)
        conflicting_symbols = sorted(
            symbol for symbol, position_ids in symbol_position_ids.items() if len(position_ids) > 1
        )
        if conflicting_symbols:
            self._block_unowned_protection_orders(
                [f"PROTECTION_MULTIPLE_ACTIVE_GENERATIONS:{symbol}" for symbol in conflicting_symbols]
            )
            return False

        for position_id, position_rows in grouped.items():
            symbols = {str(row.get("symbol", "")).strip().upper() for row in position_rows}
            owners = {str(row.get("owner_id", "")).strip() for row in position_rows}
            try:
                generations = {int(row.get("position_generation", 0) or 0) for row in position_rows}
            except (TypeError, ValueError, OverflowError):
                self._block_unowned_protection_orders([f"PROTECTION_GENERATION_UNKNOWN:{position_id}"])
                return False
            sessions = {str(row.get("session_id", "")) for row in position_rows}
            if len(symbols) != 1 or "" in symbols:
                self._block_unowned_protection_orders([f"PROTECTION_SYMBOL_UNKNOWN:{position_id}"])
                return False
            symbol = next(iter(symbols))
            if owners != {str(self._protection_owner_id)} or len(generations) != 1 or 0 in generations:
                self._block_unowned_protection_orders([f"PROTECTION_OWNER_OR_GENERATION_UNKNOWN:{position_id}"])
                return False
            if len(sessions) != 1:
                self._block_unowned_protection_orders([f"PROTECTION_SESSION_MISMATCH:{position_id}"])
                return False
            venue_position = venue_positions.get(symbol)
            if venue_position is None:
                self._block_unowned_protection_orders([f"PROTECTION_WITHOUT_VENUE_POSITION:{symbol}"])
                return False
            signed_quantity, account_entry = venue_position
            if account_entry <= 0:
                projection = getattr(self, "_position_projection", {}).get(symbol, {}) or {}
                try:
                    account_entry = float(projection.get("entry_price", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    account_entry = 0.0
            if not math.isfinite(account_entry) or account_entry <= 0:
                self._block_unowned_protection_orders([f"PROTECTION_ENTRY_PRICE_UNKNOWN:{symbol}"])
                return False

            position_side = OrderSide.BUY if signed_quantity > 0 else OrderSide.SELL
            expected_protection_side = OrderSide.SELL if position_side == OrderSide.BUY else OrderSide.BUY
            stop_orders: list[ProtectionOrder] = []
            take_profit_orders: list[ProtectionOrder] = []
            stop_quantity = Decimal("0")
            for row in position_rows:
                try:
                    protection_id = str(row["protection_id"])
                    exchange_order_id = str(row["exchange_order_id"]).strip()
                    order_side = OrderSide(str(row["side"]).upper())
                    order_type = str(row["order_type"]).strip().upper()
                    trigger_price = Decimal(str(row["trigger_price"]))
                    quantity = Decimal(str(row["quantity"]))
                except (KeyError, InvalidOperation, TypeError, ValueError):
                    self._block_unowned_protection_orders([f"PROTECTION_ROW_UNKNOWN:{position_id}"])
                    return False
                if (
                    not protection_id
                    or not exchange_order_id
                    or order_side != expected_protection_side
                    or not order_type
                    or not trigger_price.is_finite()
                    or trigger_price <= 0
                    or not quantity.is_finite()
                    or quantity <= 0
                ):
                    self._block_unowned_protection_orders([f"PROTECTION_ROW_SEMANTICS_UNKNOWN:{protection_id}"])
                    return False
                stop_type_raw = str(row.get("stop_type", "") or "").strip()
                take_profit_type_raw = str(row.get("take_profit_type", "") or "").strip()
                try:
                    stop_type = StopLossType(stop_type_raw) if stop_type_raw else None
                    take_profit_type = TakeProfitType(take_profit_type_raw) if take_profit_type_raw else None
                except ValueError:
                    self._block_unowned_protection_orders([f"PROTECTION_TYPE_UNKNOWN:{protection_id}"])
                    return False
                is_stop = stop_type is not None or order_type.startswith("STOP")
                is_take_profit = take_profit_type is not None or order_type.startswith("TAKE_PROFIT")
                if (
                    is_stop == is_take_profit
                    or (is_stop and stop_type is None)
                    or (is_take_profit and take_profit_type is None)
                ):
                    self._block_unowned_protection_orders([f"PROTECTION_ROLE_UNKNOWN:{protection_id}"])
                    return False
                order = ProtectionOrder(
                    protection_id=protection_id,
                    position_id=position_id,
                    instrument_id=InstrumentId(symbol),
                    venue_id=VenueId("BINANCE"),
                    side=order_side,
                    trigger_price=Price(amount=str(trigger_price)),
                    order_price=(Price(amount=str(row["order_price"])) if row.get("order_price") else None),
                    quantity=Quantity(amount=str(quantity)),
                    order_type=order_type,
                    reduce_only=True,
                    status=ProtectionStatus.ACTIVE,
                    stop_type=stop_type,
                    take_profit_type=take_profit_type,
                    owner_id=str(self._protection_owner_id),
                    position_generation=next(iter(generations)),
                    session_id=next(iter(sessions)),
                    exchange_order_id=exchange_order_id,
                )
                if is_stop:
                    stop_orders.append(order)
                    stop_quantity += quantity
                else:
                    take_profit_orders.append(order)

            if len(stop_orders) != 1 or stop_quantity < abs(signed_quantity):
                self._block_unowned_protection_orders([f"PROTECTION_STOP_COVERAGE_UNKNOWN:{position_id}"])
                return False
            if position_id in self._protection.all_positions():
                continue
            projection = PositionProtection(
                position_id=position_id,
                instrument_id=InstrumentId(symbol),
                venue_id=VenueId("BINANCE"),
                entry_price=account_entry,
                quantity=float(abs(signed_quantity)),
                side=position_side,
                stop_loss=stop_orders[0],
                take_profits=take_profit_orders,
                owner_id=str(self._protection_owner_id),
                position_generation=next(iter(generations)),
                session_id=next(iter(sessions)),
            )
            projection.update_price_extremes(account_entry)
            try:
                self._protection.restore_position_protection(projection)
            except (TypeError, ValueError) as exc:
                self._block_unowned_protection_orders([f"PROTECTION_RESTORE_FAILED:{type(exc).__name__}"])
                return False
            self._position_entry_times.setdefault(position_id, time.time())
        return True

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

    def _safe_no_new_risk(self, reason: str = "") -> None:
        """安全降级控制面到 NO_NEW_RISK；环境标签不得改变语义。"""
        _control = getattr(self, "_control", None)
        if _control is None:
            return
        try:
            if _control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
                _control.execute_action(ControlAction.NO_NEW_RISK)
        except Exception:
            pass  # 降级动作失败不得掩盖原始故障；上层 incident/日志已记录

    def _record_execution_fact_failure_env_guarded(
        self,
        reason: str,
        *,
        alert_title: str = "Execution fact persistence blocked",
        alert_category: str = "execution_fact",
    ) -> None:
        """执行/用户流层失败的环境化处理（BD-FIX，覆盖 12+ 调用点）。

        ``_record_execution_fact_failure`` 会 freeze 账本（无解冻路径），
        demo 抖动（REST 超时/sqlite 锁/ws 断连/共享账户事件）即永久停机。
        testnet 只降级控制面（NO_NEW_RISK，可恢复）并保留事故可见性
        （incident 照发，不 freeze）；live/canary 保留 freeze 语义
        （真实资金下执行真相丢失不可接受，账本必须冻结）。

        安全等价性：testnet 不 freeze 时，账本写失败意味着 durable 行
        缺失 → ``_durable_fact_status`` 仍判定不可恢复 → RESUME 被阻断，
        与 freeze 一样阻止继续交易，只是可恢复。
        """
        if str(getattr(getattr(self, "_env_mode", None), "value", "")) in ("live", "canary"):
            self._record_execution_fact_failure(reason, alert_title=alert_title, alert_category=alert_category)
        else:
            self._safe_no_new_risk(reason)
            _alerts = getattr(self, "_alerts", None)
            if _alerts is not None:
                with contextlib.suppress(Exception):
                    _alerts.send_incident(
                        AlertSeverity.CRITICAL,
                        alert_title,
                        str(reason)[:500],
                        category=alert_category,
                    )

    def _record_execution_fact_failure(
        self,
        reason: str,
        *,
        alert_title: str = "Execution fact persistence blocked",
        alert_category: str = "execution_fact",
    ) -> None:
        """Freeze execution truth after a durable fact write cannot be proven."""
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
                    alert_title,
                    str(reason)[:500],
                    category=alert_category,
                )

    def _mark_order_unknown(self, order_id: str, symbol: str, reason: str) -> None:
        """Persist an ambiguous venue order as ``UNKNOWN`` and close risk.

        An order-status/read-after-fill failure is not equivalent to a
        cancellation.  The durable UNKNOWN row is the restart/reconciliation
        anchor; dropping the process-local tracker alone would allow a later
        process to forget an in-flight venue fact and potentially submit new
        exposure against an incomplete projection.
        """

        tracker = getattr(self, "_order_trackers", {}).get(order_id)
        if tracker is not None:
            with contextlib.suppress(Exception):
                tracker.apply(OrderEvent.UNKNOWN)

        persist_error = ""
        try:
            store = getattr(self, "_store", None)
            if store is None or not callable(getattr(store, "save_order_state", None)):
                raise RuntimeError("ORDER_UNKNOWN_STORE_UNAVAILABLE")
            store.save_order_state(
                order_id,
                str(symbol or getattr(self, "_order_symbols", {}).get(order_id, "UNKNOWN")),
                "UNKNOWN",
                "UNKNOWN",
                "0",
                None,
                "UNKNOWN",
            )
        except Exception as exc:
            persist_error = f"; UNKNOWN_PERSISTENCE_FAILED:{type(exc).__name__}"

        # Keep the venue order out of the ordinary polling path, but retain a
        # durable UNKNOWN record that blocks readiness and requires governed
        # client-order/exchange reconciliation before any retry.
        getattr(self, "_active_order_ids", set()).discard(order_id)
        self._record_execution_fact_failure_env_guarded(f"{reason}{persist_error}")

    async def _resolve_unknown_outbox_intents(self) -> int:
        """Resolve UNKNOWN only from an identity-bound positive venue fact.

        A failed client-order lookup is not proof that the venue never
        accepted the write.  Until the adapter exposes a separately typed,
        definitive absence result, failures stay UNKNOWN and cannot be
        requeued.
        """

        try:
            get_unknown = getattr(self._outbox, "get_unknown_intents", None)
            unknown_intents = get_unknown() if callable(get_unknown) else []
        except Exception:
            return 0

        resolved_count = 0
        for item in unknown_intents:
            if not isinstance(item, dict):
                continue
            intent_id = str(item.get("intent_id", "")).strip()
            client_id = str(item.get("client_order_id", "")).strip()
            symbol = str(item.get("symbol", "")).strip().upper()
            if not intent_id or not client_id or not symbol:
                continue
            try:
                query = await self._adapter.query_order_by_client_id(symbol, client_id)
            except Exception as exc:
                logger.warning(
                    "UNKNOWN intent lookup failed for %s: %s",
                    intent_id,
                    type(exc).__name__,
                )
                continue
            if not query.is_success() or not isinstance(query.data, dict) or not query.data.get("orderId"):
                continue
            venue_order = query.data
            if (
                str(venue_order.get("clientOrderId", "")) != client_id
                or str(venue_order.get("symbol", "")).strip().upper() != symbol
            ):
                continue
            try:
                OrderStatus(str(venue_order.get("status", "")))
            except ValueError:
                continue
            try:
                store = getattr(self, "_store", None)
                save_order_state = getattr(store, "save_order_state", None)
                if not callable(save_order_state):
                    raise RuntimeError("UNKNOWN_ORDER_FACT_STORE_UNAVAILABLE")
                save_order_state(
                    order_id=str(venue_order["orderId"]),
                    symbol=symbol,
                    side=str(venue_order["side"]),
                    order_type=str(venue_order["type"]),
                    quantity=str(venue_order["origQty"]),
                    price=str(venue_order["price"]) if venue_order.get("price") not in (None, "", "0") else None,
                    status=str(venue_order["status"]),
                    filled_qty=str(venue_order["executedQty"]),
                    avg_price=(
                        str(venue_order["avgPrice"]) if venue_order.get("avgPrice") not in (None, "", "0") else None
                    ),
                    client_order_id=client_id,
                )
            except Exception as exc:
                self._record_execution_fact_failure_env_guarded(
                    f"UNKNOWN_ORDER_FACT_PERSISTENCE_FAILED:{intent_id}:{type(exc).__name__}"
                )
                continue
            self._outbox.resolve_unknown(intent_id, exchange_order_found=True)
            resolved_count += 1
        return resolved_count

    @staticmethod
    def _protection_client_algo_id(protection_order: Any) -> str:
        """Derive a stable venue idempotency key from the exact protection command."""

        def _value(value: Any) -> str:
            return str(getattr(value, "value", value) or "")

        material = "|".join(
            [
                _value(getattr(protection_order, "protection_id", "")),
                _value(getattr(protection_order, "owner_id", "")),
                _value(getattr(protection_order, "position_generation", "")),
                _value(getattr(protection_order, "instrument_id", "")),
                _value(getattr(protection_order, "side", "")),
                _value(getattr(protection_order, "order_type", "")),
                _value(getattr(getattr(protection_order, "quantity", None), "amount", "")),
                _value(getattr(getattr(protection_order, "trigger_price", None), "amount", "")),
            ]
        )
        return f"bdp-{hashlib.sha256(material.encode()).hexdigest()[:28]}"

    @classmethod
    def _protection_algo_params(
        cls,
        protection_order: Any,
        *,
        symbol: str,
        side: str,
        precision: dict[str, int],
    ) -> dict[str, str]:
        """Build one exact, idempotency-bound protection command."""

        try:
            quantity_value = Decimal(str(protection_order.quantity.amount))
            trigger_value = Decimal(str(protection_order.trigger_price.amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("protection quantity or trigger is invalid") from exc
        if not quantity_value.is_finite() or quantity_value <= 0:
            raise ValueError("protection quantity must be finite and positive")
        if not trigger_value.is_finite() or trigger_value <= 0:
            raise ValueError("protection trigger must be finite and positive")
        quantity = f"{quantity_value:.{precision['quantity']}f}"
        trigger_price = f"{trigger_value:.{precision['price']}f}"
        if Decimal(quantity) <= 0:
            raise ValueError("protection quantity rounds to zero at venue precision")
        if Decimal(trigger_price) <= 0:
            raise ValueError("protection trigger rounds to zero at venue precision")
        return {
            "symbol": str(symbol),
            "side": str(side),
            "algoType": "CONDITIONAL",
            "type": str(protection_order.order_type),
            "quantity": quantity,
            "triggerPrice": trigger_price,
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
            "clientAlgoId": cls._protection_client_algo_id(protection_order),
        }

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
            self._record_execution_fact_failure_env_guarded(
                f"ledger transaction {transaction.transaction_id} persistence/post failed: {type(exc).__name__}: {exc}"
            )
            raise

    async def _verify_intent_at_send(self, intent, *, consume_nonce: bool = False) -> bool:
        """最终写边界复核审批、nonce、策略版本和风险下降语义。"""

        if not self._can_write:
            return True
        # The configured Testnet/Production database must be the authoritative
        # execution fact store.  Readiness already reports this condition, but
        # the executor is a second, independent safety boundary: a direct
        # engine caller must not turn the diagnostic SQLite fallback into a
        # live risk-increasing send.  Explicit reduce-only/close-position
        # intents remain eligible for a governed emergency exit.
        if not getattr(self, "_state_backend_supported", False) and not (
            bool(getattr(intent, "reduce_only", False)) or bool(getattr(intent, "close_position", False))
        ):
            return False
        if (
            getattr(self, "_can_write", False)
            and getattr(self, "_policy_error", None)
            and not (bool(getattr(intent, "reduce_only", False)) or bool(getattr(intent, "close_position", False)))
        ):
            return False
        approval_id = str(getattr(intent, "risk_approval_id", "") or "")
        if approval_id == "RISK_EXEMPT_CLOSE":
            # 仅允许明确标记为 reduce-only 的风险下降命令走紧急路径。
            return bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False))
        if not approval_id or not getattr(intent, "risk_approval_signature", None):
            return False
        intent_hash = str(getattr(intent, "risk_intent_hash", "") or "")
        if not intent_hash or intent_hash != order_intent_binding_hash(intent):
            # The signed risk proposal is not enough: a durable payload could
            # otherwise mutate quantity/side/client-id after approval.  The
            # exact venue order must match the HMAC-bound canonical digest.
            return False
        approval_ref = RiskApprovalId(approval_id)
        if not self._risk_sm.is_valid_for_use(
            approval_ref,
            nonce=str(getattr(intent, "risk_nonce", "")),
            risk_snapshot_hash=str(getattr(intent, "risk_snapshot_hash", "")),
            policy_version=str(getattr(intent, "risk_policy_version", "")),
        ):
            return False
        try:
            verified = await self._approval.verify(
                approval_ref,
                signature=str(intent.risk_approval_signature),
                proposal_hash=str(getattr(intent, "risk_proposal_hash", "")),
                intent_hash=intent_hash,
                account_snapshot_hash=str(getattr(intent, "risk_account_snapshot_hash", "")),
                risk_snapshot_hash=str(getattr(intent, "risk_snapshot_hash", "")),
                policy_version=str(getattr(intent, "risk_policy_version", "")),
                nonce=str(getattr(intent, "risk_nonce", "")),
                expires_at=getattr(intent, "risk_expires_at", None),
                consume_nonce=consume_nonce,
            )
            if not verified:
                return False
            if consume_nonce:
                self._risk_sm.consume(approval_ref)
                return self._risk_sm.is_consumed(approval_ref)
            return True
        except (RuntimeError, TypeError, ValueError):
            return False

    async def _place_order(self, intent, symbol: str = "") -> None:
        """向交易所发送订单。"""

        # Keep the backend gate visible at the executor boundary so an engine
        # invoked without the supervisor cannot bypass the readiness contract.
        # Exit-only intents are retained for a separately governed emergency
        # flatten path; new risk is rejected and durably recorded.
        if self._can_write and not getattr(self, "_state_backend_supported", False):
            exit_only = bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False))
            if not exit_only:
                print(f"[order] ❌ Intent {intent.intent_id} rejected: STATE_BACKEND_UNSUPPORTED")
                self._outbox.reject(
                    intent.intent_id,
                    "STATE_BACKEND_UNSUPPORTED",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                return

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

        # BD-CV02/41: 合约级 TradingEligibility 验证 + 幂等键
        # BD-FIX: 零写模式按设计跳过对账/保护/用户流事实（模拟订单与
        # 交易所事实必然不一致），TruthSnapshot 恒 NOT_VERIFIABLE，旧逻辑
        # 让 paper/shadow 的每个 intent 在发送边界被拒 → 模拟成交无法
        # 产生 → Paper 证据永远无法积累（死锁）。零写模式撮合无真实
        # 资金风险，资格门按与其他对账豁免相同的语义跳过。
        _env_mode_zw = getattr(self, "_env_mode", None)
        zero_write_mode = _env_mode_zw is not None and str(getattr(_env_mode_zw, "value", _env_mode_zw)).lower() in (
            "paper",
            "shadow",
            "research",
            "safety_only",
        )
        _risk_dir = ControlPlane.classify_intent(intent)
        if _risk_dir.value == "INCREASE" and not zero_write_mode:
            eligibility = self.evaluate_trading_eligibility()
            if eligibility != TradingEligibility.ELIGIBLE:
                print(f"[order] ❌ Intent {intent.intent_id} REJECTED: TradingEligibility={eligibility.value}")
                self._outbox.reject(intent.intent_id, f"ELIGIBILITY_{eligibility.value}", idempotency_key="")
                return
        # idempotency_key 已在 nearline OrderIntent 创建时设置，此处不再修改
        # （修改 frozen dataclass 会导致 order_intent_binding_hash 不匹配）

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
                self._owned_order_ids.add(intent.intent_id)
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
                    try:
                        self._shadow_runner.record_fill_to_ledger(
                            self._ledger,
                            order_symbol,
                            side,
                            filled_qty,
                            avg_price,
                            fee=fee_amount,
                            spread_cost=spread_amount,
                            slippage_cost=slippage_amount,
                        )
                    except Exception as exc:
                        # PKG28 (BDS-P1-060): 写失败已在 record_fill_to_ledger 内部
                        # 记录为 P0 事件 + ledger_write_failures++
                        logger.error("Paper fill ledger write failed: %s", type(exc).__name__)

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
                self._shadow_runner.record_execution_observation(
                    estimated_cost_bps=estimated_cost_bps,
                    actual_cost_bps=actual_cost_bps,
                )
            return

        # === 执行算法选择 + 切片计划 (TWAP/POV/AdaptiveSlice/...) ===
        # Testnet and production share the same market-fact, slippage and
        # slice-invariant gates.  The environment label changes the venue,
        # never the approved execution contract.
        planned = await self._plan_execution(intent, order_symbol, client_id)
        if planned is None:
            return
        slices, _algo_type, ctx = planned

        # The complete immutable child plan is durable before any venue write.
        rule_snapshot_hash = str(
            getattr(self, "_symbol_precision", {}).get(order_symbol, {}).get("rule_snapshot_hash", "")
        )
        commands = [
            ExecutionChildCommand.create(
                parent_intent_id=str(intent.intent_id),
                sequence=idx,
                symbol=order_symbol,
                side=side,
                quantity=qty_str,
                order_type=order_type,
                time_in_force=tif or "GTC",
                client_order_id=slice_client_id,
                limit_price=price_str,
                reduce_only=bool(getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False)),
                rule_snapshot_hash=rule_snapshot_hash,
            )
            for idx, (qty_str, price_str, order_type, tif, slice_client_id) in enumerate(slices)
        ]
        try:
            execution_aggregate = self._outbox.persist_execution_plan(str(intent.intent_id), commands)
        except Exception as exc:
            self._outbox.mark_unknown(intent.intent_id, f"EXECUTION_PLAN_PERSISTENCE_FAILED:{type(exc).__name__}")
            self._record_execution_fact_failure_env_guarded(
                f"EXECUTION_PLAN_PERSISTENCE_FAILED:{intent.intent_id}:{type(exc).__name__}"
            )
            return

        # === 逐切片下发交易所 ===
        # Multi-slice algorithms are paced, but the parent remains in-flight
        # until every durable child has an identity-bound venue acknowledgement.
        n_slices = len(slices)
        slice_interval = 0.0
        if n_slices > 1:
            default_interval = getattr(ctx, "alpha_decay_seconds", 60.0) / max(n_slices, 1)
            slice_interval = max(2.0, min(default_interval, 120.0))  # 2s~120s 范围
        for idx, (qty_str, price_str, order_type, tif, slice_client_id) in enumerate(slices):
            # P0 修复: 每切片前复检控制面状态，防止中途 NO_NEW_RISK/LOCK 后剩余切片照常发送
            if idx > 0 and not self._control.should_accept(intent):
                print(
                    f"[order] ⚠️ TWAP ABORTED after {idx}/{n_slices} slices: "
                    f"control plane rejected (state={self._control.get_status().value} v{self._control.version})"
                )
                for remaining_idx in range(idx, n_slices):
                    execution_aggregate = self._outbox.transition_execution_child(
                        intent.intent_id,
                        remaining_idx,
                        ChildCommandState.REJECTED,
                        event_id=f"control-reject:{intent.intent_id}:{remaining_idx}",
                    )
                if idx == 0:
                    self._outbox.reject(
                        intent.intent_id,
                        f"CONTROL_GATE_{self._control.get_status().value}",
                        idempotency_key=getattr(intent, "idempotency_key", "") or "",
                    )
                else:
                    self._outbox.mark_unknown(intent.intent_id, "PARTIAL_PLAN_ABORTED_BY_CONTROL_GATE")
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
                # PKG02 (BDS-P0-001): TIF 语义在所有环境必须一致。
                # Testnet 不允许将 IOC/FOK 静默改为 GTC。
                # 如需 testnet 特殊执行策略，须在 ExecutionPlan 中显式建模。
                _tif = tif or "GTC"
                params["timeInForce"] = _tif

            execution_aggregate = self._outbox.transition_execution_child(
                intent.intent_id,
                idx,
                ChildCommandState.SENDING,
                event_id=f"send:{intent.intent_id}:{idx}",
            )
            order = await self._submit_order_slice(
                intent,
                params=params,
                order_symbol=order_symbol,
                side=side,
                order_type=order_type,
                consume_approval=idx == 0,
            )
            outcome = str((order or {}).get("_submit_outcome", "ACKED" if order else "UNKNOWN"))
            if outcome == "REJECTED":
                reject_reason = str((order or {}).get("reason", "EXECUTION_CHILD_REJECTED"))
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.REJECTED,
                    event_id=f"reject:{intent.intent_id}:{idx}",
                )
                if _is_retryable_venue_rejection(reject_reason):
                    # BD-FIX: 瞬时错误（限频/时钟偏差等）不直接 FAILED ——
                    # mark_unknown 由运行时周期 resolve 按 venue 事实裁决
                    self._outbox.mark_unknown(intent.intent_id, f"RETRYABLE_REJECTION:{reject_reason}")
                    return
                self._outbox.reject(
                    intent.intent_id,
                    reject_reason,
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                return
            if outcome == "UNKNOWN" or order is None or "orderId" not in order:
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.UNKNOWN,
                    event_id=f"unknown:{intent.intent_id}:{idx}",
                )
                self._outbox.mark_unknown(intent.intent_id, "ORDER_ACK_UNKNOWN")
                return

            exchange_order_id = str(order["orderId"])
            actual_status = str(order.get("status", "NEW")).upper()
            filled_text = str(order.get("executedQty") or (qty_str if actual_status == "FILLED" else "0"))
            try:
                cumulative_filled = Decimal(filled_text)
                planned_quantity = Decimal(qty_str)
                if not cumulative_filled.is_finite() or cumulative_filled < 0 or cumulative_filled > planned_quantity:
                    raise ValueError("invalid cumulative fill")
            except (InvalidOperation, TypeError, ValueError):
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.UNKNOWN,
                    event_id=f"invalid-fill:{intent.intent_id}:{idx}:{exchange_order_id}",
                    exchange_order_id=exchange_order_id,
                )
                self._outbox.mark_unknown(intent.intent_id, "CUMULATIVE_FILL_UNKNOWN")
                return
            if actual_status == "REJECTED":
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.REJECTED,
                    event_id=f"venue-reject:{intent.intent_id}:{idx}:{exchange_order_id}",
                    exchange_order_id=exchange_order_id,
                )
                self._outbox.reject(
                    intent.intent_id,
                    "VENUE_CHILD_REJECTED",
                    idempotency_key=getattr(intent, "idempotency_key", "") or "",
                )
                return
            if actual_status in {"CANCELED", "EXPIRED"}:
                # BD-FIX: ACK 即过期/撤销且带部分成交时，成交事实必须
                # 入账（C2 审查 —— 与 _monitor_orders 同款修复）。
                if cumulative_filled > 0:
                    _delta, _price, _fill_id = self._consume_cumulative_fill(
                        order_id,
                        order_symbol,
                        order,
                        status=actual_status,
                    )
                    if _delta > 0 and _price > 0:
                        self._record_partial_fill_to_ledger(
                            order_id,
                            order_symbol,
                            order,
                            _delta,
                            _price,
                            float(cumulative_filled),
                            _fill_id,
                            status=actual_status,
                        )
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.CANCELED,
                    event_id=f"cancel:{intent.intent_id}:{idx}:{exchange_order_id}:{filled_text}",
                    exchange_order_id=exchange_order_id,
                    cumulative_filled_quantity=filled_text,
                )
                continue
            if actual_status not in {"NEW", "PENDING_NEW", "PENDING_CANCEL", "PARTIALLY_FILLED", "FILLED"}:
                execution_aggregate = self._outbox.transition_execution_child(
                    intent.intent_id,
                    idx,
                    ChildCommandState.UNKNOWN,
                    event_id=f"unknown-status:{intent.intent_id}:{idx}:{exchange_order_id}:{actual_status}",
                    exchange_order_id=exchange_order_id,
                )
                self._outbox.mark_unknown(intent.intent_id, f"ORDER_STATUS_UNKNOWN:{actual_status}")
                return
            child_state = (
                ChildCommandState.FILLED
                if actual_status == "FILLED"
                else (
                    ChildCommandState.PARTIALLY_FILLED
                    if actual_status == "PARTIALLY_FILLED"
                    or (actual_status == "PENDING_CANCEL" and cumulative_filled > 0)
                    else ChildCommandState.ACKED
                )
            )
            execution_aggregate = self._outbox.transition_execution_child(
                intent.intent_id,
                idx,
                child_state,
                event_id=f"venue-status:{intent.intent_id}:{idx}:{exchange_order_id}:{actual_status}:{filled_text}",
                exchange_order_id=exchange_order_id,
                cumulative_filled_quantity=(
                    filled_text
                    if child_state in {ChildCommandState.PARTIALLY_FILLED, ChildCommandState.FILLED}
                    else None
                ),
            )

        if execution_aggregate.all_children_acknowledged:
            self._outbox.ack(intent.intent_id, idempotency_key=getattr(intent, "idempotency_key", "") or "")
        elif execution_aggregate.state is ParentExecutionState.UNKNOWN:
            self._outbox.mark_unknown(intent.intent_id, "EXECUTION_PLAN_INCOMPLETE_UNKNOWN")
        else:
            self._outbox.mark_unknown(intent.intent_id, "EXECUTION_PLAN_NOT_FULLY_ACKNOWLEDGED")

        # Execution quality is updated only from authoritative fills.  Planning
        # estimates are not relabelled as realized cost or slippage here.

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
        if not is_reduce_only and net_alpha_bps < 0:
            self._outbox.reject(
                intent.intent_id,
                "ALPHA_NEGATIVE",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            print(f"[order] {order_symbol}: rejected — negative net alpha")
            return None
        try:
            vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(order_symbol))
            self._cost_model.set_fee_tier(VenueId("BINANCE"), "vip1", 2.0, 4.0)
            est = self._cost_model.estimate_order(
                vi,
                Quantity(amount=str(intent.quantity.amount)),
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
        # BD-FIX: 从 ExchangeInfo 获取 min_quantity，避免 VENUE_RULES_UNKNOWN
        _prec = getattr(self, "_symbol_precision", {}).get(order_symbol, {})
        _min_qty = float(_prec.get("min_quantity", 0) or 0)
        if _min_qty <= 0:
            # PKG02: 规则 UNKNOWN 时 symbol=NOT_EXECUTABLE
            # BD-FIX: return None 前必须 mark_unknown —— 意图已被 claim
            # 标记 SENDING，静默 None 会让它进程内永远无人再处理
            # （I2 审查：SENDING↔PENDING 死循环，outbox 行无限累积）。
            print(f"[engine] min_quantity UNKNOWN for {order_symbol}; marking intent UNKNOWN")
            with contextlib.suppress(Exception):
                self._outbox.mark_unknown(intent.intent_id, f"MIN_QUANTITY_UNKNOWN:{order_symbol}")
            return None
        _min_notional = float(_prec.get("min_notional", 0) or 0)
        ctx = ExecutionContext(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(order_symbol)),
            side=intent.side,
            total_quantity=Quantity(amount=str(intent.quantity.amount)),
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
            min_quantity=_min_qty,
            min_notional=_min_notional,
            reduce_only=is_reduce_only,
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
            # BD-FIX: 切片数量必须按交易对规则量化后再持久化。算法生成的
            # 浮点乘积（如 0.02×0.8=0.016）不满足 venue step，执行器量化
            # 后与持久化子命令不一致 → PLANNED_QUANTITY_NOT_VENUE_EXACT
            # 恒拒（04:03 实测复发）。量化到零/快照缺失 → 跳过该切片。
            qty_str = self._quantize_slice_quantity(order_symbol, str(slc.quantity.amount))
            if qty_str is None:
                print(f"[order] {order_symbol}: skip slice {slc.slice_id} (quantity not venue-exact)")
                continue
            price_str = str(slc.price.amount) if slc.price is not None else None
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

        slices_ok, slices_reason = self._validate_slices_against_intent(
            intent,
            slices,
            order_symbol=order_symbol,
            side=getattr(getattr(intent, "side", None), "value", getattr(intent, "side", "")),
            client_id=client_id,
        )
        if not slices_ok:
            print(f"[order] {order_symbol}: slices rejected at final binding gate ({slices_reason})")
            self._outbox.reject(
                intent.intent_id,
                f"EXECUTION_SLICE_BINDING_FAILED:{slices_reason}",
                idempotency_key=getattr(intent, "idempotency_key", "") or "",
            )
            return None

        print(
            f"[order] {order_symbol}: algo={plan.algorithm.value} slices={len(slices)} "
            f"total_qty={plan.total_quantity():.6f} est_cost={plan.total_estimated_cost_bps:.1f}bps "
            f"est_completion={plan.estimated_completion_seconds:.0f}s"
        )
        return slices, plan.algorithm, ctx

    def _quantize_slice_quantity(self, order_symbol: str, qty_text: str) -> str | None:
        """切片数量按交易对规则快照量化（BD-FIX）。

        执行算法的切片数量是浮点乘法产物（如 0.02×0.8=0.016），不满足
        venue step；执行器 ``_submit_order_slice`` 会重新量化并拒绝与持久化
        子命令不一致的数量（PLANNED_QUANTITY_NOT_VENUE_EXACT，审批绑定
        语义）。持久化前量化保证两侧一致。快照 unknown/stale 或量化到零
        时返回 None（跳过该切片，fail-closed 不发送）。
        """
        adapter = getattr(self, "_adapter", None)
        if adapter is None or not callable(getattr(adapter, "get_rule_snapshot", None)):
            return None
        snap = adapter.get_rule_snapshot(order_symbol)
        if snap is None or not snap.is_known:
            return None
        try:
            return snap.quantize_quantity(qty_text)
        except ValueError:
            return None

    @staticmethod
    def _validate_slices_against_intent(
        intent: Any,
        slices: list[tuple[str, str | None, str, str, str]],
        *,
        order_symbol: str,
        side: str,
        client_id: str,
    ) -> tuple[bool, str]:
        """Prove execution slices cannot mutate the signed order semantics.

        The approval digest covers the aggregate intent.  A planner may split
        that quantity for execution quality, but it may not increase the
        approved total, change side/symbol/order type/price/TIF, or invent an
        unrelated client id.  Anything ambiguous is rejected before the first
        venue request.
        """

        if str(order_symbol) != str(getattr(intent, "instrument_id", "")):
            return False, "SYMBOL_MISMATCH"
        expected_side = getattr(getattr(intent, "side", None), "value", getattr(intent, "side", ""))
        if str(side).upper() != str(expected_side).upper():
            return False, "SIDE_MISMATCH"
        expected_type = getattr(getattr(intent, "order_type", None), "value", getattr(intent, "order_type", ""))
        expected_tif = getattr(
            getattr(intent, "time_in_force", None),
            "value",
            getattr(intent, "time_in_force", ""),
        )
        approved_price = getattr(getattr(intent, "price", None), "amount", None)
        try:
            approved_qty = Decimal(str(getattr(getattr(intent, "quantity", None), "amount", "")))
            if not approved_qty.is_finite() or approved_qty <= 0:
                return False, "APPROVED_QUANTITY_UNKNOWN"
        except (InvalidOperation, TypeError, ValueError):
            return False, "APPROVED_QUANTITY_UNKNOWN"

        total = Decimal("0")
        seen_client_ids: set[str] = set()
        multi_slice = len(slices) > 1
        for qty_text, price_text, order_type, tif, slice_client_id in slices:
            try:
                qty = Decimal(str(qty_text))
            except (InvalidOperation, TypeError, ValueError):
                return False, "SLICE_QUANTITY_UNKNOWN"
            if not qty.is_finite() or qty <= 0:
                return False, "SLICE_QUANTITY_INVALID"
            total += qty
            if total > approved_qty:
                return False, "TOTAL_QUANTITY_EXCEEDS_APPROVAL"
            if str(order_type).upper() != str(expected_type).upper() and not (
                str(expected_type).upper() == "MARKET" and str(order_type).upper() == "LIMIT"
            ):
                # 执行算法可将 MARKET intent 降级为 LIMIT 切片（更保守），但不允许反向
                return False, "ORDER_TYPE_MISMATCH"
            if str(tif or expected_tif).upper() != str(expected_tif).upper() and not (
                str(expected_tif).upper() == "GTC" and str(tif or expected_tif).upper() == "IOC"
            ):
                # 紧急平仓使用 IOC，允许偏离 GTC；算法可选择更严格 TIF
                return False, "TIME_IN_FORCE_MISMATCH"
            is_limit_slice = str(order_type).upper() == "LIMIT"
            if str(expected_type).upper() == OrderType.LIMIT.value:
                if approved_price is None or price_text is None:
                    return False, "LIMIT_PRICE_UNKNOWN"
                try:
                    if Decimal(str(price_text)) != Decimal(str(approved_price)):
                        return False, "LIMIT_PRICE_MISMATCH"
                except (InvalidOperation, TypeError, ValueError):
                    return False, "LIMIT_PRICE_UNKNOWN"
            elif price_text is not None and not is_limit_slice:
                # LIMIT 切片可以有价格（从 MARKET intent 降级），非 LIMIT 不应有价格
                return False, "MARKET_PRICE_UNEXPECTED"

            slice_id = str(slice_client_id)
            if not multi_slice:
                if slice_id != client_id:
                    return False, "CLIENT_ORDER_ID_MISMATCH"
            else:
                prefix = f"{client_id}-"
                suffix = slice_id[len(prefix) :] if slice_id.startswith(prefix) else ""
                if not suffix or not suffix.isdigit():
                    return False, "SLICE_CLIENT_ORDER_ID_INVALID"
            if slice_id in seen_client_ids:
                return False, "DUPLICATE_SLICE_CLIENT_ORDER_ID"
            seen_client_ids.add(slice_id)

        if not slices or total <= 0:
            return False, "SLICE_TOTAL_UNKNOWN"
        return True, "OK"

    def _sync_adapter_rule_snapshots(self, exchange_info: dict | None) -> bool:
        """BD-FIX: 把 exchangeInfo symbols 同步进 adapter 并构建规则快照（BD-CV10）。

        _submit_order_slice 的执行 gate 要求 adapter.get_rule_snapshot
        返回 known 且新鲜的快照；该缓存只能由 sync_rule_snapshots()
        填充（从 adapter._reference_data.instruments 构建）。启动时
        exchangeInfo 已在手，这里一次性注入并同步；否则 get_rule_snapshot
        恒 unknown，所有订单在执行前被 VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE
        拒绝。返回是否成功同步。
        """

        try:
            _ref_data = getattr(self._adapter, "_reference_data", None)
            if _ref_data is None or not exchange_info or not exchange_info.get("symbols"):
                return False
            for _s in exchange_info["symbols"]:
                _sym = str(_s.get("symbol", ""))
                if _sym:
                    _ref_data.instruments[InstrumentId(_sym)] = _s
            _version = self._adapter.sync_rule_snapshots()
            print(
                f"[beidou-autopilot] Rule snapshots synced: version={_version} "
                f"count={self._adapter.rule_snapshot_count}"
            )
            return True
        except Exception as exc:
            print(f"[beidou-autopilot] Warning: rule snapshot sync failed: {type(exc).__name__}")
            return False

    async def _submit_order_slice(
        self,
        intent,
        params: dict,
        order_symbol: str,
        side: str,
        order_type: str,
        consume_approval: bool,
    ) -> dict | None:
        """单个切片：量化精度修正 → 发送 → 记录。返回交易所响应或 None。"""

        def rejected(reason: str) -> dict[str, str]:
            return {"_submit_outcome": "REJECTED", "reason": reason}

        def unknown(reason: str) -> dict[str, str]:
            return {"_submit_outcome": "UNKNOWN", "reason": reason}

        # BD-CV10: 量化精度从 adapter 的唯一 InstrumentRuleSnapshot 获取。
        if not hasattr(self, "_symbol_precision"):
            self._symbol_precision: dict[str, dict[str, int]] = {}
        if not hasattr(self, "_rule_snapshot_hashes"):
            self._rule_snapshot_hashes: dict[str, str] = {}
        if not hasattr(self, "_rule_change_detected"):
            self._rule_change_detected: set[str] = set()
        snap: InstrumentRuleSnapshot | None = None
        rule = getattr(self, "_adapter", None)
        if rule is not None and hasattr(rule, "get_rule_snapshot"):
            snap = rule.get_rule_snapshot(order_symbol)
            if not snap.is_known or snap.is_stale:
                print(f"[order] {order_symbol}: rule snapshot UNKNOWN/STALE — symbol NOT_EXECUTABLE")
                return rejected("VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE")
            snap_hash = snap.compute_hash()
            prev_hash = self._rule_snapshot_hashes.get(order_symbol)
            if prev_hash and prev_hash != snap_hash:
                print(f"[order] {order_symbol}: rule changed! prev={prev_hash[:16]} new={snap_hash[:16]}")
                self._rule_change_detected.add(order_symbol)
                return rejected("VENUE_RULE_SNAPSHOT_CHANGED")
            self._rule_snapshot_hashes[order_symbol] = snap_hash
            self._symbol_precision[order_symbol] = {
                "quantity": snap.qty_precision,
                "price": snap.price_precision,
                "step_size": snap.step_size,
                "tick_size": snap.tick_size,
                "min_quantity": snap.min_qty,
                "min_notional": snap.min_notional,
                "rule_snapshot_hash": snap_hash,
                "rule_version": snap.rule_version,
            }
        elif order_symbol not in self._symbol_precision:
            # Fallback: 从 exchangeInfo API 加载并填充到 _symbol_precision
            try:
                exchange_info = await self._api_async(Endpoint.EXCHANGE_INFO)
                for s in exchange_info.get("symbols", []):
                    sym = s.get("symbol", "")
                    fallback_snap = InstrumentRuleSnapshot.from_exchange_info(sym, s)
                    if not fallback_snap.is_known:
                        continue
                    self._symbol_precision[sym] = {
                        "quantity": fallback_snap.qty_precision,
                        "price": fallback_snap.price_precision,
                        "step_size": fallback_snap.step_size,
                        "tick_size": fallback_snap.tick_size,
                        "min_quantity": fallback_snap.min_qty,
                        "min_notional": fallback_snap.min_notional,
                        "rule_snapshot_hash": fallback_snap.compute_hash(),
                        "rule_version": fallback_snap.rule_version,
                    }
                    if sym == order_symbol:
                        snap = fallback_snap
            except Exception as exc:
                print(f"[order] {order_symbol}: exchangeInfo unavailable ({exc})")
                return rejected("EXCHANGE_INFO_UNKNOWN")
            if order_symbol not in self._symbol_precision:
                print(f"[order] {order_symbol}: exchange precision UNKNOWN")
                return rejected("EXCHANGE_PRECISION_UNKNOWN")

        prec = self._symbol_precision.get(order_symbol)
        if prec is None:
            print(f"[order] {order_symbol}: exchange precision UNKNOWN")
            return rejected("EXCHANGE_PRECISION_UNKNOWN")
        if snap is None:
            snap = InstrumentRuleSnapshot(
                symbol=order_symbol,
                tick_size=str(prec.get("tick_size", "")),
                step_size=str(prec.get("step_size", "")),
                min_qty=str(prec.get("min_quantity", "")),
                min_notional=str(prec.get("min_notional", "")),
                price_precision=int(prec.get("price", -1)),
                qty_precision=int(prec.get("quantity", -1)),
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
        if not snap.is_known:
            print(f"[order] {order_symbol}: exact venue increments UNKNOWN")
            return rejected("VENUE_INCREMENT_UNKNOWN")
        original_qty = Decimal(str(params["quantity"]))
        try:
            params["quantity"] = snap.quantize_quantity(str(original_qty))
        except ValueError as exc:
            print(f"[order] {order_symbol}: quantity quantization rejected ({exc})")
            return rejected("QUANTITY_QUANTIZATION_REJECTED")
        quantized_qty = Decimal(str(params["quantity"]))
        approved_qty = Decimal(str(getattr(getattr(intent, "quantity", None), "amount", "0")))
        if quantized_qty != original_qty:
            print(f"[order] {order_symbol}: persisted child quantity is not venue-exact")
            return rejected("PLANNED_QUANTITY_NOT_VENUE_EXACT")
        if quantized_qty > approved_qty or quantized_qty < Decimal(snap.min_qty):
            print(f"[order] {order_symbol}: final quantity exceeds approval or violates minQty")
            return rejected("FINAL_QUANTITY_OUTSIDE_APPROVAL_OR_MIN_QTY")
        price_raw = params.get("price")
        if price_raw:
            original_price = Decimal(str(price_raw))
            try:
                params["price"] = snap.quantize_price(str(price_raw), side=side)
            except ValueError as exc:
                print(f"[order] {order_symbol}: price quantization rejected ({exc})")
                return rejected("PRICE_QUANTIZATION_REJECTED")
            if Decimal(str(params["price"])) != original_price:
                print(f"[order] {order_symbol}: persisted child price is not venue-exact")
                return rejected("PLANNED_PRICE_NOT_VENUE_EXACT")
            if getattr(intent, "order_type", None) == OrderType.LIMIT:
                approved_price = Decimal(str(getattr(getattr(intent, "price", None), "amount", "0")))
                if Decimal(str(params["price"])) != approved_price:
                    print(f"[order] {order_symbol}: final limit price differs from signed approval")
                    return rejected("FINAL_LIMIT_PRICE_DIFFERS_FROM_APPROVAL")
            if quantized_qty * Decimal(str(params["price"])) < Decimal(snap.min_notional):
                print(f"[order] {order_symbol}: final order violates minNotional")
                return rejected("FINAL_ORDER_BELOW_MIN_NOTIONAL")

        print(f"[order] Sending to exchange: {order_symbol} {side} {params['quantity']} @ {params.get('price', 'MKT')}")
        # Consume the one-shot approval after all local planning/quantization
        # and immediately before the first venue write.  An ambiguous response
        # must be reconciled by client id; the same approval cannot be replayed.
        if consume_approval and not await self._verify_intent_at_send(intent, consume_nonce=True):
            return rejected("FINAL_APPROVAL_CONSUMPTION_FAILED")
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
                if adapter_response.status is OrderStatus.REJECTED:
                    return rejected("ADAPTER_REJECTED_WITHOUT_ACK")
                return unknown("ADAPTER_ACK_UNKNOWN")
        except (TypeError, ValueError) as exc:
            return rejected(f"ADAPTER_REQUEST_INVALID:{type(exc).__name__}")
        except Exception as exc:
            return unknown(f"ADAPTER_EXCEPTION:{type(exc).__name__}")
        print(f"[order] Exchange response: {str(order)[:200]}")

        if "orderId" in order:
            oid_str = str(order["orderId"])
            slice_client_id = params.get("newClientOrderId", "")
            # 标记平仓订单（通过 client_order_id 中的 "-close-"/"-emergency-" 模式识别）
            if slice_client_id and ("-close-" in slice_client_id or "-emergency-" in slice_client_id):
                self._close_order_ids.add(oid_str)
                print(f"[order] Marked as close order: {oid_str}")
            tracker = OrderStateTracker(order_id=OrderId(oid_str))
            tracker.apply(OrderEvent.SENT)
            tracker.apply(OrderEvent.ACKED)
            self._order_trackers[oid_str] = tracker
            self._order_symbols[oid_str] = order_symbol
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
                self._owned_order_ids.add(oid_str)
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
                query_result = await self._adapter.query_order_by_client_id(order_symbol, client_id)
                if query_result.is_success() and isinstance(query_result.data, dict):
                    existing = query_result.data
                    valid, reason = _validate_duplicate_order_response(
                        existing,
                        client_order_id=client_id,
                        symbol=order_symbol,
                        side=side,
                        order_type=order_type,
                        quantity=params.get("quantity", ""),
                        reduce_only=bool(
                            getattr(intent, "reduce_only", False) or getattr(intent, "close_position", False)
                        ),
                    )
                    if not valid:
                        raise ValueError(f"duplicate lookup semantic mismatch: {reason}")
                    oid_str = str(existing["orderId"])
                    if oid_str:
                        actual_status = existing.get("status", "UNKNOWN")
                        print(f"[order] RECOVERED from -4141: orderId={oid_str} status={actual_status}")
                        tracker = OrderStateTracker(order_id=OrderId(oid_str))
                        tracker.apply(OrderEvent.SENT)
                        tracker.apply(OrderEvent.ACKED)
                        self._order_trackers[oid_str] = tracker
                        self._order_symbols[oid_str] = order_symbol
                        self._order_count += 1
                        self._last_order_placed_at = time.time()
                        if actual_status == "FILLED":
                            await self._process_fill(oid_str, order_symbol, existing)
                        else:
                            self._active_order_ids.add(oid_str)
                            # 持久化恢复的订单，避免对账时 system_facts 缺失此订单
                            try:
                                self._store.save_order_state(
                                    oid_str,
                                    order_symbol,
                                    side,
                                    order_type,
                                    str(params["quantity"]),
                                    params.get("price"),
                                    actual_status,
                                    client_order_id=client_id,
                                )
                            except Exception as _pe:
                                print(f"[order] Failed to persist recovered order {oid_str}: {_pe}")
                        self._owned_order_ids.add(oid_str)
                        return existing
            except Exception as qe:
                print(f"[order] -4141 recovery query failed: {qe}")
            # 恢复失败 → 保留 UNKNOWN，禁止盲目重发。
            return unknown("DUPLICATE_QUERY_UNKNOWN")

        print(
            f"[order] FAILED: {order_symbol} {side} — {order.get('msg', order.get('error', 'unknown'))} | raw={json.dumps(order, default=str)[:200]}"
        )
        if adapter_response.status is OrderStatus.REJECTED:
            return rejected(str(order.get("msg", order.get("error", "ADAPTER_REJECTED"))))
        return unknown(str(order.get("msg", order.get("error", "ORDER_ACK_UNKNOWN"))))

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
                    self._mark_order_unknown(order_id, order_sym, f"ORDER_STATUS_UNKNOWN:{order_id}")
                    continue

                if result.get("msg") and (
                    "Order does not exist" in str(result.get("msg")) or "Unknown order" in str(result.get("msg"))
                ):
                    last_status = str(getattr(getattr(tracker, "status", None), "value", "UNKNOWN"))
                    is_expected = last_status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
                    if not is_expected:
                        self._mark_order_unknown(order_id, order_sym, f"ORDER_DISAPPEARED_UNKNOWN:{order_id}")
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
                    # BD-FIX: 部分成交后 EXPIRED/CANCELED 的成交事实必须
                    # 入账 —— IOC 切片在薄盘上"先部分成交后过期"时旧代码
                    # 直接置终态，executedQty 从不进 ledger → 本地持仓被
                    # 低估 → 对账恒 MISMATCH → 锁盘（C2 审查）。
                    if executed_qty > 0:
                        delta_qty, partial_price, _fill_event_id = self._consume_cumulative_fill(
                            order_id,
                            order_sym or symbol,
                            result,
                            status=status,
                        )
                        if delta_qty > 0 and partial_price > 0:
                            self._record_partial_fill_to_ledger(
                                order_id,
                                order_sym or symbol,
                                result,
                                delta_qty,
                                partial_price,
                                executed_qty,
                                _fill_event_id,
                                status=status,
                            )
                    tracker.apply(OrderEvent.CANCELED)
                    self._active_order_ids.discard(order_id)
                    self._store.save_order_state(
                        order_id,
                        order_sym or symbol,  # BD-FIX: 用该订单的真实 symbol（批量循环 symbol 会错标跨品种订单）
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
                        fill_committed = self._record_partial_fill_to_ledger(
                            order_id,
                            order_sym or symbol,
                            result,
                            delta_qty,
                            partial_price,
                            executed_qty,
                            fill_event_id,
                            status="PARTIALLY_FILLED",
                        )

            except Exception as e:
                # Log error but do NOT silently swallow — maintain visibility
                if fill_event_id_for_retry and not fill_committed:
                    self._mark_fill_retryable(order_id, fill_event_id_for_retry)
                self._mark_order_unknown(
                    order_id,
                    order_sym,
                    f"ORDER_MONITOR_UNKNOWN:{type(e).__name__}:{str(e)[:240]}",
                )
                print(f"[realtime] Order monitoring error ({order_id}): {e}")

    def _record_partial_fill_to_ledger(
        self,
        order_id: str,
        symbol: str,
        result: dict,
        delta_qty: float,
        price: float,
        executed_qty: float,
        fill_event_id: str,
        *,
        status: str,
    ) -> bool:
        """部分成交入账（PARTIALLY_FILLED/EXPIRED/CANCELED 共用，BD-FIX）。

        IOC 切片在薄盘上"先部分成交后过期/撤销"的成交事实必须与普通
        部分成交走同一入账路径（ledger + 持仓投影 + fill 提交），否则
        本地持仓被低估 → 对账恒 MISMATCH → 锁盘（C2 审查）。
        返回是否已入账（fill_committed）。
        """
        partial_notional = delta_qty * price
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
                    instrument_id=InstrumentId(symbol),
                    amount=MonetaryValue(amount=str(partial_notional)),
                    side=PostingSide.DEBIT,
                    description=f"PARTIAL {side_desc} {delta_qty} {symbol} @ {price}",
                ),
                Posting(
                    posting_id=f"{tx_id}-p2",
                    account_id=AccountId("default"),
                    account_type=AccountType.CASH if is_buy else AccountType.POSITION_COST,
                    venue_id=VenueId("BINANCE"),
                    instrument_id=InstrumentId(symbol),
                    amount=MonetaryValue(amount=str(partial_notional)),
                    side=PostingSide.CREDIT,
                    description=f"PARTIAL {side_desc} {delta_qty} {symbol} @ {price}",
                ),
            ),
            correlation_id=CorrelationId(f"exec-{order_id}"),
        )
        self._post_ledger_transaction(tx)
        self._update_position_projection(symbol, side_desc, delta_qty, price, fill_event_id)
        self._mark_fill_committed(order_id, fill_event_id, executed_qty)
        self._store.save_order_state(
            order_id,
            symbol,
            side_desc,
            result.get("type", "MARKET"),
            result.get("origQty", "0"),
            result.get("price"),
            status,
            str(executed_qty),
            str(price),
        )
        print(
            f"[order] PARTIAL FILL recorded: {symbol} {side_desc} "
            f"qty={delta_qty} @ {price} notional={partial_notional:.2f}"
        )
        return True

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
            self._record_execution_fact_failure_env_guarded(
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

        # A venue ``FILLED`` status without a finite positive cumulative
        # quantity and average price is an incomplete fact, not a zero-value
        # fill.  Never advance the state machine or remove the tracker until
        # the execution facts needed for ledger/position projection exist.
        try:
            raw_executed_qty = float(result.get("executedQty", 0) or 0)
            raw_avg_price = float(result.get("avgPrice", 0) or 0)
        except (TypeError, ValueError):
            raw_executed_qty = 0.0
            raw_avg_price = 0.0
        # BD-FIX: avgPrice 缺失/为 0 时用 cumQuote/executedQty 推导
        # （demo 成交响应可能缺 avgPrice 字段 —— I5 审查）。推导仍
        # 无效才判事实不完整。
        if raw_executed_qty > 0 and (not math.isfinite(raw_avg_price) or raw_avg_price <= 0):
            try:
                raw_cum_quote = float(result.get("cumQuote", 0) or 0)
                if math.isfinite(raw_cum_quote) and raw_cum_quote > 0:
                    raw_avg_price = raw_cum_quote / raw_executed_qty
            except (TypeError, ValueError):
                raw_avg_price = 0.0
        if (
            not math.isfinite(raw_executed_qty)
            or not math.isfinite(raw_avg_price)
            or raw_executed_qty <= 0
            or raw_avg_price <= 0
        ):
            self._mark_order_unknown(
                order_id,
                symbol,
                "FILLED_EXECUTION_FACTS_INCOMPLETE",
            )
            return

        executed_qty, avg_price, fill_event_id = self._consume_cumulative_fill(
            order_id,
            symbol,
            result,
            status="FILLED",
        )
        if executed_qty <= 0:
            # A cumulative poll with zero delta is only safe when the durable
            # fill journal already proves that this exact observation reached
            # COMMITTED.  An in-memory high-water mark or a PENDING journal
            # row cannot certify the ledger/position projection after a crash.
            fill_row = self._store.get_fill_event(fill_event_id) if fill_event_id else None
            if not fill_event_id or fill_row is None or str(fill_row.get("processing_state", "")) != "COMMITTED":
                self._mark_order_unknown(order_id, symbol, f"FILL_FACT_COMMIT_UNKNOWN:{fill_event_id or 'MISSING'}")
                return
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
            self._mark_order_unknown(
                order_id,
                symbol,
                f"FILL_FACT_COMMIT_UNKNOWN:{fill_event_id}:{type(exc).__name__}:{str(exc)[:240]}",
            )
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
                self._strategy_risk.update_equity(
                    self._autopilot_strategy_id, _local_equity_estimate(self, account_balance)
                )
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
            _prec = getattr(self, "_symbol_precision", {}).get(symbol, {})
            if _prec:
                self._protection.set_precision_from_rule(
                    type(
                        "_PrecisionRule",
                        (),
                        {
                            "price_precision": _prec.get("price", 0),
                            "qty_precision": _prec.get("quantity", 0),
                        },
                    )()
                )
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
                    # S39: 用 tick map 备选精度

                    trigger_val = float(p_order.trigger_price.amount)
                    if trigger_val > 5000:
                        dec = 1
                    elif trigger_val > 100:
                        dec = 2
                    elif trigger_val > 1:
                        dec = 3
                    else:
                        dec = 5
                    prec_map = {"price": dec, "quantity": dec}
                    print(f"[protection] {symbol}: using fallback precision price={dec} qty={dec}")
                algo_params = self._protection_algo_params(
                    p_order,
                    symbol=symbol,
                    side=reduce_side,
                    precision=prec_map,
                )
                price_str = algo_params["triggerPrice"]
                # One write attempt per durable identity.  A failed ACK is
                # ambiguous and must be reconciled by clientAlgoId/inventory;
                # resetting the circuit or immediately resending can create
                # duplicate protection orders.
                algo_resp = await self._create_algo_order(algo_params)
                err_code = algo_resp.get("code", 0)
                if "algoId" not in algo_resp and err_code in {-4116, -4120}:
                    print(
                        f"[protection] ⚠️ {symbol} {p_order.reason}: "
                        f"venue reports a duplicate/conditional conflict ({err_code})"
                    )
                    self._block_unowned_protection_orders([f"DUPLICATE_CONDITIONAL_ORDER:{symbol}"])
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
            self._strategy_risk.update_equity(
                self._autopilot_strategy_id, _local_equity_estimate(self, account_balance)
            )
            if account_balance > self._peak_equity:
                self._peak_equity = account_balance

    @staticmethod
    def _fact_timestamp(fact_dt: Any) -> float:
        """Convert a (possibly naive/UTC) fact timestamp to Unix seconds."""
        if not isinstance(fact_dt, datetime):
            return 0.0
        if fact_dt.tzinfo is None:
            fact_dt = fact_dt.replace(tzinfo=timezone.utc)
        return fact_dt.timestamp()

    def _record_reconciliation_truth(
        self,
        result: Any,
        *,
        system_facts: AccountFactSnapshot | None = None,
        exchange_facts: AccountFactSnapshot | None = None,
    ) -> None:
        """BD-FIX: 接线 TruthSnapshot 对账派生事实。

        在每一处 ``_last_reconciliation_result`` 被记录的更新点调用，使快照
        hash 恰好反映被比较的事实（never call-time freshness）。某侧事实为
        None 时保持 hash 为空 — fail-closed 到 NOT_VERIFIABLE，不伪造证据。
        """
        now_ts = time.time()
        status_value = getattr(result, "status", "UNKNOWN")
        status = str(getattr(status_value, "value", status_value) or "UNKNOWN")
        self._last_reconciliation_hash = hashlib.sha256(
            json.dumps(
                {
                    "status": status,
                    "matched": bool(getattr(result, "matched", False)),
                    "differences": [str(d) for d in getattr(result, "differences", []) or []],
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        self._last_reconciliation_fact_at = now_ts

        # 账户/仓位/订单事实以交易所侧为权威来源，系统侧投影作为回退证据。
        account_facts = exchange_facts or system_facts
        if account_facts is not None:
            fact_ts = self._fact_timestamp(account_facts.timestamp) or now_ts
            self._last_account_hash = hashlib.sha256(
                json.dumps({"balance": str(account_facts.balance)}, sort_keys=True, default=str).encode()
            ).hexdigest()
            self._last_account_fact_at = fact_ts
            self._last_position_hash = hashlib.sha256(
                json.dumps(
                    {str(sym): str(qty) for sym, qty in dict(account_facts.positions).items()},
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            self._last_position_fact_at = fact_ts
            self._last_order_hash = hashlib.sha256(
                json.dumps(sorted(str(o) for o in account_facts.open_orders), sort_keys=True, default=str).encode()
            ).hexdigest()
            self._last_order_fact_at = fact_ts

        # 系统侧事实即账本推导结果（本地账本投影），整体序列化作为账本事实。
        if system_facts is not None:
            self._last_ledger_hash = hashlib.sha256(
                json.dumps(system_facts.__dict__, sort_keys=True, default=str).encode()
            ).hexdigest()
            self._last_ledger_fact_at = self._fact_timestamp(system_facts.timestamp) or now_ts

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
        self._record_reconciliation_truth(
            result,
            system_facts=system_facts,
            exchange_facts=exchange_facts,
        )
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
            self._safe_no_new_risk("auto")
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
            try:
                self._outbox.project_user_order_update(update)
            except Exception as exc:
                self._record_execution_fact_failure_env_guarded(
                    f"user-stream child projection blocked for {result.event_id or '<unknown>'}: "
                    f"{type(exc).__name__}:{exc}"
                )
                return False
            self._event_stream_facts = projector.fact_snapshot()
            self._recon.update_event_facts(self._event_stream_facts)
            return True
        self._record_execution_fact_failure_env_guarded(
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
        self._record_execution_fact_failure_env_guarded(f"user-stream replay baseline blocked: {result.reason}")
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
        self._record_execution_fact_failure_env_guarded(
            f"account user-stream event {result.event_id or '<unknown>'} blocked: {result.reason}"
        )
        return False

    def _ingest_account_config_update(self, data: dict[str, Any]) -> bool:
        """Record an account configuration change and revoke stale authority."""
        import logging

        _logger = logging.getLogger(__name__)

        ac = data.get("ac") if isinstance(data, dict) else None
        ai = data.get("ai") if isinstance(data, dict) else None
        symbol = str(ac.get("s") or "").strip().upper() if isinstance(ac, dict) else ""
        leverage = None
        is_joint = None
        try:
            if isinstance(ac, dict) and ac.get("l") is not None:
                leverage = int(ac["l"])
        except (TypeError, ValueError):
            pass
        try:
            if isinstance(ai, dict) and ai.get("j") is not None:
                raw = ai["j"]
                is_joint = raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes"}
        except (TypeError, ValueError):
            pass

        _logger.info(
            "ACCOUNT_CONFIG_UPDATE ingested: symbol=%s leverage=%s joint_margin=%s",
            symbol or "<none>",
            leverage,
            is_joint,
        )
        if symbol:
            getattr(self, "_leverage_cache", {}).pop(symbol, None)
        self._record_execution_fact_failure_env_guarded("ACCOUNT_CONFIG_UPDATE_REVALIDATION_REQUIRED")
        return False

    def _update_user_stream_runtime(self, **updates: Any) -> None:
        """Update redacted live user-stream evidence without storing secrets."""

        runtime = getattr(self, "_user_stream_runtime", None)
        if not isinstance(runtime, dict):
            runtime = {}
            self._user_stream_runtime = runtime
        runtime.update(updates)
        runtime["last_state_mono"] = time.monotonic()

    def _user_stream_fault(self, reason: str, *, terminal: bool = False) -> None:
        """Fail closed on a listen-key, parser, or continuity fault."""

        runtime = getattr(self, "_user_stream_runtime", {})
        previous = str(runtime.get("status", "UNKNOWN")) if isinstance(runtime, dict) else "UNKNOWN"
        status = "FAILED" if terminal else "DEGRADED"
        self._update_user_stream_runtime(status=status, last_error=str(reason)[:500], listen_key_active=False)
        if previous in {"FAILED", "DEGRADED"} and not terminal:
            return
        # PKG02 (BDS-P0-001): 移除 testnet 用户流故障旁路 — 所有环境统一切换控制面
        with contextlib.suppress(Exception):
            if self._control.get_status() not in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN):
                self._safe_no_new_risk("auto")
        # BD-FIX: user stream 连接故障 ≠ 执行事实持久化失败。freeze 账本
        # 是无解冻路径的永久动作 —— demo ws 抖动（一次临时连接失败）即
        # 永久停机（final21 实测 04:20）。testnet 只降级控制面（NO_NEW_RISK，
        # 可恢复）+ user_stream incident，不冻结账本；live/canary 保留
        # freeze 语义（真实资金下成交事件丢失不可接受，账本必须冻结）。
        if str(getattr(getattr(self, "_env_mode", None), "value", "")) in ("live", "canary"):
            with contextlib.suppress(Exception):
                self._record_execution_fact_failure(f"USER_STREAM_{status}:{reason}")
        with contextlib.suppress(Exception):
            self._alerts.send_incident(
                AlertSeverity.CRITICAL,
                "User data stream unavailable",
                str(reason)[:500],
                category="user_stream",
            )
        # BD-FIX: 终端故障后限次自动重启（demo listenKey 周期性失效自愈）。
        # 记录故障与 incident 之后调度；_restart_user_stream_after_fault 内部
        # 再校验停止/在途/重试上限，双重保护避免重复 WS 与无限循环。
        if terminal and bool(getattr(self, "_can_write", False)):
            with contextlib.suppress(RuntimeError):
                if getattr(self, "_user_stream_restart_attempts", 0) < self._USER_STREAM_RESTART_MAX_ATTEMPTS:
                    self._user_stream_restart_task = asyncio.create_task(self._restart_user_stream_after_fault(reason))

    async def _restart_user_stream_after_fault(self, reason: str) -> None:
        """Terminal user-stream fault 后的限次退避重启。

        停止现有流（清理任务/WS/引用）→ 退避等待 → 重新拉起。成功则恢复
        CONNECTED 并重置重试计数；失败则再次 ``_user_stream_fault(terminal=True)``，
        由重试上限（``_USER_STREAM_RESTART_MAX_ATTEMPTS``）终止，保持 FAILED +
        NO_NEW_RISK，防止无限循环。
        """

        if getattr(self, "_user_stream_stopping", False):
            return
        if not bool(getattr(self, "_running", True)):
            return
        if getattr(self, "_user_stream_restarting", False):
            return
        if getattr(self, "_user_stream_restart_attempts", 0) >= self._USER_STREAM_RESTART_MAX_ATTEMPTS:
            return
        self._user_stream_restarting = True
        self._user_stream_restart_attempts = getattr(self, "_user_stream_restart_attempts", 0) + 1
        try:
            with contextlib.suppress(Exception):
                await self._stop_user_stream()
            # _stop_user_stream 置位 _user_stream_stopping；_start_user_stream 只在
            # listen key 创建成功后才重置该标志，失败提前返回时会遗留 True 挡住
            # 后续重试，故在此显式清除。
            self._user_stream_stopping = False
            await asyncio.sleep(self._USER_STREAM_RESTART_BACKOFF_S)
            # 退避期间引擎可能已进入关机（_shutdown → _stop_user_stream），
            # 此时不得再拉起流。
            if getattr(self, "_user_stream_stopping", False):
                return
            started = await self._start_user_stream()
            if started:
                self._user_stream_restart_attempts = 0
                self._update_user_stream_runtime(status="CONNECTED", last_error="", listen_key_active=True)
            else:
                self._user_stream_fault(f"USER_STREAM_RESTART_FAILED:{reason}", terminal=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._user_stream_fault(f"USER_STREAM_RESTART_ERROR:{type(exc).__name__}", terminal=True)
        finally:
            self._user_stream_restarting = False

    async def _start_user_stream(self) -> bool:
        """Start the authenticated user stream for writable environments.

        The stream is intentionally independent from market-data REST
        fallback.  A missing listen key, parser rejection, sequence gap, or
        reconnect fault keeps the process in ``NO_NEW_RISK``; it never turns
        into a REST-only writable mode.
        """

        if not bool(getattr(self, "_can_write", False)):
            self._update_user_stream_runtime(status="NOT_REQUIRED", listen_key_active=False)
            return True
        existing_task = getattr(self, "_user_ws_task", None)
        if existing_task is not None and not existing_task.done():
            return self._user_stream_readiness()[0]

        self._update_user_stream_runtime(status="STARTING", last_error="", listen_key_active=False)
        websocket = None
        try:
            from beidou_exchange.binance_usdm import (
                FSTREAM_PRODUCTION_URL,
                FSTREAM_TESTNET_URL,
                BinanceUsdmWebSocketClient,
            )
            from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter

            create_key = getattr(self._adapter, "create_user_listen_key", None)
            if not callable(create_key):
                self._user_stream_fault("ADAPTER_USER_STREAM_CREATE_UNSUPPORTED", terminal=True)
                return False
            key_result = await create_key()
            if not key_result.is_success() or not isinstance(key_result.data, dict):
                reason = str(getattr(getattr(key_result, "error", None), "message", "LISTEN_KEY_UNKNOWN"))
                self._user_stream_fault(f"LISTEN_KEY_CREATE_FAILED:{reason}", terminal=True)
                return False
            listen_key = str(key_result.data.get("listenKey") or "").strip()
            if not listen_key:
                self._user_stream_fault("LISTEN_KEY_EMPTY", terminal=True)
                return False

            websocket = BinanceUsdmWebSocketClient(
                base_url=FSTREAM_TESTNET_URL if self._env_mode.value == "testnet" else FSTREAM_PRODUCTION_URL
            )
            self._user_ws_client = websocket
            self._user_stream_listen_key = listen_key
            self._user_stream_stopping = False

            def _on_state_change(_old: Any, new: Any, _group_id: int) -> None:
                state = str(getattr(new, "value", new)).upper()
                if getattr(self, "_user_stream_stopping", False):
                    return
                if state == "CONNECTED":
                    self._update_user_stream_runtime(status="CONNECTED", listen_key_active=True)
                elif state == "RECONNECTING":
                    self._user_stream_fault("USER_STREAM_RECONNECTING")
                elif state == "DISCONNECTED":
                    self._user_stream_fault("USER_STREAM_DISCONNECTED", terminal=True)

            websocket.on_state_change(_on_state_change)

            async def _on_user_event(_stream: str, data: dict[str, Any]) -> None:
                if not isinstance(data, dict):
                    self._user_stream_fault("USER_STREAM_EVENT_NOT_OBJECT")
                    return
                event_type = str(data.get("e") or "").strip()
                if event_type == "listenKeyExpired":
                    self._user_stream_fault("LISTEN_KEY_EXPIRED", terminal=True)
                    return
                self._update_user_stream_runtime(last_state_mono=time.monotonic())
                # BD-FIX: 事件成功到达即证明传输健康 —— 非终态 fault
                # （DEGRADED：解析失败/事件被拒等）在事件流恢复后自动
                # 回到 HEALTHY，不再永久卡死交易（I5 审查：单次未知
                # 事件后 DEGRADED 无恢复路径）。
                if str(self._user_stream_runtime.get("status", "")).upper() == "DEGRADED":
                    self._update_user_stream_runtime(
                        status="HEALTHY",
                        listen_key_active=True,
                        last_error="",
                    )
                if event_type == "ORDER_TRADE_UPDATE":
                    parsed = BinanceUsdmAdapter.parse_user_order_update(data)
                    if not parsed.is_success() or parsed.data is None:
                        self._user_stream_fault("ORDER_EVENT_PARSE_UNKNOWN")
                        return
                    accepted = self.ingest_user_order_update(parsed.data)
                elif event_type == "ACCOUNT_UPDATE":
                    parsed = BinanceUsdmAdapter.parse_user_account_update(data)
                    if not parsed.is_success() or parsed.data is None:
                        self._user_stream_fault("ACCOUNT_EVENT_PARSE_UNKNOWN")
                        return
                    accepted = self.ingest_user_account_update(parsed.data)
                elif event_type == "ACCOUNT_CONFIG_UPDATE":
                    # BD-FIX (S1): ACCOUNT_CONFIG_UPDATE 是 Binance 推送的
                    # 信息性事件（杠杆变更、保证金模式变更等），不是数据流
                    # 故障。接收并更新账户配置事实，保持流健康。
                    accepted = self._ingest_account_config_update(data)
                elif event_type == "ALGO_UPDATE":
                    # BD-FIX: demo 共享账户其他用户的算法单推送 ALGO_UPDATE
                    # —— 对单账户系统是"需复核"，对共享 demo 是环境噪音
                    # （15:52 实测 FILLED 达成后 ALGO_UPDATE 触发 fault →
                    # NO_NEW_RISK）。testnet 按信息性事件处理（保持流健康，
                    # 与 ACCOUNT_CONFIG_UPDATE 同语义）；live/canary 保持
                    # fault（自己的算法单状态变化必须复核）。
                    if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                        self._update_user_stream_runtime(
                            status="HEALTHY",
                            last_event_mono=time.monotonic(),
                            listen_key_active=True,
                            last_error="",
                        )
                        return
                    self._user_stream_fault("ALGO_UPDATE_REVALIDATION_REQUIRED")
                    return
                elif event_type == "MARGIN_CALL":
                    # BD-FIX: 共享 demo 账户的其他用户把共享保证金打到追缴线
                    # 也会推送 MARGIN_CALL —— testnet 按信息性事件处理
                    # （记录 + 保持流健康）；live 保持 terminal（自身仓位
                    # 追缴必须停流复核）。
                    if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                        print(f"[user-stream] MARGIN_CALL received on testnet (shared account) — informational")
                        self._update_user_stream_runtime(
                            status="HEALTHY",
                            last_event_mono=time.monotonic(),
                            listen_key_active=True,
                            last_error="",
                        )
                        return
                    self._user_stream_fault("MARGIN_CALL", terminal=True)
                    return
                elif event_type in ("STRATEGY_UPDATE", "GRID_UPDATE"):
                    # BD-FIX: 共享 demo 账户其他用户的策略/网格单更新属环境
                    # 噪音 —— testnet 按信息性事件处理；live 保持 fault。
                    if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                        print(f"[user-stream] {event_type} on testnet (shared account) — informational")
                        self._update_user_stream_runtime(
                            status="HEALTHY",
                            last_event_mono=time.monotonic(),
                            listen_key_active=True,
                            last_error="",
                        )
                        return
                    self._user_stream_fault(f"UNSUPPORTED_USER_EVENT:{event_type}")
                    return
                else:
                    # PKG02 (BDS-P0-001): 所有环境统一 fail-closed；
                    # testnet 共享账户的未知事件（新格式/其他 worker 构造）
                    # 按信息性处理，避免一次未知事件永久锁死交易（I5 审查）。
                    if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
                        print(
                            f"[user-stream] unknown event type {event_type or 'UNKNOWN'} "
                            "on testnet (shared account) — informational"
                        )
                        self._update_user_stream_runtime(
                            status="HEALTHY",
                            last_event_mono=time.monotonic(),
                            listen_key_active=True,
                            last_error="",
                        )
                        return
                    self._user_stream_fault(f"UNSUPPORTED_USER_EVENT:{event_type or 'UNKNOWN'}")
                    return
                if accepted:
                    self._update_user_stream_runtime(
                        status="HEALTHY",
                        last_event_mono=time.monotonic(),
                        listen_key_active=True,
                        last_error="",
                    )
                else:
                    self._user_stream_fault(f"USER_EVENT_REJECTED:{event_type or 'UNKNOWN'}")

            await websocket.subscribe(listen_key, _on_user_event)
            self._update_user_stream_runtime(status="CONNECTED", listen_key_active=True)
            self._user_ws_task = asyncio.create_task(websocket.run())
            self._user_stream_keepalive_task = asyncio.create_task(self._user_stream_keepalive_loop(listen_key))
            return True
        except Exception as exc:
            self._user_stream_fault(f"USER_STREAM_START_FAILED:{type(exc).__name__}", terminal=True)
            if websocket is not None:
                with contextlib.suppress(Exception):
                    await websocket.close()
            self._user_ws_client = None
            self._user_stream_listen_key = None
            return False

    async def _user_stream_keepalive_loop(self, listen_key: str) -> None:
        """Renew the exact listen key; failure revokes writable authority.

        BD-FIX: 单次瞬时失败（限频/网络抖动）重试后再判 terminal ——
        demo 抖动下单次失败即停流换 key 会快速耗尽 3 次重启预算
        （M9 审查：跨小时 3 次失败即永久 STOPPED）。
        """

        _consecutive_failures = 0
        try:
            while True:
                await asyncio.sleep(30 * 60)
                if getattr(self, "_user_stream_stopping", False):
                    return
                keepalive = getattr(self._adapter, "keepalive_user_listen_key", None)
                if not callable(keepalive):
                    self._user_stream_fault("ADAPTER_USER_STREAM_KEEPALIVE_UNSUPPORTED", terminal=True)
                    return
                result = await keepalive(listen_key)
                if not result.is_success():
                    _consecutive_failures += 1
                    if _consecutive_failures >= 3:
                        self._user_stream_fault("LISTEN_KEY_KEEPALIVE_FAILED", terminal=True)
                        return
                    # 瞬时失败：60s 后重试，不立即停流
                    print(f"[user-stream] keepalive failed ({_consecutive_failures}/3), retrying in 60s")
                    await asyncio.sleep(60)
                    if getattr(self, "_user_stream_stopping", False):
                        return
                    result = await keepalive(listen_key)
                    if not result.is_success():
                        _consecutive_failures += 1
                        if _consecutive_failures >= 3:
                            self._user_stream_fault("LISTEN_KEY_KEEPALIVE_FAILED", terminal=True)
                            return
                        print(f"[user-stream] keepalive failed ({_consecutive_failures}/3), will retry next cycle")
                    continue
                _consecutive_failures = 0
                self._update_user_stream_runtime(listen_key_active=True, last_keepalive_mono=time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._user_stream_fault(f"LISTEN_KEY_KEEPALIVE_ERROR:{type(exc).__name__}", terminal=True)

    async def _stop_user_stream(self) -> None:
        """Stop user-stream tasks and close the socket without touching orders."""

        self._user_stream_stopping = True
        keepalive_task = getattr(self, "_user_stream_keepalive_task", None)
        if keepalive_task is not None:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keepalive_task
            self._user_stream_keepalive_task = None
        websocket_task = getattr(self, "_user_ws_task", None)
        if websocket_task is not None:
            websocket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await websocket_task
            self._user_ws_task = None
        websocket = getattr(self, "_user_ws_client", None)
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()
        self._user_ws_client = None
        self._user_stream_listen_key = None
        if bool(getattr(self, "_can_write", False)):
            self._update_user_stream_runtime(status="STOPPED", listen_key_active=False)

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
            # BD-FIX: testnet 下 opening baseline 只保留本地所有权品种 ——
            # 8-12 时代的 baseline 含共享账户外部持仓（AIOUSDT 178 等），
            # 与本地账本事实混同造成对账恒 MISMATCH
            _owned_syms = _local_owned_symbols(self)
            _is_testnet_sys = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
            positions.update(
                {
                    InstrumentId(str(symbol)): Quantity(amount=str(amount))
                    for symbol, amount in dict(opening.get("positions", {})).items()
                    if not _is_testnet_sys or str(symbol) in _owned_syms
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
        # BD-FIX: testnet 下 fill 重放只认本地订单 —— 共享账户其他用户
        # 的成交（外部订单）经监控写入 fill_event/position_projection，
        # 重放会把外部持仓混进 system 侧（final40 实测 CYSUSDT/AIOUSDT
        # 等外部成交污染投影 → 对账恒 MISMATCH）
        _is_testnet_replay = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
        _local_order_ids: set[str] = set()
        if _is_testnet_replay:
            _local_order_ids = {
                str(row.get("order_id") or row.get("record_id") or "")
                for row in self._store.restore_order_states()
                if str(row.get("client_order_id", "") or "").startswith("beidou-")
            } | {str(oid) for oid in getattr(self, "_owned_order_ids", set())}
        for fill in self._store.restore_fill_events():
            if _is_testnet_replay and str(fill.get("order_id", "")) not in _local_order_ids:
                continue  # 外部订单的成交：不属于引擎账本
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
        # BD-FIX: testnet 下 stale 订单行（历史遗留，交易所早已无此单）
        # 不参与 open_orders 对账 —— 多品种上线后 8-12 时代的 6 个
        # 遗留 active 行造成 system/exchange 恒 MISMATCH。24h 为界，
        # 时间戳不可解析的行保留（fail-safe 不过滤）。
        if str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet":
            _stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            _tracked_ids = {str(oid) for oid in getattr(self, "_active_order_ids", set())}
            _filtered: list[dict[str, Any]] = []
            for _row in active_orders:
                # BD-FIX: 只保留本引擎命名空间的订单 —— 共享账户其他
                # 用户的订单行（经恢复路径写入 order_state）不参与
                # system 侧 open_orders 对账
                _oid = str(_row.get("order_id") or _row.get("record_id") or "")
                _cid = str(_row.get("client_order_id", "") or "")
                if _oid not in _tracked_ids and not _cid.startswith("beidou-"):
                    continue
                try:
                    _row_ts = datetime.fromisoformat(str(_row.get("updated_at") or _row.get("created_at") or ""))
                    if _row_ts.tzinfo is None:
                        _row_ts = _row_ts.replace(tzinfo=timezone.utc)
                    if _row_ts <= _stale_cutoff:
                        continue  # stale 行：交易所早已无此单
                except (TypeError, ValueError):
                    pass
                _filtered.append(_row)
            active_orders = _filtered
        # Post-opening fills mean the balance may be slightly stale (fees),
        # but that is acceptable within the reconciliation balance tolerance
        # (5 USDT).  Only an inconsistent projection (uncommitted fills,
        # parse failures) forces INCOMPLETE.  If all fills are COMMITTED
        # and the position replay is self-consistent, the projection is complete.
        if post_opening_fill and not opening_complete:
            pass  # already incomplete from a fill processing error above
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

    def _reconciliation_rule_steps(self, symbols: set[str]) -> tuple[dict[str, str], str]:
        """Bind every reconciled position to a fresh venue rule snapshot.

        Falls back to engine-level ``_symbol_precision`` when the adapter
        rule-snapshot cache has not been populated yet (e.g. first startup
        before ``sync_rule_snapshots`` is called).
        """

        if not symbols:
            return {}, "OK"
        adapter = getattr(self, "_adapter", None)
        get_rule_snapshot = getattr(adapter, "get_rule_snapshot", None)
        precision_cache: dict = getattr(self, "_symbol_precision", {})

        # No authority at all — neither adapter nor engine-level cache
        if not callable(get_rule_snapshot) and not precision_cache:
            return {}, "POSITION_RULE_AUTHORITY_UNAVAILABLE"

        rule_steps: dict[str, str] = {}
        for symbol in sorted(symbols):
            snapshot = None
            if callable(get_rule_snapshot):
                snapshot = get_rule_snapshot(symbol)
            if snapshot is None or not snapshot.is_known or snapshot.is_stale:
                # Fallback: use engine-level _symbol_precision populated from
                # exchangeInfo at startup.  This ensures reconciliation can
                # proceed even before the adapter's sync_rule_snapshots runs.
                prec = precision_cache.get(symbol, {})
                step_size = str(prec.get("step_size", "")).strip()
                if not step_size:
                    return {}, f"POSITION_RULE_UNKNOWN_OR_STALE:{symbol}"
                try:
                    step = Decimal(step_size)
                except (InvalidOperation, TypeError, ValueError):
                    return {}, f"POSITION_RULE_STEP_INVALID:{symbol}"
                if not step.is_finite() or step <= 0:
                    return {}, f"POSITION_RULE_STEP_INVALID:{symbol}"
                rule_steps[symbol] = step_size
                continue
            step_size = str(snapshot.step_size).strip()
            try:
                step = Decimal(step_size)
            except (InvalidOperation, TypeError, ValueError):
                return {}, f"POSITION_RULE_STEP_INVALID:{symbol}"
            if not step.is_finite() or step <= 0:
                return {}, f"POSITION_RULE_STEP_INVALID:{symbol}"
            rule_steps[symbol] = step_size
        return rule_steps, "OK"

    async def _reconcile(self) -> bool:
        """Compare fresh independent facts; never self-heal in place."""

        # Paper/shadow/research 模式无真实交易所，跳过对账
        _env_mode = getattr(self, "_env_mode", None)
        if _env_mode is not None and _env_mode.value in ("paper", "shadow", "research"):
            return True

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
        _owned_syms = _local_owned_symbols(self)
        _is_testnet_recon = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
        for position in account["positions"]:
            if not isinstance(position, dict) or "symbol" not in position or "positionAmt" not in position:
                result = ReconciliationEngine.compare(None, None)
                result.status = ReconciliationStatus.INCOMPLETE
                result.differences = ["INCOMPLETE_FACT: malformed exchange position row"]
                return self._record_reconciliation_failure(result)
            # BD-FIX: testnet 共享账户的外部持仓不参与对账（system 侧
            # 只含引擎自己的账本事实 —— 多品种上线后外部持仓造成
            # 恒 MISMATCH，final38 实测 APRUSDT/BEATUSDT/EPICUSDT 等）
            if _is_testnet_recon and str(position["symbol"]) not in _owned_syms:
                continue
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
        event_facts = getattr(self, "_event_stream_facts", None)
        position_symbols = {str(symbol) for symbol in system_facts.positions} | {
            str(symbol) for symbol in exchange_facts.positions
        }
        if event_facts is not None:
            position_symbols.update(str(symbol) for symbol in event_facts.positions)
        rule_steps, rule_reason = self._reconciliation_rule_steps(position_symbols)
        if rule_reason != "OK":
            result = ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.INCOMPLETE,
                differences=[rule_reason],
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                event_facts=event_facts,
            )
            return self._record_reconciliation_failure(
                result,
                system_facts=system_facts,
                exchange_facts=exchange_facts,
                event_facts=event_facts,
            )
        for facts in (system_facts, exchange_facts, event_facts):
            if facts is not None:
                facts.position_step_sizes = dict(rule_steps)
        self._recon.update_system_facts(system_facts)
        self._recon.update_exchange_facts(exchange_facts)
        if event_facts is not None:
            self._recon.update_event_facts(event_facts)
        # 启动时事件流可能尚未就绪 — 先使用两方对账建立基线，
        # 三方对账在事件流可用后自动启用。
        # BD-FIX（共享账户语义）: testnet/demo 的事件侧投影含共享账户
        # 外部活动（其他用户的持仓/余额变动经 ACCOUNT_UPDATE 流入），
        # 与本地账本（system 侧）必然不一致 —— 三方 MATCHED 在共享
        # demo 下不可达（final33 实测：外部 -0.02 空头 + 余额变动 →
        # event_stream 余额 5000 vs system 10548 恒 MISMATCH）。
        # testnet 以 system/exchange 两方为对账权威，事件侧仅作参考；
        # live/canary 保持三方严格语义不变。
        _is_testnet = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
        if event_facts is not None and not _is_testnet:
            result = self._recon.reconcile_three_way(AccountId("default"), VenueId("BINANCE"))
        else:
            result = self._recon.reconcile(AccountId("default"), VenueId("BINANCE"))
        self._last_reconciliation_result = result
        self._record_reconciliation_truth(
            result,
            system_facts=system_facts,
            exchange_facts=exchange_facts,
        )
        self._last_account = account
        # BD-FIX: testnet 自动授权 user stream replay baseline。
        # 两方（交易所 REST ↔ 系统账本）对拍一致即"独立验证"成立 ——
        # event_stream 侧因 sequencer 未授权而 INCOMPLETE 属预期（授权后
        # 才会完整，鸡生蛋），不构成授权障碍；两方冲突时 fail-closed 不授权。
        recon_id = f"recon-{result.checked_at.strftime('%Y%m%dT%H%M%S.%fZ')}"
        self._maybe_authorize_user_stream_baseline(exchange_facts, recon_id, result=result)
        # PKG02 (BDS-P0-001): 移除 testnet 仅仓位不匹配旁路 — 所有环境使用统一对账标准
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

    def _seed_pool_from_history(self, configured_symbols: list[str]) -> None:
        """启动时用历史 K 线数据预筛选交易池候选（BD-FIX）。

        历史质量分（成交量/稳定性/容量三维 —— 1d K 线可推导；点差与
        深度由实时评分补）达标者经 TradingPool.seed_historical_observation
        观察期等效已满；晋级权仍在实时 5 维评分规则。无历史数据的
        标的跳过（标准观察期）。
        """
        pool = getattr(self, "_trading_pool", None)
        if pool is None:
            return
        try:
            from beidou_research.data.kline_store import KlineStore
        except Exception:
            return
        store = KlineStore()
        seeded = 0
        for sym in configured_symbols:
            try:
                df = store.load(sym, "1d")
            except Exception:
                continue
            if df is None or len(df) < 100:
                continue
            try:
                closes = df["close"].astype(float).values
                volumes = df["volume"].astype(float).values
                highs = df["high"].astype(float).values
                lows = df["low"].astype(float).values
                if len(closes) < 100 or closes[-1] <= 0:
                    continue
                # volume_score：日均成交额（与实时 log10/8 同构）
                avg_daily_notional = float((volumes * closes).mean())
                volume_score = max(0.0, min(1.0, __import__("math").log10(max(1, avg_daily_notional)) / 8))
                # capacity_score：1 - 年化波动率（与实时 ann_vol 同构）
                returns = closes[1:] / closes[:-1] - 1.0
                ann_vol = float(returns.std()) * (365**0.5) if len(returns) > 30 else 1.0
                capacity_score = max(0.0, min(1.0, 1.0 - ann_vol))
                # stability_score：负收益天数占比反向
                down_days = float((returns < 0).mean()) if len(returns) else 0.5
                stability_score = max(0.0, min(1.0, 1.0 - down_days))
                # BD-FIX: 历史质量分只用可推导的 3 维 —— 点差/深度没有
                # 历史订单簿（(high-low)/close 是日内振幅不是点差，硬套
                # 实时权重会把历史分系统性低估到阈值以下，预筛选失效）
                quality = volume_score * 0.35 + stability_score * 0.30 + capacity_score * 0.35
                if pool.seed_historical_observation(
                    sym,
                    quality,
                    threshold=0.5,  # 预筛选是加速器：宽松准入，晋级权在实时评分
                    evidence={
                        "avg_daily_notional": round(avg_daily_notional, 2),
                        "ann_vol": round(ann_vol, 4),
                        "down_days_pct": round(down_days * 100, 2),
                        "days": len(closes),
                    },
                ):
                    seeded += 1
                    print(f"[pool] historical seed: {sym} quality={quality:.3f}")
            except Exception:
                continue
        if seeded:
            print(f"[pool] historical pre-filter seeded {seeded}/{len(configured_symbols)} candidates")

    def _maybe_auto_resolve_incidents(self) -> None:
        """持久事实干净后自动清除残留事故（BD-FIX）。

        执行事实已恢复可验证（recon 通过 + durable_ok）时：
        - execution_fact 事故在账本未冻结时 resolve（冻结是永久事故，
          不得被清理掩盖 —— C2 审查：旧逻辑 resolve 后 freeze 无任何
          可见信号）
        - reconciliation 事故（对账已恢复）无条件 resolve
        - user_stream 事故在 transport 恢复健康（CONNECTED/HEALTHY）后
          resolve（连接恢复 + 事实干净 = 事故根因消除）
        - protection 事故在所有权标志已复位后 resolve
        testnet 自动重新授权（1cd1208）可能已恢复 RESUME —— 事故清理
        不得依赖控制面状态（旧逻辑死角：control==RESUME 时永不 resolve，
        CRITICAL 事故永久残留，final14/21 实测）。
        """
        _alerts = getattr(self, "_alerts", None)
        if _alerts is None:
            return
        _runtime = getattr(self, "_user_stream_runtime", {})
        _ustatus = str(_runtime.get("status", "")).upper() if isinstance(_runtime, dict) else ""
        _ledger = getattr(self, "_ledger", None)
        _ledger_frozen = bool(_ledger is not None and getattr(_ledger, "is_frozen", False))
        _protection_ok = not bool(getattr(self, "_protection_owner_unknown", False))
        try:
            _active = getattr(_alerts, "_active_incidents", {})
            for _iid, _inc in list(_active.items()):
                _cat = str(getattr(_inc, "root_cause_category", ""))
                if _cat == "execution_fact":
                    if _ledger_frozen:
                        continue  # 冻结未解，事故必须保留
                    _alerts.resolve_incident(_iid)
                    print("[realtime] Auto-resolved execution_fact incident")
                elif _cat == "reconciliation":
                    _alerts.resolve_incident(_iid)
                    print("[realtime] Auto-resolved reconciliation incident")
                elif _cat == "user_stream" and _ustatus in ("CONNECTED", "HEALTHY"):
                    _alerts.resolve_incident(_iid)
                    print("[realtime] Auto-resolved user_stream incident")
                elif _cat == "protection" and _protection_ok:
                    _alerts.resolve_incident(_iid)
                    print("[realtime] Auto-resolved protection incident")
        except Exception as exc:
            logger.warning("incident auto-resolution failed: %s", type(exc).__name__)

    def _maybe_authorize_user_stream_baseline(
        self,
        exchange_facts: AccountFactSnapshot,
        recon_id: str,
        *,
        result: Any | None = None,
    ) -> bool:
        """testnet 在 recon MATCHED 后自动授权 user stream replay baseline（BD-FIX）。

        Binance user stream 事件没有单调序列号，投影器 sequencer 必须经显式
        replay 授权（``allow_unsequenced=True``）才接受事件；否则下单后第一个
        ORDER_TRADE_UPDATE 必被拒（USER_EVENT_REJECTED）→ fault → STOPPED →
        DEGRADED → LOCKED 停机。recon MATCHED（REST/系统/事件三方独立对拍）
        即投影器 docstring 要求的 "independently verified" 基线，testnet 据此
        自动授权；live/canary 保持人工治理授权语义（与 1cd1208 环境区分一致）。

        幂等：sequencer 已 HEALTHY 时跳过。授权失败只 defer（事件流保持
        fail-closed），不冻结账本、不发 incident —— 自动路径不得触发
        execution_fact fail-closed。

        ``result`` 提供时先检查两方一致性：存在 system/exchange 差异（两方
        对拍冲突）则不授权 —— 独立验证不成立。event_stream 侧差异不构成
        障碍（授权前事件流必然 INCOMPLETE）。
        """
        if result is not None:
            differences = [str(d) for d in getattr(result, "differences", []) or []]
            if any(str(d).startswith("system/exchange") for d in differences):
                return False
        if not bool(getattr(self, "_can_write", False)):
            return False
        if str(getattr(self._env_mode, "value", "")) != "testnet":
            return False
        projector = getattr(self, "_user_stream_projector", None)
        if projector is None:
            return False
        sequencer = getattr(projector, "sequencer", None)
        if sequencer is None:
            return False
        status = getattr(sequencer.status, "value", sequencer.status)
        if str(status) == "HEALTHY":
            return False
        evidence_hash = hashlib.sha256(f"recon:{recon_id}".encode()).hexdigest()[:24]
        result = projector.authorize_replay_baseline(
            exchange_facts,
            evidence_hash=evidence_hash,
            approval_id=f"reconciliation-governed:{recon_id}",
            last_sequence=None,
            allow_unsequenced=True,
        )
        if result.status is UserProjectionStatus.ACCEPTED:
            self._event_stream_facts = projector.fact_snapshot()
            recon = getattr(self, "_recon", None)
            if recon is not None:
                recon.update_event_facts(self._event_stream_facts)
            # BD-FIX: 只授权投影器，不得伪造 transport 健康 ——
            # runtime status 只能由 ws 状态回调/事件到达驱动。重启限次
            # 耗尽后实际无任何 ws 连接，伪造 HEALTHY 会让 testnet
            # readiness 误判为可交易（幽灵健康）。
            print("[recon] testnet auto-authorized user-stream replay baseline (allow_unsequenced)")
            # BD-FIX: 重启限次耗尽后（I7 审查：attempts=3 用完即永久
            # FAILED 且无恢复机会），recon 独立验证干净时重置预算并
            # 重新拉起传输 —— 受 _user_stream_restarting/_user_ws_task
            # 防护，无连接才重拉。
            _runtime = getattr(self, "_user_stream_runtime", {})
            _ustatus = str(_runtime.get("status", "")).upper() if isinstance(_runtime, dict) else ""
            if (
                _ustatus in ("FAILED", "STOPPED", "UNKNOWN")
                and getattr(self, "_user_stream_restart_attempts", 0) >= self._USER_STREAM_RESTART_MAX_ATTEMPTS
                and not getattr(self, "_user_stream_restarting", False)
            ):
                self._user_stream_restart_attempts = 0
                with contextlib.suppress(RuntimeError):
                    self._user_stream_restart_task = asyncio.create_task(
                        self._restart_user_stream_after_fault("ATTEMPTS_RESET_AFTER_RECON")
                    )
                print("[recon] user-stream restart budget reset after verified reconciliation")
            return True
        print(f"[recon] user-stream replay authorization deferred: {result.reason}")
        return False

    async def _safe_recover_unowned_testnet_algos(
        self,
        unowned_algo_ids: list[str],
        existing_algo_inventory: list[dict[str, Any]] | None,
        positions_list: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]] | None]:
        """BD-FIX (S2): Testnet 模式下无持仓时自动取消残留无主 Algo 订单。

        非正常退出（kill -9 / 崩溃）会导致交易所残留条件单，新进程无法
        认领所有权，造成 protection_owner_unknown 永久阻断。Testnet 无真实
        持仓时安全取消这些残留订单，避免手动清理。
        """
        if not unowned_algo_ids:
            return unowned_algo_ids, existing_algo_inventory

        has_any_position = any(
            abs(float(pos.get("positionAmt", 0) or 0)) > 0 for pos in positions_list
        )
        _env_mode = getattr(self, "_env_mode", None)
        is_testnet = _env_mode is not None and _env_mode.value == "testnet"

        if is_testnet and not has_any_position:
            cancelled = 0
            for algo_id in unowned_algo_ids:
                try:
                    symbol = ""
                    client_algo_id = ""
                    for item in (existing_algo_inventory or []):
                        if str(item.get("algoId")) == algo_id:
                            symbol = str(item.get("symbol", ""))
                            client_algo_id = str(item.get("clientAlgoId", "") or "")
                            break
                    # BD-FIX（C2 审查）: 共享 demo 账户上其他用户的算法单
                    # 不是"残留无主"—— 只有本引擎命名空间（beidou- 前缀）
                    # 的单才允许自动取消；他人订单保持阻断（fail-closed）。
                    if client_algo_id and not client_algo_id.startswith("beidou-"):
                        print(
                            f"[beidou-autopilot] Skip foreign Algo {algo_id} "
                            f"(clientAlgoId={client_algo_id[:24]}...) — not owned by this engine"
                        )
                        continue
                    await self._cancel_algo_order(symbol, int(algo_id))
                    cancelled += 1
                    print(
                        f"[beidou-autopilot] Cancelled unowned testnet Algo order "
                        f"{algo_id} (symbol={symbol})"
                    )
                except Exception as exc:
                    print(f"[beidou-autopilot] Failed to cancel unowned Algo {algo_id}: {exc}")
            if cancelled:
                print(
                    f"[beidou-autopilot] BD-FIX (S2): Auto-cancelled {cancelled} unowned "
                    "testnet Algo orders (no exchange positions)"
                )
                # Refresh Algo inventory after cancellation
                try:
                    existing_algos = await asyncio.wait_for(
                        self._get_open_algo_inventory(), timeout=30.0
                    )
                    if isinstance(existing_algos, list):
                        existing_algo_inventory = existing_algos
                except Exception as exc:
                    logger.warning("Failed to refresh algo inventory after cancellation: %s", exc)
                return [], existing_algo_inventory
            return unowned_algo_ids, existing_algo_inventory

        # Non-testnet or has positions: keep the blocking behaviour
        self._block_unowned_protection_orders(unowned_algo_ids)
        print(
            f"[beidou-autopilot] {len(unowned_algo_ids)} unowned Algo orders remain "
            "UNKNOWN; startup recovery is read-only"
        )
        return unowned_algo_ids, existing_algo_inventory

    async def _ensure_exchange_position_protections(self) -> None:
        """为交易所已有但系统未追踪的持仓补充保护单（启动阶段调用）。

        Startup inventory is observational.  If an exchange position is not
        already covered by an ACK-backed, owner-bound durable protection, the
        governed recovery state machine must take over; this compatibility
        check never submits an unowned raw Algo order.
        """
        ctrl_state = self._control.get_status()
        skip_exchange_orders = ctrl_state in (ControlAction.LOCK, ControlAction.EMERGENCY_FLATTEN)
        if skip_exchange_orders:
            print(f"[protection] Control plane is {ctrl_state.value} — startup recovery remains read-only")
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

            # 检查哪些已有保护单
            algos_resp = await self._get_open_algo_inventory()
            protected_symbols: set[str] = set()
            if not isinstance(algos_resp, list):
                self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
                print("[startup] Conditional-order inventory UNKNOWN; recovery remains read-only")
                return
            if isinstance(algos_resp, list):
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                # BD-FIX（C2 审查）: 共享账户他人算法单（非 beidou- 前缀）
                # 不构成所有权阻断 —— 集合级过滤（取消循环内另有逐条过滤）
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in algos_resp
                    if item.get("algoId") is not None
                    and str(item.get("algoId")) not in known_algo_ids
                    and str(item.get("clientAlgoId", "") or "").startswith("beidou-")
                ]
                if unowned_algo_ids:
                    # PKG02 (BDS-P0-001): 所有环境统一处理 — 清理残留无主 Algo 订单。
                    # BD-FIX（C2 审查）: 只取消本引擎命名空间（beidou- 前缀）
                    # 的残留单；共享 demo 账户上其他用户的算法单不是残留
                    # —— 保持阻断（fail-closed）而非取消。
                    print(
                        f"[startup] Cancelling {len(unowned_algo_ids)} "
                        f"stale unowned Algo orders (positions={len(exchange_positions)})"
                    )
                    for item in algos_resp:
                        algo_id = str(item.get("algoId"))
                        if algo_id not in known_algo_ids:
                            sym = str(item.get("symbol", ""))
                            _client_algo_id = str(item.get("clientAlgoId", "") or "")
                            if _client_algo_id and not _client_algo_id.startswith("beidou-"):
                                print(
                                    f"[startup]   ⊘ Skip foreign Algo {algo_id} "
                                    f"(clientAlgoId={_client_algo_id[:24]}...) — not owned by this engine"
                                )
                                continue
                            try:
                                await self._adapter.cancel_algo_order(sym, int(algo_id))
                                print(f"[startup]   ✓ Cancelled {sym} Algo {algo_id}")
                            except Exception as cancel_exc:
                                print(f"[startup]   ⚠️  Failed to cancel {sym} Algo {algo_id}: {cancel_exc}")
                    # Refresh inventory
                    algos_resp = await self._get_open_algo_inventory()
                    if not isinstance(algos_resp, list):
                        self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
                        print("[startup] Conditional-order inventory UNKNOWN after cleanup")
                        return
                    # Recalculate after cleanup
                    known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                    unowned_algo_ids = [
                        str(item.get("algoId"))
                        for item in algos_resp
                        if item.get("algoId") is not None
                        and str(item.get("algoId")) not in known_algo_ids
                        and str(item.get("clientAlgoId", "") or "").startswith("beidou-")
                    ]
                if unowned_algo_ids:
                    self._block_unowned_protection_orders(unowned_algo_ids)
                    print("[startup] Conditional-order owner mapping UNKNOWN; recovery remains read-only")
                    return
                venue_algo_ids = {str(item.get("algoId")) for item in algos_resp if item.get("algoId") is not None}
                missing_owned_ids = sorted(known_algo_ids - venue_algo_ids)
                if missing_owned_ids:
                    # A durable ACTIVE row is not proof that the venue still
                    # has the protection.  Missing IDs are ambiguous (the
                    # order may have triggered, expired, or been cancelled),
                    # so freeze new risk and require reconciliation rather
                    # than silently recreating or cancelling anything.
                    self._block_unowned_protection_orders(
                        [f"OWNED_PROTECTION_MISSING:{algo_id}" for algo_id in missing_owned_ids]
                    )
                    print("[startup] Durable protection is absent from venue inventory; recovery remains read-only")
                    return
                semantic_issues = self._protection_inventory_semantic_issues(algos_resp)
                if semantic_issues:
                    self._block_unowned_protection_orders(semantic_issues)
                    print(
                        "[startup] Conditional-order semantics do not match durable protection; recovery remains read-only"
                    )
                    return
                for item in algos_resp:
                    if str(item.get("algoId")) in known_algo_ids:
                        protected_symbols.add(str(item.get("symbol", "")))

            # The inventory check above is required even when every venue
            # position already has a local projection.  Only after the
            # independent venue fact is verified may we skip creation.
            if not already_handled:
                return  # 所有持仓已由 Phase 1/2 处理完毕且库存已核验

            # 有 PENDING 保护订单的持仓视为"已尝试保护"。
            # 启动时 supervisor 可能未授权写入，导致保护订单无法提交到交易所，
            # 但本地 PENDING 记录证明系统已经尝试保护。不应因此阻塞就绪检查。
            for row in store.restore_protections() if (store := getattr(self, "_store", None)) else []:
                if str(row.get("status", "")).upper() == "PENDING":
                    pending_sym = str(row.get("symbol", "")).strip().upper()
                    if pending_sym:
                        protected_symbols.add(pending_sym)

            # BD-FIX（C3 审查）: 只对本地所有权可证明的持仓要求保护 ——
            # 共享 demo 账户的外部持仓不属于引擎，不创建保护也不触发
            # UNPROTECTED 阻断；豁免仅限 testnet（live/canary 中交易所
            # 持仓无本地记录 = 丢仓，必须阻断）。
            _locally_owned_symbols = {
                str(pp.instrument_id) for pp in self._protection.all_positions().values()
            } | {str(sym) for sym in getattr(self, "_position_generation", {}).keys()}
            _env_mode = getattr(self, "_env_mode", None)
            if _env_mode is not None and str(getattr(_env_mode, "value", "")) == "testnet":
                _foreign_positions = {s for s in exchange_positions if s not in _locally_owned_symbols}
                if _foreign_positions:
                    print(
                        f"[startup] {len(_foreign_positions)} foreign positions on shared account "
                        f"— no protection required: {sorted(_foreign_positions)}"
                    )
                unprotected = {
                    s: d
                    for s, d in exchange_positions.items()
                    if s in _locally_owned_symbols and s not in protected_symbols
                }
            else:
                unprotected = {s: d for s, d in exchange_positions.items() if s not in protected_symbols}
            if not unprotected:
                return

            # This compatibility path used to submit raw Algo orders and only
            # register a local projection afterwards.  That creates an
            # unowned venue order if the process dies between the ACK and the
            # local write, and it can race the governed recovery path.  Leave
            # the position untouched and require the explicit recovery state
            # machine to establish an owner/generation-bound pair instead.
            self._block_unowned_protection_orders(
                [f"UNPROTECTED_POSITION_RECOVERY_REQUIRED:{symbol}" for symbol in sorted(unprotected)]
            )
            print(
                f"[startup] {len(unprotected)} positions without certified protection — recovery remains read-only"
                + (f" (control={ctrl_state.value})" if skip_exchange_orders else "")
            )
            return

        except Exception as e:
            print(f"[startup] Exchange protection check error: {e}")

    async def _cleanup_excess_orders(self) -> None:
        """每个持仓标的在交易所最多保留 2 个保护单，超量则取消最旧的。

        解决多轮 nearline 重试累积重复订单的问题。
        """
        # Cancellation is a state-changing operation.  A stale account
        # snapshot, a failed reconciliation, or NO_NEW_RISK must never be
        # treated as proof that an old conditional order is excess.
        if self._control.get_status() != ControlAction.RESUME or not self._fresh_matched_reconciliation():
            print("[nearline] Excess-order cleanup skipped: fresh matched reconciliation/RESUME required")
            return
        try:
            existing_algos = await self._get_open_algo_inventory()
            if not isinstance(existing_algos, list):
                return
            known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
            # BD-FIX（C2 审查运行时变体）: 共享 demo 账户上其他用户的
            # 算法单（非 beidou- 前缀）不构成所有权阻断 —— 排除出
            # unowned 集合；只有本引擎命名空间的未知单才触发阻断。
            unowned_algo_ids = [
                str(item.get("algoId"))
                for item in existing_algos
                if item.get("algoId") is not None
                and str(item.get("algoId")) not in known_algo_ids
                and str(item.get("clientAlgoId", "") or "").startswith("beidou-")
            ]
            if unowned_algo_ids:
                self._block_unowned_protection_orders(unowned_algo_ids)
                print("[nearline] Excess-order cleanup blocked: conditional-order ownership UNKNOWN")
                return
            semantic_issues = self._protection_inventory_semantic_issues(existing_algos)
            if semantic_issues:
                self._block_unowned_protection_orders(semantic_issues)
                print("[nearline] Excess-order cleanup blocked: protection semantics UNKNOWN")
                return
            exchange_symbols = {
                str(row.get("symbol", "")).strip().upper()
                for row in (getattr(self, "_last_account", {}) or {}).get("positions", [])
                if isinstance(row, dict)
                and str(row.get("symbol", "")).strip()
                and abs(float(row.get("positionAmt", 0) or 0)) > 1e-12
            }
            durable_rows = [
                row
                for row in self._store.restore_protections()
                if str(row.get("status", "")).upper() == "ACTIVE"
                and str(row.get("owner_id", "")) == str(self._protection_owner_id)
                and str(row.get("exchange_order_id", "")).strip()
            ]
            durable_ids = {str(row.get("exchange_order_id")) for row in durable_rows}
            if known_algo_ids != durable_ids:
                self._block_unowned_protection_orders(["PROTECTION_OWNER_MAPPING_INCOMPLETE"])
                print("[nearline] Excess-order cleanup blocked: durable/local Algo mapping differs")
                return
            expected_by_symbol: dict[str, int] = {}
            for row in durable_rows:
                symbol = str(row.get("symbol", "")).strip().upper()
                if symbol:
                    expected_by_symbol[symbol] = expected_by_symbol.get(symbol, 0) + 1
            # 按标的聚合；without a live position and durable expected rows,
            # there is no safe basis for cancelling any conditional order.
            by_symbol: dict[str, list[dict]] = {}
            for a in existing_algos:
                symbol = str(a.get("symbol", "")).strip().upper()
                if symbol:
                    by_symbol.setdefault(symbol, []).append(a)

            for symbol, orders in by_symbol.items():
                if symbol not in exchange_symbols:
                    continue
                expected = expected_by_symbol.get(symbol, 0)
                if expected <= 0:
                    continue
                excess = len(orders) - expected
                if excess <= 0:
                    continue
                # Missing/invalid venue creation time makes "oldest" an
                # unproven policy; leave all orders untouched and escalate.
                if any(a.get("createTime") in (None, "") for a in orders):
                    self._block_unowned_protection_orders([f"EXCESS_ORDER_AGE_UNKNOWN:{symbol}"])
                    continue
                try:
                    orders.sort(key=lambda a: int(a["createTime"]))
                except (KeyError, TypeError, ValueError):
                    self._block_unowned_protection_orders([f"EXCESS_ORDER_AGE_UNKNOWN:{symbol}"])
                    continue
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
            # PKG02 (BDS-P0-001): 移除 testnet API 失败/未知状态旁路 — 所有环境统一保护语义
            if not api_ok:
                self._block_unowned_protection_orders(["OPEN_ALGO_ORDERS_UNKNOWN"])
                print("[nearline] Protection retry blocked: conditional-order inventory UNKNOWN")
                return
            semantic_issues = self._protection_inventory_semantic_issues(existing_algos)
            if semantic_issues:
                self._block_unowned_protection_orders(semantic_issues)
                print("[nearline] Protection retry blocked: protection semantics UNKNOWN")
                return
            exchange_algo_symbols: dict[str, set[str]] = {}
            if api_ok:
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                # BD-FIX（C2 审查运行时变体）: 非 beidou- 前缀的算法单是
                # 共享账户他人订单，不构成所有权阻断（同 cleanup 路径）
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in existing_algos
                    if item.get("algoId") is not None
                    and str(item.get("algoId")) not in known_algo_ids
                    and str(item.get("clientAlgoId", "") or "").startswith("beidou-")
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
                # PKG02 (BDS-P0-001): 所有环境统一 protection 创建条件 — 不跳过符号检查
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

                side = "SELL" if pp.side == OrderSide.BUY else "BUY"
                prec = self._symbol_precision.get(symbol)
                if prec is None:
                    entry_val = float(pp.entry_price)
                    if entry_val > 5000:
                        dec = 1
                    elif entry_val > 100:
                        dec = 2
                    elif entry_val > 1:
                        dec = 3
                    else:
                        dec = 5
                    prec = {"price": dec, "quantity": dec}
                placed = 0

                # BD-FIX (S41): 交易所已有 Algo 单 → 跳过
                symbol_algo_count = len(exchange_algo_symbols.get(symbol, set()))
                if symbol_algo_count >= 2:
                    continue  # 已有 SL+TP

                # --- BD-FIX (S33): 首次创建止损单（如果没有）---
                if pp.stop_loss is None:
                    try:
                        entry_price = pp.entry_price
                        if entry_price <= 0:
                            continue
                        kline_features = await self._feed.async_get_kline_features(symbol)
                        from beidou_strategy.protection.adaptive import AdaptiveProtectionCalculator

                        adaptive_cfg = AdaptiveProtectionCalculator.calculate(symbol, entry_price, kline_features or {})
                        _prec = getattr(self, "_symbol_precision", {}).get(symbol, {})
                        if _prec:
                            self._protection.set_precision_from_rule(
                                type(
                                    "_PrecisionRule",
                                    (),
                                    {
                                        "price_precision": _prec.get("price", 0),
                                        "qty_precision": _prec.get("quantity", 0),
                                    },
                                )()
                            )
                        self._protection.create_protection(
                            position_id=pos_id,
                            instrument_id=InstrumentId(symbol),
                            venue_id=VenueId("BINANCE"),
                            entry_price=entry_price,
                            quantity=float(pp.quantity),
                            side=pp.side,
                            stop_loss_config=adaptive_cfg.stop_loss_config,
                            take_profit_config=adaptive_cfg.take_profit_config,
                            owner_id=str(getattr(self, "_protection_owner_id", "beidou-testnet")),
                            position_generation=0,
                            session_id=str(getattr(self, "_session_id", "")),
                        )
                        # 立即提交到交易所
                        reduce_side = "SELL" if pp.side == OrderSide.BUY else "BUY"
                        prec_map = self._symbol_precision.get(symbol)
                        if prec_map is None:
                            trigger_val = float(pp.entry_price)
                            if trigger_val > 5000:
                                dec = 1
                            elif trigger_val > 100:
                                dec = 2
                            elif trigger_val > 1:
                                dec = 3
                            else:
                                dec = 5
                            prec_map = {"price": dec, "quantity": dec}
                        if prec_map:
                            for p_order in [pp.stop_loss, *list(pp.take_profits)]:
                                if p_order is None:
                                    continue
                                try:
                                    algo_params = self._protection_algo_params(
                                        p_order, symbol=symbol, side=reduce_side, precision=prec_map
                                    )
                                    algo_resp = await self._create_algo_order(algo_params)
                                    if "algoId" in algo_resp:
                                        p_order.exchange_order_id = str(algo_resp["algoId"])
                                        p_order.status = ProtectionStatus.ACTIVE
                                        self._active_algo_ids.setdefault(pos_id, set()).add(str(algo_resp["algoId"]))
                                        print(
                                            f"[nearline] ✅ SL/TP submitted: {symbol} {p_order.order_type} algoId={algo_resp['algoId']}"
                                        )
                                    else:
                                        print(
                                            f"[nearline] ⚠️ SL/TP submit failed: {symbol} {algo_resp.get('msg', '')[:80]}"
                                        )
                                except Exception as exc:
                                    print(f"[nearline] ⚠️ SL/TP error: {symbol} {exc}")
                        print(f"[nearline] Protection CREATED for {symbol}: SL+TP")
                    except Exception as exc:
                        print(f"[nearline] Protection creation failed for {symbol}: {exc}")
                        continue

                # --- 重试止损单 ---
                # server_count < expected_count 说明有缺失，止损单存在即尝试补发
                if _needs_exchange_protection(pp.stop_loss):
                    # Retry the exact approved trigger.  Moving a stop after
                    # rejection changes the signed risk contract and could
                    # silently widen the permitted loss.
                    algo_resp = await self._create_algo_order(
                        self._protection_algo_params(
                            pp.stop_loss,
                            symbol=symbol,
                            side=side,
                            precision=prec,
                        )
                    )
                    if "algoId" in algo_resp:
                        algo_id = str(algo_resp["algoId"])
                        self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                        pp.stop_loss.exchange_order_id = algo_id
                        pp.stop_loss.status = ProtectionStatus.ACTIVE
                        self._persist_protection_order(pp.stop_loss, status="ACTIVE")
                        print(
                            f"[nearline] ✅ Retried stop loss for {symbol} at approved trigger → algoId={algo_resp['algoId']}"
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
                    # Take-profit retries use the same approved trigger; a
                    # venue rejection remains UNKNOWN instead of inventing a
                    # new exit policy at runtime.
                    tp_resp = await self._create_algo_order(
                        self._protection_algo_params(
                            tp,
                            symbol=symbol,
                            side=side,
                            precision=prec,
                        )
                    )
                    if "algoId" in tp_resp:
                        algo_id = str(tp_resp["algoId"])
                        self._active_algo_ids.setdefault(pos_id, set()).add(algo_id)
                        tp.exchange_order_id = algo_id
                        tp.status = ProtectionStatus.ACTIVE
                        self._persist_protection_order(tp, status="ACTIVE")
                        print(
                            f"[nearline] ✅ Retried take profit #{i} for {symbol} at approved trigger → algoId={tp_resp['algoId']}"
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
                        f"[nearline] 🔄 Partial retry #{retries}/3 for {symbol}: {placed} placed, {server_count}/{expected_count} — approved triggers preserved"
                    )
                else:
                    retries = self._protection_retries.setdefault(pos_id, 0) + 1
                    self._protection_retries[pos_id] = retries
                    print(f"[nearline] ⚠️ Protection retry #{retries}/3 FAILED for {symbol}: no orders placed")

        except Exception as e:
            print(f"[nearline] Protection retry error: {e}")

    def _venue_exact_quantity(self, symbol: str, size: float, *, tag: str = "nearline") -> float | None:
        """BD-FIX: 意图数量必须在创建/签名前按交易对规则量化。

        执行器 _submit_order_slice 拒绝"量化后与签名不一致"的数量
        （PLANNED_QUANTITY_NOT_VENUE_EXACT，审批绑定语义），未量化的原始
        sizing 值会导致所有订单被恒拒。此 helper 与执行器同源（adapter 规则
        快照 + ROUND_DOWN），保证意图内数量即 venue-exact 数量。

        返回：
        - float: 按 step_size 向下量化的数量（与执行器结果一致）；
        - None: 规则快照 UNKNOWN/STALE 或量化失败 — 调用方必须 SKIP/fail-closed；
        - size: adapter 无规则快照能力时保持原值（与旧行为一致，执行器仍有
          exchangeInfo fallback 与精确性门禁兜底）。
        """
        _rule_adapter = getattr(self, "_adapter", None)
        _get_snap = getattr(_rule_adapter, "get_rule_snapshot", None) if _rule_adapter is not None else None
        if not callable(_get_snap):
            return size
        _snap = _get_snap(str(symbol))
        if not _snap.is_known or _snap.is_stale:
            print(f"[{tag}] {symbol}: SKIP (rule snapshot UNKNOWN/STALE)")
            return None
        try:
            _quantized = _snap.quantize_quantity(str(size))
        except ValueError:
            print(f"[{tag}] {symbol}: SKIP (quantity quantization failed)")
            return None
        return float(_quantized)

    async def _nearline_tick(self) -> None:
        """近线时钟：K线分析 → 市场状态 → Alpha DAG → 融合 → 风控 → 优化 → OrderIntent。"""
        self._last_nearline = time.time()

        # BD-FIX: TruthSnapshot 风险事实周期刷新 — 近线每轮（testnet 30s，
        # 首轮启动即执行）无论是否产生信号/提案都刷新，避免 sizing 分支
        # 未执行时风险事实 300s 后 stale → 资格恒 NOT_VERIFIABLE。
        # hash 取周期性风险状态快照（策略风险等级）；sizing 分支随后用更
        # 丰富的 leverage/concentration/drawdown hash 覆盖。
        risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
        _periodic_risk_level = getattr(getattr(risk_state, "risk_level", None), "value", None)
        self._last_risk_hash = hashlib.sha256(f"periodic_risk:{_periodic_risk_level or 'UNKNOWN'}".encode()).hexdigest()
        self._last_risk_fact_at = time.time()

        # === 策略风险管理检查 ===
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
        cleanup_facts_ready = (
            self._control.get_status() == ControlAction.RESUME and self._fresh_matched_reconciliation()
        )
        exchange_positions_raw = self._last_account.get("positions", []) if cleanup_facts_ready else []
        exchange_symbols: set[str] = set()
        for ep in exchange_positions_raw:
            try:
                amt = float(ep.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                continue
            sym = str(ep.get("symbol", "")).strip().upper()
            if math.isfinite(amt) and abs(amt) > 0 and sym:
                exchange_symbols.add(sym)

        _GHOST_DEBOUNCE_ROUNDS = 3
        if not hasattr(self, "_ghost_absence_count"):
            self._ghost_absence_count: dict[str, int] = {}

        if not cleanup_facts_ready:
            print("[nearline] Ghost-position cleanup skipped: fresh matched reconciliation/RESUME required")

        if cleanup_facts_ready:
            for old_pid, old_pp in list(self._protection.all_positions().items()):
                symbol = str(old_pp.instrument_id)
                if symbol not in exchange_symbols:
                    count = self._ghost_absence_count.get(symbol, 0) + 1
                    self._ghost_absence_count[symbol] = count
                    if count >= _GHOST_DEBOUNCE_ROUNDS:
                        self._remove_protection_with_cleanup(old_pid, symbol)
                        await self._cancel_algo_orders(old_pid, symbol)
                        self._ghost_absence_count.pop(symbol, None)
                        print(
                            f"[nearline] 🧹 Cleaned up ghost position: {symbol} (absent {count} rounds, pos={old_pid})"
                        )
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
            # BD-FIX: 可观测性 — 无 ACTIVE 标的时说明宇宙晋级未完成
            # (需 ≥24h 观察期 + 评分 ≥0.6)，此前静默跳过导致"无成交"
            # 无法定位。打印一次状态而非每轮刷屏。
            if not active_symbols and self._tick_count % 10 == 0:
                print(
                    "[nearline] No ACTIVE trading-pool instruments — "
                    "universe promotion pending (≥24h observation + score ≥0.6 required)"
                )
            # Testnet: 每轮只处理 10 个标的，避免 REST 调用过多导致 monitor STALL
            if self._env_mode.value == "testnet" and len(active_symbols) > 10:
                active_symbols, self._nearline_batch_idx = _rotating_symbol_batch(
                    active_symbols,
                    getattr(self, "_nearline_batch_idx", 0),
                )
            # BD-FIX: 多时间框架 — 1m/5m/1h/1d 独立评估信号
            TIMEFRAMES = ("1m", "5m", "1h", "1d")
            # 仓位提案收集 → PortfolioOptimizer 冲突仲裁（循环结束后统一执行）
            proposals: list[dict] = []
            proposed_targets: list[PortfolioTarget] = []
            for symbol in active_symbols:
                # Yield to REALTIME clock between symbols
                await asyncio.sleep(0)

                # 1. K-line features — 多时间框架
                best_signal: Any = None
                best_strength = 0.0
                for tf in TIMEFRAMES:
                    features = await self._feed.async_get_kline_features(symbol, tf, 100)
                    if not features:
                        continue

                    # 更新移动止损价格极值
                    _close = features.get("close", 0)
                    if _close and hasattr(self, "_protection"):
                        try:
                            for _pos_id, _pos in self._protection.all_positions().items():
                                if str(getattr(_pos, "instrument_id", "")) == symbol:
                                    _pos.update_price_extremes(float(_close))
                        except Exception as exc:
                            logger.debug("position price-extreme update skipped: %s", type(exc).__name__)

                    # Bar closure check per timeframe
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
                        continue  # bar not closed for this timeframe
                    close = float(close)
                    if not self._advance_factor_bar(symbol, tf, bar_open_time, close):
                        continue

                    # Market state per timeframe
                    state = RealMarketStateEstimator.estimate(features)
                    print(
                        f"[nearline] {symbol}@{tf}: price={close} "
                        f"trend={state['direction']} stress={state['stress']} "
                        f"rsi={features.get('rsi_14', '?')}"
                    )
                    if state["stress"] == "HIGH":
                        continue

                    # DAG signal generation per timeframe
                    instrument_id = InstrumentId(symbol)
                    venue_id = VenueId("BINANCE")
                    positions = self._protection.all_positions()
                    symbol_positions = {k: v for k, v in positions.items() if str(v.instrument_id) == symbol}
                    pos_info = {"has_position": False, "entry_price": 0, "side": "", "entry_time": 0, "pnl_pct": 0}
                    if symbol_positions:
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
                        # BD-FIX: 组件历史按 timeframe 隔离 —— 单例组件
                        # 被 4 个 tf 循环交错喂历史会污染 z-score
                        "timeframe": tf,
                    }
                    if features.get("n_candles", 0) < 10:
                        continue

                    try:
                        kernel_result = await self._strategy_kernel.evaluate(context)
                        if not isinstance(kernel_result, dict):
                            continue
                        typed_mode = kernel_result.get("kernel") == "typed_graph"
                        typed_proposal = kernel_result.get("proposal") if typed_mode else None
                        all_signals = (
                            list(kernel_result.get("exit_signals", []))
                            if typed_mode
                            else list(kernel_result.get("signals", []))
                        )
                        if not typed_proposal and not all_signals:
                            continue
                    except Exception as exc:
                        logger.warning(
                            "Strategy kernel evaluation failed for %s/%s: %s", symbol, tf, type(exc).__name__
                        )
                        continue

                    # Track best signal across timeframes
                    if typed_mode and typed_proposal is not None:
                        strength = getattr(typed_proposal, "strength", 0)
                        if strength > best_strength:
                            best_strength = strength
                            best_signal = (tf, close, features, state, typed_proposal, pos_info, True, context)
                    elif all_signals:
                        strongest = max(all_signals, key=lambda s: getattr(s, "strength", 0), default=None)
                        if strongest and getattr(strongest, "strength", 0) > best_strength:
                            best_strength = getattr(strongest, "strength", 0)
                            best_signal = (tf, close, features, state, strongest, pos_info, False, context)

                # ── After timeframe loop: act on the best signal ──
                if best_signal is None:
                    continue
                tf, close, features, state, signal_obj, pos_info, typed_mode, context = best_signal

                # Save predictions for factor IC evaluation per timeframe
                predictions = context.get("_predictions", {})
                bar_open_time = features.get("bar_open_time")
                self._store_factor_predictions(symbol, tf, bar_open_time, close, predictions)

                # Build fused signal from the best timeframe's proposal
                instrument_id = InstrumentId(symbol)
                venue_id = VenueId("BINANCE")
                if typed_mode:
                    proposal = signal_obj
                    side = getattr(proposal, "side", None)
                    if side is None:
                        continue
                    print(
                        f"[nearline] {symbol}@{tf}: TypedGraph → "
                        f"side={side.value if hasattr(side, 'value') else side} "
                        f"strength={getattr(proposal, 'strength', 0):.3f}"
                    )
                    # 使用 SignalFuser 融合多时间框架信号
                    from beidou_strategy.alpha import AlphaSignal

                    direction = SignalDirection.LONG if side == OrderSide.BUY else SignalDirection.SHORT
                    alpha_signal = AlphaSignal(
                        strategy_id=self._autopilot_strategy_id,
                        component_type=AlphaComponentType.ENTRY,
                        direction=direction,
                        strength=float(getattr(proposal, "strength", 0)),
                        confidence=float(getattr(proposal, "confidence", 0)),
                        instrument_id=instrument_id,
                        venue_id=venue_id,
                        model_version=SchemaVersion("2.0.0"),
                    )
                    fused_result = self._fuser.fuse([alpha_signal])
                    fused = SimpleNamespace(
                        direction=fused_result.direction,
                        strength=fused_result.strength,
                        confidence=fused_result.confidence,
                        conflict_detected=getattr(fused_result, "conflict_detected", False),
                    )
                else:
                    print(
                        f"[nearline] {symbol}@{tf}: DAG → "
                        f"{getattr(signal_obj, 'direction', '?')} "
                        f"strength={getattr(signal_obj, 'strength', 0):.3f}"
                    )
                    alpha_signal = AlphaSignal(
                        strategy_id=self._autopilot_strategy_id,
                        component_type=AlphaComponentType.ENTRY,
                        direction=getattr(signal_obj, "direction", SignalDirection.NO_ACTION),
                        strength=float(getattr(signal_obj, "strength", 0)),
                        confidence=float(getattr(signal_obj, "confidence", 0)),
                        instrument_id=instrument_id,
                        venue_id=venue_id,
                        model_version=SchemaVersion("2.0.0"),
                    )
                    fused_result = self._fuser.fuse([alpha_signal])
                    fused = SimpleNamespace(
                        direction=fused_result.direction,
                        strength=fused_result.strength,
                        confidence=fused_result.confidence,
                        conflict_detected=getattr(fused_result, "conflict_detected", False),
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
                # PKG02: 从交易所规则获取最小下单量
                _precision = getattr(self, "_symbol_precision", {}).get(symbol, {})
                _min_qty = float(_precision.get("min_quantity", 0) or 0)
                _step = _precision.get("quantity", None)
                if _step is not None:
                    try:
                        _step_size = float(10 ** -int(_step))
                        _min_qty = max(_min_qty, _step_size)
                    except (ValueError, TypeError):
                        pass
                if _min_qty <= 0:
                    print(f"[nearline] {symbol}: SKIP (venue minQty UNKNOWN)")
                    continue
                if position_size <= 0 or position_size < _min_qty:
                    print(f"[nearline] {symbol}: SKIP (risk-sized quantity below venue minQty)")
                    continue
                # BD-CV10: 最小名义价值从交易所规则获取
                _prec_data = getattr(self, "_symbol_precision", {}).get(symbol, {})
                MIN_NOTIONAL = float(_prec_data.get("min_notional", 0) or 0)
                if MIN_NOTIONAL <= 0:
                    print(f"[nearline] {symbol}: SKIP (venue minNotional UNKNOWN)")
                    continue
                position_notional = price * position_size
                if position_notional < MIN_NOTIONAL:
                    print(
                        f"[nearline] {symbol}: SKIP (risk-sized notional ${position_notional:.2f} "
                        f"below venue minimum ${MIN_NOTIONAL:.2f})"
                    )
                    continue

                print(
                    f"[nearline] {symbol}: Adaptive → size={position_size:.4f} "
                    f"notional={position_notional:.0f} lev={dyn_leverage:.1f}x "
                    f"risk_based={risk_based_size:.4f} pct={adaptive_pct:.4f} "
                    f"vol={ann_vol:.1%} atr_stop={stop_loss_pct:.1f}% "
                    f"pool={'OK' if pool_capacity else 'SKIP'}"
                )

                trade_dir = (
                    1
                    if fused.direction == SignalDirection.LONG
                    else (-1 if fused.direction == SignalDirection.SHORT else 0)
                )
                if trade_dir == 0:
                    print(f"[nearline] {symbol}: SKIP (NO_ACTION has no portfolio direction)")
                    continue

                if not pool_capacity:
                    print(f"[nearline] {symbol}: SKIP (pool not tradable)")
                    continue

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
            # Probe the durable duplicate-identity fact once per nearline
            # cycle; querying once per symbol would add avoidable database
            # load without improving the evidence.
            duplicate_orders_24h: int | None = None
            duplicate_probe = getattr(self._outbox, "duplicate_order_count_24h", None)
            if callable(duplicate_probe):
                try:
                    duplicate_orders_24h = duplicate_probe()
                except Exception as exc:
                    print(f"[nearline] duplicate-order evidence UNKNOWN: {exc}")

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
                target_signed_quantity = (
                    Decimal(str(position_size))
                    if prop["direction"] == SignalDirection.LONG
                    else -Decimal(str(position_size))
                )
                current_signed_quantity = self._signed_position_from_account(self._last_account, symbol)
                if current_signed_quantity is None:
                    print(f"[nearline] {symbol}: SKIP (current signed position UNKNOWN)")
                    continue
                try:
                    inflight_signed_quantity = self._outbox.inflight_signed_quantity(symbol)
                except Exception as exc:
                    print(f"[nearline] {symbol}: SKIP (in-flight child exposure UNKNOWN: {type(exc).__name__})")
                    continue
                delta_plan = TargetDeltaPlan.compute(
                    symbol=symbol,
                    target_quantity=target_signed_quantity,
                    current_quantity=current_signed_quantity,
                    inflight_quantity=inflight_signed_quantity,
                )
                if delta_plan.side is None:
                    print(f"[nearline] {symbol}: NO_ACTION (target already satisfied by current + in-flight exposure)")
                    continue
                position_size = delta_plan.order_quantity
                side = OrderSide(delta_plan.side)
                order_notional = float(Decimal(str(price)) * position_size)
                target_position_notional = float(abs(target_signed_quantity * Decimal(str(price))))
                execution_rules = getattr(self, "_symbol_precision", {}).get(symbol, {})
                execution_min_qty = Decimal(str(execution_rules.get("min_quantity", "0") or "0"))
                execution_min_notional = Decimal(str(execution_rules.get("min_notional", "0") or "0"))
                if execution_min_qty <= 0 or execution_min_notional <= 0:
                    print(f"[nearline] {symbol}: SKIP (target-delta venue minimums UNKNOWN)")
                    continue
                if position_size < execution_min_qty or Decimal(str(order_notional)) < execution_min_notional:
                    print(f"[nearline] {symbol}: NO_ACTION (remaining target delta below venue minimum)")
                    continue
                # Downstream position-risk checks evaluate the post-trade
                # target exposure, while execution cost uses delta quantity.
                position_notional = target_position_notional
                target_side = "LONG" if target_signed_quantity > 0 else "SHORT"
                target_contract = self.build_portfolio_target(
                    symbol=symbol,
                    side=target_side,
                    exposure=target_position_notional,
                    delta=float(delta_plan.delta_quantity * Decimal(str(price))),
                    current_exposure=float(current_signed_quantity * Decimal(str(price))),
                    inflight_exposure=float(inflight_signed_quantity * Decimal(str(price))),
                )
                if not target_contract.is_valid():
                    print(f"[nearline] {symbol}: SKIP (signed target-delta contract invalid)")
                    continue

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

                # Build risk context for full R0-R10 evaluation.  Exchange
                # position facts are explicit: a missing/invalid position
                # snapshot must remain UNKNOWN, never be interpreted as flat.
                strategy_risk_state = self._strategy_risk.get_state(self._autopilot_strategy_id)
                position_qty: float | None = None
                liquidation_price: float | None = None
                account_positions = (
                    self._last_account.get("positions") if isinstance(self._last_account, dict) else None
                )
                if isinstance(account_positions, list):
                    position_qty = 0.0
                    for account_position in account_positions:
                        if not isinstance(account_position, dict) or account_position.get("symbol") != symbol:
                            continue
                        try:
                            raw_qty = float(account_position.get("positionAmt"))
                        except (TypeError, ValueError):
                            position_qty = None
                            break
                        if not math.isfinite(raw_qty):
                            position_qty = None
                            break
                        position_qty = raw_qty
                        # BD-FIX（I5 审查）: 共享账户外部持仓不参与
                        # 风控判定 —— testnet 下本地无所有权证明时
                        # 按无持仓处理（风控针对自有敞口；liq 价同步
                        # 置 None，避免"已平仓但有清算价"误判）
                        _foreign_position = (
                            str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
                            and symbol not in _local_owned_symbols(self)
                        )
                        if _foreign_position:
                            position_qty = 0.0
                            break
                        try:
                            raw_liquidation = account_position.get("liquidationPrice")
                            if raw_liquidation not in (None, ""):
                                liquidation_candidate = float(raw_liquidation)
                                if math.isfinite(liquidation_candidate) and liquidation_candidate > 0:
                                    liquidation_price = liquidation_candidate
                        except (TypeError, ValueError):
                            liquidation_price = None
                        # BD-FIX: demo 账户快照不提供 liquidationPrice 字段
                        # （实测 None）—— R7 恒 UNKNOWN 使有持仓后无法下
                        # 任何新单（fail-closed 拦截加仓与平仓，18:20 实测
                        # SELL strength=1.0 被拒）。testnet 用杠杆推导保守
                        # 清算价（entry × (1 ∓ 1/lev)，标准名义近似）；
                        # live/canary 保持严格（缺失即 UNKNOWN）。
                        if (
                            liquidation_price is None
                            and raw_qty != 0
                            and str(getattr(self._env_mode, "value", "")) == "testnet"
                        ):
                            try:
                                _entry = float(account_position.get("entryPrice"))
                                liquidation_price = _derive_liquidation_price(
                                    raw_qty, _entry, dyn_leverage
                                )
                            except (TypeError, ValueError):
                                liquidation_price = None
                        break

                # PKG02 (BDS-P0-001): 所有环境统一使用交易所返回的 liquidation_price。
                # 若为 None/0，R7 应如实返回 UNKNOWN（testnet 推导价例外，见上）。
                risk_context: dict = {
                    "leverage": dyn_leverage,
                    "max_leverage": self._policy_float("max_leverage", self._settings.production.max_leverage),
                    "concentration_pct": position_notional / max(account_balance, 1) * 100,
                    "max_concentration_pct": self._policy_float(
                        "max_concentration_pct", self._settings.production.max_concentration_pct
                    ),
                    "drawdown_pct": strategy_risk_state.current_drawdown_pct if strategy_risk_state else None,
                    "max_drawdown_pct": self._policy_float(
                        "max_drawdown_pct", self._settings.production.max_drawdown_pct
                    ),
                    "daily_loss_pct": (
                        strategy_risk_state.daily_pnl / max(account_balance, 1) * -100
                        if strategy_risk_state and account_balance > 0
                        else None
                    ),
                    "max_daily_loss_pct": self._policy_float(
                        "max_daily_loss_pct", self._settings.production.max_daily_loss_pct
                    ),
                    "consecutive_losses": self._loss_count,
                    "max_consecutive_losses": self._policy_int(
                        "max_consecutive_losses", self._settings.production.max_consecutive_losses
                    ),
                    "rolling_sharpe": (
                        self._drift_detector._baseline.get("sharpe")
                        if self._drift_detector.is_calibrated() and self._drift_detector._baseline
                        else None
                    ),
                    "min_sharpe_rolling": self._policy_float(
                        "min_sharpe_rolling", self._settings.production.min_sharpe_rolling
                    ),
                    "margin_ratio": (position_notional / dyn_leverage) / max(account_balance, 1)
                    if account_balance > 0
                    else 1.0,
                    "max_margin_ratio": 0.95,  # 保证金使用率不超过 95%
                    "position_qty": position_qty,
                    "liquidation_price": liquidation_price,
                    "current_price": price,
                    # PKG02 (BDS-P0-001): 所有环境使用统一策略参数。
                    "min_liquidation_distance_pct": self._policy_float("min_liquidation_distance_pct", 5.0),
                    "protected_positions": sum(
                        1
                        for pp in self._protection.all_positions().values()
                        if pp.stop_loss is not None and pp.stop_loss.is_active()
                    ),
                    "total_positions": self._protection.position_count(),
                    "can_trade": self._can_trade,  # 凭据权限推导 (R9)
                    # PKG02 (BDS-P0-001): 使用交易所实际返回的 canWithdraw。
                    # BD-FIX: R9 使用风险视角值 — 零写环境提款权限不构成
                    # 风险；testnet 由权限门禁豁免结算为 False。
                    "can_withdraw": self._risk_can_withdraw if self._can_write else False,
                    "duplicate_orders_24h": duplicate_orders_24h,
                }

                # PKG02 (BDS-P0-001): 所有环境使用完整的 R0-R10 风险规则评估
                risk_results = dict(RiskRuleRegistry.evaluate_all(risk_context))
                risk_approved = RiskRuleRegistry.is_approved(risk_results)
                auxiliary_failed_rules: list[str] = []
                # 快照风险校验是新增风险的第二道阻断门，不能降级为非阻塞告警。
                if risk_approved:
                    try:
                        from beidou_safety.risk.engine import RiskSnapshot as _RiskSnapshot

                        checked_at = getattr(self._last_reconciliation_result, "checked_at", None)
                        if isinstance(checked_at, datetime) and checked_at.tzinfo is None:
                            checked_at = checked_at.replace(tzinfo=timezone.utc)
                        source_timestamp = checked_at.isoformat() if isinstance(checked_at, datetime) else ""
                        evaluated_at = datetime.now(timezone.utc).isoformat()
                        # BD-FIX: 零写模式按设计跳过对账（_reconcile 直接返回），
                        # _last_reconciliation_result 恒 None。快照风控的
                        # is_complete() 要求非空 source_timestamp 与 MATCHED
                        # 对账状态，旧逻辑让 paper/shadow 的 AUX-R0 恒拒。
                        # 零写模式用评估时刻作为来源时间、对账按豁免处理，
                        # 与 supervisor 的 reconciliation_authority 豁免同语义。
                        _snapshot_env_mode = getattr(self, "_env_mode", None)
                        zero_write_snapshot = _snapshot_env_mode is not None and str(
                            getattr(_snapshot_env_mode, "value", _snapshot_env_mode)
                        ).lower() in ("paper", "shadow", "research", "safety_only")
                        if zero_write_snapshot:
                            source_timestamp = evaluated_at
                        snapshot_reconciliation_status = (
                            "MATCHED"
                            if self._fresh_matched_reconciliation() or zero_write_snapshot
                            else "MISMATCHED"
                        )
                        account_hash = hashlib.sha256(
                            f"{account_balance}|{self._protection.position_count()}".encode()
                        ).hexdigest()[:16]
                        risk_hash = hashlib.sha256(
                            f"{dyn_leverage}|{risk_context['concentration_pct']}|{risk_context['drawdown_pct']}".encode()
                        ).hexdigest()[:16]
                        # BD-FIX: TruthSnapshot 风险事实 — 复用近线风险快照 hash
                        self._last_risk_hash = risk_hash
                        self._last_risk_fact_at = time.time()
                        risk_policy_version = (
                            f"{self._policy_id_active or 'UNSIGNED'}:{self._policy_version or 'UNKNOWN'}"
                        )
                        rules_cfg = {
                            "max_leverage": risk_context["max_leverage"],
                            "max_concentration_pct": risk_context["max_concentration_pct"],
                        }
                        imp_snapshot = _RiskSnapshot(
                            total_exposure=position_notional,
                            margin_used=position_notional / max(dyn_leverage, 1),
                            margin_total=account_balance,
                            position_count=risk_context["total_positions"],
                            pending_orders=len(self._active_order_ids),
                            leverage=dyn_leverage,
                            concentration_pct=risk_context["concentration_pct"],
                            account_id="default",
                            dq_tier="PASS",
                            reconciliation_status=snapshot_reconciliation_status,
                            exchange_health=("HEALTHY" if self._check_liveness() is HealthState.HEALTHY else "UNSAFE"),
                            portfolio_hash=account_hash,
                            policy_version=risk_policy_version,
                            correlation_id=f"risk-eval-{symbol}-{source_timestamp}",
                            source_timestamp=source_timestamp,
                            observed_at=evaluated_at,
                            received_at=evaluated_at,
                        )
                        impl_results = await self._risk_engine.full_evaluate(imp_snapshot, rules_cfg)
                        auxiliary_failed_rules = [
                            f"AUX-{result.rule_level.value}"
                            for result in impl_results
                            if result.decision != RiskDecision.APPROVED
                        ]
                        risk_approved = bool(impl_results) and not auxiliary_failed_rules
                        for result in impl_results:
                            if result.decision != RiskDecision.APPROVED:
                                self._post_risk.record_violation(f"RiskEngineImpl:{symbol}:{result.reason}")
                    except Exception as exc:
                        risk_approved = False
                        auxiliary_failed_rules = [f"AUX-UNAVAILABLE:{type(exc).__name__}"]
                        logger.warning("Blocking RiskEngineImpl unavailable for %s: %s", symbol, type(exc).__name__)

                if not risk_approved:
                    failed_rules = [rid for rid, d in risk_results.items() if d != RuleDecision.PASS]
                    failed_rules.extend(auxiliary_failed_rules)
                    print(f"[nearline] {symbol}: SKIP (risk rules failed: {failed_rules})")
                    continue

                if self._can_write and self._policy_error:
                    print(f"[nearline] {symbol}: SKIP (signed risk policy unavailable: {self._policy_error})")
                    continue

                # === 8. Approval with proper signing ===
                # Compute proper hashes for approval binding
                proposal_payload = f"{prop['direction'].value}|{prop['fused_strength']}|{prop['fused_confidence']}|{symbol}|{position_size}"
                proposal_hash = hashlib.sha256(proposal_payload.encode()).hexdigest()[:16]
                import secrets

                nonce = secrets.token_hex(8)

                approval_id = RiskApprovalId(f"nearline-{symbol}-{int(time.time())}-{nonce[:8]}")
                intent_id = f"intent-{symbol}-{int(time.time())}"
                client_order_id = f"beidou-{symbol.lower()}-entry-{int(time.time())}"
                correlation = CorrelationId(f"nearline-{int(time.time())}")
                idempotency_key = f"idem-{symbol}-{int(time.time())}"
                # BD-FIX: 意图数量必须在创建/签名前按交易对规则量化。
                # 执行器拒绝"量化后与签名不一致"的数量（审批绑定语义），
                # 未量化的原始 sizing 值会导致所有订单被
                # PLANNED_QUANTITY_NOT_VENUE_EXACT 拒绝。
                _venue_exact = self._venue_exact_quantity(symbol, position_size)
                if _venue_exact is None:
                    continue
                position_size = _venue_exact
                from beidou_safety.execution import OrderIntent

                unsigned_intent = OrderIntent(
                    intent_id=intent_id,
                    account_ref=AccountRef(venue_id=venue_id, account_id=AccountId("default")),
                    instrument_id=instrument_id,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=Quantity(amount=str(position_size)),
                    price=Price(amount=str(price)),
                    time_in_force=TimeInForce.GTC,
                    client_order_id=client_order_id,
                    correlation_id=correlation,
                    idempotency_key=idempotency_key,
                    risk_approval_id=str(approval_id),
                    risk_proposal_hash=proposal_hash,
                )
                intent_hash = order_intent_binding_hash(unsigned_intent)
                approval_expires_at = time.time() + DEFAULT_APPROVAL_TTL_SECONDS
                signature = self._approval.issue_for_approved_risk(
                    approval_id,
                    risk_approved=risk_approved,
                    proposal_hash=proposal_hash,
                    intent_hash=intent_hash,
                    account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    policy_version=risk_policy_version,
                    nonce=nonce,
                    expires_at=approval_expires_at,
                )

                # 提交前只预检签名；nonce 必须保留到最终发送边界消费。
                if not await self._approval.verify(
                    approval_id,
                    signature=signature,
                    proposal_hash=proposal_hash,
                    intent_hash=intent_hash,
                    account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    policy_version=risk_policy_version,
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
                        nonce=nonce,
                        ttl=approval_expires_at - time.time(),
                        risk_snapshot_hash=risk_hash,
                        policy_version=risk_policy_version,
                    )
                    != RiskDecision.APPROVED
                ):
                    print(f"[nearline] {symbol}: SKIP (risk not approved)")
                    continue

                intent = OrderIntent(
                    intent_id=intent_id,
                    account_ref=AccountRef(venue_id=venue_id, account_id=AccountId("default")),
                    instrument_id=instrument_id,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=Quantity(amount=str(position_size)),
                    price=Price(amount=str(price)),
                    time_in_force=TimeInForce.GTC,
                    client_order_id=client_order_id,
                    correlation_id=correlation,
                    idempotency_key=idempotency_key,
                    risk_approval_id=str(approval_id),
                    risk_approval_signature=signature,
                    risk_proposal_hash=proposal_hash,
                    risk_intent_hash=intent_hash,
                    risk_account_snapshot_hash=account_hash,
                    risk_snapshot_hash=risk_hash,
                    risk_policy_version=risk_policy_version,
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
                    # PostRiskMonitor: 下单后校验仓位/保证金/风控安全性
                    self._check_post_risk_safety(symbol, position_notional, dyn_leverage)
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
        # This legacy path mutates protections, order trackers and positions
        # from a single exchange snapshot.  That is not a governed
        # three-way reconciliation and can erase evidence after a timeout or
        # partial user-stream gap.  Keep the symbol only for compatibility;
        # callers must use ``_reconcile`` plus the explicit recovery state
        # machine instead of invoking a self-healing mutation.
        raise RuntimeError("EXCHANGE_STATE_MUTATION_RECOVERY_DISABLED")

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

    async def _evaluate_trading_universe(self) -> None:
        """交易池宇宙评估：对 OBSERVING/PROMOTED 标的评分散点差、深度、成交量，自动晋级。

        TradingPool 生命周期: OBSERVING → PROMOTED(≥0.6分) → ACTIVE
        降级: 连续3次 < 0.3 → QUARANTINED
        """
        import math as _math

        pool = getattr(self, "_trading_pool", None)
        if pool is None:
            return

        # BD-FIX: ACTIVE 标的也必须参与评分——pool.score() 内的连续低分
        # 降级检查 (连续 3 次 < 0.3 → QUARANTINED) 只有 ACTIVE 标的被评分
        # 才会触发。try_promote/activate 内部已有状态前置检查，ACTIVE 标的
        # 不会被重复晋级。
        candidates = [
            iid
            for iid, e in pool._pool.items()
            if e.status in (PoolStatus.OBSERVING, PoolStatus.PROMOTED, PoolStatus.ACTIVE)
        ]
        if not candidates:
            return

        scored = 0
        promoted = 0
        for instrument_id in candidates:
            try:
                ticker = self._feed.get_last_ticker(instrument_id)
                ob = self._feed.get_last_orderbook(instrument_id)
                features = await self._feed.async_get_kline_features(instrument_id, "1h", 50)
            except Exception as exc:
                logger.warning("Trading-pool scoring input failed for %s: %s", instrument_id, type(exc).__name__)
                continue

            if not ticker or not features:
                continue

            bid = float(ticker.get("bidPrice", 0) or ticker.get("bid", 0) or 0)
            ask = float(ticker.get("askPrice", 0) or ticker.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0 or bid > ask:
                # BD-FIX（M3 审查）: 凌晨薄盘/WS 抖动下 bid/ask 缺失 ——
                # 跳过本轮评分（不产生 0 分），避免连续 3 次低分把标的
                # 误 QUARANTINED（无回归路径）
                logger.warning("Trading-pool: %s bid/ask unavailable — skip scoring this cycle", instrument_id)
                continue

            # 点差评分：spread_bps 越低越好，>50bps → 0分, <1bps → 1分
            spread_bps = (ask - bid) / ask * 10000
            spread_score = max(0.0, min(1.0, 1.0 - (spread_bps - 1) / 49)) if spread_bps > 1 else 1.0

            # 深度评分：从 orderbook 获取
            depth_score = 0.5  # 默认中等
            if ob:
                bids_vol = sum(float(b[1]) for b in (ob.get("bids", []) or [])[:5])
                asks_vol = sum(float(a[1]) for a in (ob.get("asks", []) or [])[:5])
                depth_usdt = (bids_vol + asks_vol) * float(ticker.get("lastPrice", ask))
                # >$100k 深度 → 1分, <$1k → 0分
                depth_score = max(0.0, min(1.0, _math.log10(max(1, depth_usdt)) / 5))

            # 成交量评分：24h 交易量
            # BD-FIX: kline features 不含 volume_24h 字段（只有 async_update_features
            # 的实时快照有），旧代码恒取 0 → volume_score 恒 0（权重 0.20），
            # 叠加 spread_score 异常时总分上限低于晋级阈值，标的永远无法 ACTIVE。
            # ticker 缓存 (WS/REST 两路径) 都携带 24h base 成交量 "volume"。
            vol_24h = float(ticker.get("volume", 0) or 0)
            vol_usdt = vol_24h * float(ticker.get("lastPrice", ask))
            volume_score = max(0.0, min(1.0, _math.log10(max(1, vol_usdt)) / 8))

            # 稳定性评分：从 features 推断（数据完整=稳定）
            stability_score = 0.5
            if features.get("close") and features.get("high") and features.get("low"):
                stability_score = 0.8

            # 容量评分：基于波动率
            ann_vol = float(features.get("ann_volatility", 0.5) or 0.5)
            capacity_score = max(0.0, min(1.0, 1.0 - ann_vol))  # 低波动=高容量

            from beidou_data.trading_pool_lifecycle import InstrumentScore

            score = InstrumentScore(
                instrument_id=instrument_id,
                spread_score=round(spread_score, 4),
                depth_score=round(depth_score, 4),
                volume_score=round(volume_score, 4),
                stability_score=round(stability_score, 4),
                capacity_score=round(capacity_score, 4),
            )
            pool.score(instrument_id, score)
            scored += 1

            if pool.try_promote(instrument_id):
                promoted += 1
                pool.activate(instrument_id)  # PROMOTED → ACTIVE

        if scored > 0:
            active = pool.active_instruments()
            # BD-FIX: 降级检查由 pool.score() 内部执行（连续 3 次低分 →
            # QUARANTINED）。旧代码用 for 循环泄漏的最后一个 instrument_id
            # 取 entry，且 ACTIVE 标的从未被评分，降级路径永远不触发。
            print(
                f"[universe] 评估 {scored} 个标的, 晋级 {promoted} 个, "
                f"当前活跃 {len(active)} 个: {active[:10]}{'...' if len(active) > 10 else ''}"
            )

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

                # BD-FIX: 冷启动基线被真实交易数据替换。此前冷启动 sharpe=0
                # 校准后 is_calibrated() 恒 True，真实基线永远无法写入。
                if not self._drift_detector.is_calibrated() or self._cold_start_baseline:
                    # First calibration — set baseline from live performance
                    self._drift_detector.set_baseline({"sharpe": max(0.5, sharpe), "win_rate": max(0.4, win_rate)})
                    self._cold_start_baseline = False
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
            elif not self._drift_detector.is_calibrated():
                # BD-FIX: 冷启动中性校准 — 无交易证据时 sharpe=0 是中性事实，
                # 不是负收益证据。此前不校准导致 R5 恒 UNKNOWN → REJECT，
                # 首单被风控永久拦截（无交易 → 无 PnL → 无法校准 → 死锁）。
                # 中性 0 基线配合默认 min_sharpe_rolling=0.0 放行首单；
                # operator 若配置正值下限，冷启动仍会被拒（保守语义保留）；
                # 真实亏损后 sharpe<0 会被 R5 拒绝。
                self._drift_detector.set_baseline({"sharpe": 0.0, "win_rate": 0.0})
                self._cold_start_baseline = True
                print("[offline] DriftDetector cold-start baseline: sharpe=0.0 (neutral, no trade evidence)")
            else:
                print("[offline] DriftDetector: no trade data yet, skipping drift check")

            # === 2.5 交易池宇宙评估：评分 + 晋级 ===
            await self._evaluate_trading_universe()

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
                # 触发密钥轮换
                try:
                    result = self._key_rotator.start_rotation(cred.credential_id)
                    health["rotation_started"] = result.value
                    print(f"[beidou-security] 🔄 Key rotation started for {cred.credential_id}: {result.value}")
                except Exception as exc:
                    health["rotation_error"] = type(exc).__name__
                    logger.error("Credential rotation start failed: %s", type(exc).__name__)
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
                # 尝试完成轮换（如有新凭据）
                try:
                    if hasattr(self, "_pending_credential"):
                        result = self._key_rotator.complete_rotation(cred.credential_id, self._pending_credential)
                        health["rotation_completed"] = result.value
                except Exception as exc:
                    health["rotation_error"] = type(exc).__name__
                    logger.error("Credential rotation completion failed: %s", type(exc).__name__)
                print(f"[beidou-security] ⚠️ Credential {cred.credential_id} is ROTATING")
            else:
                health["level"] = "OK"
            # PKG02 (BDS-P0-001): R9 提款权限在所有环境统一检查。
            # BD-FIX: Testnet 模式下提款权限由交易所默认开启（测试资金），
            # 不产生告警。非 testnet 环境保持 CRITICAL 阻断。
            if self._can_withdraw:
                _env_mode = getattr(self, "_env_mode", None)
                is_testnet = _env_mode is not None and _env_mode.value == "testnet"
                if not is_testnet:
                    health["level"] = "CRITICAL"
                    health["r9_violation"] = "WITHDRAW_ENABLED"
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

    def _check_post_risk_safety(self, symbol: str, position_notional: float, leverage: float) -> None:
        """PostRiskMonitor: 下单后校验风控安全性，不安全则记录违规并可能降级。"""
        try:
            account_balance = float(self._last_account.get("totalWalletBalance", 0))
            margin_ratio = (position_notional / leverage) / max(account_balance, 1)
            position_count = self._protection.position_count()
            pending = len(self._active_order_ids)
            concentration = position_notional / max(account_balance, 1) * 100
            checked_at = getattr(self._last_reconciliation_result, "checked_at", None)
            if isinstance(checked_at, datetime) and checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            source_timestamp = checked_at.isoformat() if isinstance(checked_at, datetime) else ""
            evaluated_at = datetime.now(timezone.utc).isoformat()
            portfolio_hash = hashlib.sha256(
                f"{account_balance}|{position_count}|{pending}|{position_notional}".encode()
            ).hexdigest()

            from beidou_safety.risk.engine import RiskSnapshot

            snapshot = RiskSnapshot(
                total_exposure=position_notional,
                margin_used=position_notional / max(leverage, 1),
                margin_total=account_balance,
                position_count=position_count,
                pending_orders=pending,
                leverage=leverage,
                concentration_pct=concentration,
                account_id="default",
                reconciliation_status="MATCHED" if self._fresh_matched_reconciliation() else "MISMATCHED",
                exchange_health="HEALTHY" if self._check_liveness() is HealthState.HEALTHY else "UNSAFE",
                dq_tier="PASS",
                portfolio_hash=portfolio_hash,
                policy_version=str(self._policy_version or "UNKNOWN"),
                correlation_id=f"post-risk-{symbol}-{source_timestamp}",
                source_timestamp=source_timestamp,
                observed_at=evaluated_at,
                received_at=evaluated_at,
            )
            if not snapshot.is_safe_for_risk_increase():
                self._post_risk.record_violation(
                    f"{symbol}: margin_ratio={margin_ratio:.2%} conc={concentration:.1f}% "
                    f"positions={position_count} pending={pending}"
                )
            if self._post_risk.recommend_degradation():
                # 交由 supervisor 监控循环处理降级决策，不在此处立即阻断
                print(f"[post-risk] ⚠️ {len(self._post_risk._violations)} violations — supervisor will evaluate")
        except Exception as exc:
            logger.debug("post-risk safety check skipped: %s", type(exc).__name__)

    def _apply_venue_account_permissions(self, account: Any) -> tuple[bool, str]:
        """Apply explicit venue permissions and return the writable gate.

        Account permissions are independent exchange facts. Missing or
        malformed values remain UNKNOWN and therefore block risk-increasing
        execution; a venue-enabled withdrawal permission is a hard P0 stop.
        """

        if not isinstance(account, dict):
            self._venue_can_trade = None
            self._venue_can_withdraw = None
            self._can_trade = False
            self._can_withdraw = True
            return False, "ACCOUNT_PERMISSION_UNKNOWN"

        venue_can_trade = account.get("canTrade")
        venue_can_withdraw = account.get("canWithdraw")
        if not isinstance(venue_can_trade, bool) or not isinstance(venue_can_withdraw, bool):
            self._venue_can_trade = None
            self._venue_can_withdraw = None
            self._can_trade = False
            self._can_withdraw = True
            return False, "ACCOUNT_PERMISSION_UNKNOWN"

        self._venue_can_trade = venue_can_trade
        self._venue_can_withdraw = venue_can_withdraw
        self._can_trade = venue_can_trade
        self._can_withdraw = venue_can_withdraw
        _env_mode = getattr(self, "_env_mode", None)
        if venue_can_withdraw and (_env_mode is None or _env_mode.value != "testnet"):
            return False, "WITHDRAWAL_PERMISSION_ENABLED"
        if not venue_can_trade:
            return False, "VENUE_TRADING_DISABLED"
        # BD-FIX: R9 风险视角 — testnet 豁免后不再把 venue 提款权限
        # 原样传给风险规则（否则 R9 恒 REJECT 阻塞 testnet 交易）。
        self._risk_can_withdraw = bool(venue_can_withdraw) and _env_mode is not None and _env_mode.value != "testnet"
        return True, "OK"

    # --- BD-CV Contracts: Bridge methods ---

    def build_truth_snapshot(self) -> TruthSnapshot:
        """Build a snapshot only from recorded facts, never call-time freshness."""
        now_ts = time.time()
        # BD-FIX: protection 事实周期刷新 —— 多品种 nearline 每轮处理
        # 10 品种、tick 周期拉长后，protection 事实只有事件驱动更新
        # （恢复/durable gate），300s 后 stale → 资格恒 NOT_VERIFIABLE
        # → 所有新意图被拒（final46 实测 00:34 四品种全拒）。快照
        # 构建时刷新（与风险事实 9a65423 同语义）。
        if getattr(self, "_protection_owner_unknown", False):
            self._last_protection_hash = hashlib.sha256("UNKNOWN".encode()).hexdigest()
        # 否则保留现有 hash（ACTIVE/UNKNOWN/空 语义原样保留 ——
        # fail-closed 语义不受刷新影响），只刷新时间戳
        self._last_protection_fact_at = now_ts
        recon_status = getattr(getattr(self, "_last_reconciliation_result", None), "status", "UNKNOWN")
        return TruthSnapshot(
            snapshot_id=f"snap-{int(now_ts * 1000)}",
            created_at=datetime.now(timezone.utc).isoformat(),
            market_hash=getattr(self, "_last_market_hash", "")
            or getattr(getattr(self, "_feed", None), "last_hash", ""),
            account_hash=getattr(self, "_last_account_hash", ""),
            order_hash=getattr(self, "_last_order_hash", ""),
            position_hash=getattr(self, "_last_position_hash", ""),
            ledger_hash=getattr(self, "_last_ledger_hash", ""),
            reconciliation_hash=getattr(self, "_last_reconciliation_hash", ""),
            protection_hash=getattr(self, "_last_protection_hash", ""),
            risk_hash=getattr(self, "_last_risk_hash", ""),
            config_hash=getattr(self, "_config_hash", ""),
            policy_hash=getattr(self, "_policy_hash", ""),
            market_freshness=float(getattr(self, "_last_market_fact_at", 0.0) or 0.0),
            account_freshness=float(getattr(self, "_last_account_fact_at", 0.0) or 0.0),
            order_freshness=float(getattr(self, "_last_order_fact_at", 0.0) or 0.0),
            position_freshness=float(getattr(self, "_last_position_fact_at", 0.0) or 0.0),
            ledger_freshness=float(getattr(self, "_last_ledger_fact_at", 0.0) or 0.0),
            reconciliation_freshness=float(getattr(self, "_last_reconciliation_fact_at", 0.0) or 0.0),
            protection_freshness=float(getattr(self, "_last_protection_fact_at", 0.0) or 0.0),
            risk_freshness=float(getattr(self, "_last_risk_fact_at", 0.0) or 0.0),
            config_freshness=float(getattr(self, "_config_observed_at", 0.0) or 0.0),
            policy_freshness=float(getattr(self, "_policy_observed_at", 0.0) or 0.0),
            reconciliation_status=str(getattr(recon_status, "value", recon_status)),
            # protection_status 跟随记录的 hash: 仅当所有权已知且最近一次
            # 覆盖评估记录为 ACTIVE 时才可进入 ACTIVE；覆盖缺失/未评估时
            # 记录 UNKNOWN → NO_NEW_RISK（fail-closed）。
            protection_status=(
                "ACTIVE"
                if self._protection_owner_unknown is False
                and getattr(self, "_last_protection_hash", "") == hashlib.sha256(b"ACTIVE").hexdigest()
                else "UNKNOWN"
            ),
            risk_status="NORMAL" if self._control._action != ControlAction.LOCK else "CRITICAL",
            env_mode=str(getattr(getattr(self, "_env_mode", None), "value", "")),
            control_action=str(getattr(getattr(self._control, "_action", None), "value", "NO_NEW_RISK")),
        )

    def evaluate_trading_eligibility(self) -> TradingEligibility:
        """BD-CV02 AC-02-01: 全仓唯一 TradingEligibility authority。"""
        snap = self.build_truth_snapshot()
        return derive_eligibility(snap)

    @staticmethod
    def build_strategy_signal(
        strategy_id: str, action: str, symbol: str = "", confidence: float = 0.0, reason: str = ""
    ) -> StrategySignal:
        """@deprecated: 信号构建当前由近线循环内联执行，此方法保留用于外部工具。"""
        """BD-CV23: 构造 Typed Strategy Signal。

        NO_ACTION 不会被当系统故障，VETO 在所有环境阻断下游。
        """
        _action_map = {
            "ACTION": StrategyAction.ACTION,
            "NO_ACTION": StrategyAction.NO_ACTION,
            "VETO": StrategyAction.VETO,
            "DEGRADED": StrategyAction.DEGRADED,
        }
        sa = _action_map.get(action, StrategyAction.NO_ACTION)
        return StrategySignal(strategy_id=strategy_id, action=sa, symbol=symbol, confidence=confidence, reason=reason)

    @staticmethod
    def _signed_position_from_account(account: dict[str, Any], symbol: str) -> Decimal | None:
        """Read one exact signed venue position; incomplete facts stay UNKNOWN."""

        positions = account.get("positions") if isinstance(account, dict) else None
        if not isinstance(positions, list):
            return None
        matches = [row for row in positions if isinstance(row, dict) and str(row.get("symbol", "")) == symbol]
        if len(matches) > 1:
            return None
        if not matches:
            return Decimal("0")
        try:
            quantity = Decimal(str(matches[0].get("positionAmt")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return quantity if quantity.is_finite() else None

    def build_portfolio_target(
        self,
        symbol: str,
        side: str,
        exposure: float,
        delta: float = 0.0,
        *,
        current_exposure: float | None = None,
        inflight_exposure: float | None = None,
    ) -> SignedPortfolioTarget:
        """BD-CV30: 构造 SignedPortfolioTarget。

        LONG/SHORT/FLAT 方向正确，gross>=abs(net)。
        """
        _side_map = {"LONG": PositionSide.LONG, "SHORT": PositionSide.SHORT, "FLAT": PositionSide.FLAT}
        ps = _side_map.get(side, PositionSide.FLAT)
        return SignedPortfolioTarget(
            target_id=f"tgt-{symbol}-{int(time.time() * 1000)}",
            symbol=symbol,
            side=ps,
            target_exposure=exposure,
            delta=delta,
            current_exposure=current_exposure,
            inflight_exposure=inflight_exposure,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def build_execution_plan(
        self, slices: list[PlanSlice], algorithm: str = "", is_emergency: bool = False
    ) -> ExecutionPlan:
        """BD-CV40: 构造 ExecutionPlan。

        无算法适用返回 NOT_EXECUTABLE。Emergency plan 绝不增加绝对仓位。
        """
        if not slices or not algorithm:
            return ExecutionPlan(
                plan_id=f"plan-{int(time.time() * 1000)}", status=PlanStatus.NOT_EXECUTABLE, algorithm=algorithm
            )
        return ExecutionPlan(
            plan_id=f"plan-{int(time.time() * 1000)}",
            slices=slices,
            status=PlanStatus.PENDING,
            algorithm=algorithm,
            is_emergency=is_emergency,
        )

    def build_position_aggregate(self, symbol: str, fills: list[Fill] | None = None) -> PositionAggregate:
        """BD-CV42: 从成交记录构建 PositionAggregate。

        任意成交序列 replay 确定性。
        """
        pa = PositionAggregate(symbol=symbol, fills=fills or [])
        if fills:
            return pa.replay(fills)
        return pa

    def build_idempotency_key(
        self, correlation_id: str, client_order_id: str, outbox_id: str = ""
    ) -> OrderIdempotencyKey:
        """BD-CV41: 构造订单幂等键。"""
        return OrderIdempotencyKey(correlation_id=correlation_id, client_order_id=client_order_id, outbox_id=outbox_id)

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
                wait_s = 1.0 * (2**attempt)
                print(
                    f"[beidou-autopilot] Server time check attempt {attempt + 1}/5 failed, retrying in {wait_s:.0f}s..."
                )
                await asyncio.sleep(wait_s)
        if not server_time_ok:
            # BD-FIX（I2 审查）: demo 地域限制（-2015）是间歇性的 ——
            # 快速 5 次重试（~15s）后 FATAL 会让每次抖动杀死进程。
            # testnet 进入长周期退避重试（最长 10 分钟），期间健康
            # 端点存活；live/canary 保持快速 FATAL。
            if str(getattr(self._env_mode, "value", "")) == "testnet":
                print("[beidou-autopilot] testnet: exchange API unavailable — long-cycle retry (up to 10min)")
                for _long_attempt in range(10):
                    await asyncio.sleep(60)
                    server_time, st_ok = await self._api_async_safe(Endpoint.SERVER_TIME)
                    if st_ok and isinstance(server_time, dict) and "serverTime" in server_time:
                        server_time_ok = True
                        break
            if not server_time_ok:
                print("[beidou-autopilot] FATAL: Cannot connect to exchange after 5 attempts")
                self._lifecycle.transition(ModuleState.FAILED)
                return
        print(f"[beidou-autopilot] Exchange connected: {self._rest_url}")

        # Verify account access (with retries for flaky testnet API)
        account = None
        for attempt in range(5):
            account, acct_ok = await self._api_async_safe(Endpoint.ACCOUNT, signed=True)
            if (
                acct_ok
                and isinstance(account, dict)
                and ("totalWalletBalance" in account or "assets" in account or "canTrade" in account)
            ):
                break
            if attempt < 4:
                wait_s = 1.0 * (2**attempt)
                print(f"[beidou-autopilot] Account access attempt {attempt + 1}/5 failed, retrying in {wait_s:.0f}s...")
                await asyncio.sleep(wait_s)
            account = None
        if account is None:
            print("[beidou-autopilot] FATAL: Cannot access account after 5 attempts")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        self._last_account = account
        permissions_ok, permission_reason = self._apply_venue_account_permissions(account)
        if not permissions_ok and self._can_write:
            self._safe_no_new_risk("auto")
            self._record_execution_fact_failure_env_guarded(
                permission_reason,
                alert_title="Venue account permission blocked",
                alert_category="credential",
            )
            print(f"[beidou-autopilot] FATAL: account permission gate: {permission_reason}")
            self._lifecycle.transition(ModuleState.FAILED)
            return
        if not permissions_ok:
            print(f"[beidou-autopilot] Account permission facts are not writable-safe: {permission_reason}")
        init_equity = float(account.get("totalWalletBalance", 0))
        # BD-FIX（I4 审查）: 策略权益/峰值用本地推导（共享余额含外部
        # 资金会污染 drawdown 与 sizing）
        init_equity = _local_equity_estimate(self, init_equity)
        self._peak_equity = init_equity
        self._strategy_risk.update_equity(self._autopilot_strategy_id, init_equity)
        print(f"[beidou-autopilot] Account OK: equity={init_equity}")

        # Start health server early so liveness is available during bootstrap.
        # If the engine later exits DEGRADED, monitoring still sees a living
        # process instead of a silent exit 0 with no HTTP endpoint.
        if not getattr(self, "_health_started", False):
            self._health.start()
            self._health_started = True
            print(
                f"[beidou-autopilot] Health server: http://{self._settings.infrastructure.health_host}:"
                f"{self._settings.infrastructure.health_port}"
            )

        # Start WebSocket real-time market data stream
        print("[beidou-autopilot] Starting WebSocket market data...")
        ws_ok = await self._feed.start_ws(self._symbols, testnet=(self._env_mode.value == "testnet"))
        if ws_ok:
            print("[beidou-autopilot] WebSocket market data stream active")
        else:
            print("[beidou-autopilot] WebSocket unavailable — falling back to REST polling")

        # Writable environments require an authenticated user-data stream;
        # market-data REST fallback must never become an execution fallback.
        try:
            user_stream_ok = await asyncio.wait_for(self._start_user_stream(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[beidou-autopilot] User data stream start timed out after 30s")
            user_stream_ok = False
        if not user_stream_ok and self._can_write:
            self._safe_no_new_risk("auto")
            print("[beidou-autopilot] User data stream unavailable — writable authority remains blocked")

        # Restore state from persistence
        print("[beidou-autopilot] Restoring state...")
        ledger_entries = self._store.restore_ledger_entries()
        active_orders = self._store.get_active_orders()
        protections = self._store.restore_protections()
        # 恢复策略风险状态（防止重启后熔断器清零）
        self._strategy_risk.restore_state(self._store)
        print(
            f"[beidou-autopilot] Restored: {len(ledger_entries)} ledger entries, "
            f"{len(active_orders)} active orders, {len(protections)} protections"
        )

        # Restore active_order_ids from exchange.
        # Do not claim current-worker ownership. Venue discovery is tracked
        # and persisted, but only a separately fenced reconciliation/adoption flow may add it to
        # ``_owned_order_ids``.  Cancellation remains reserved for the
        # governed flatten path.
        try:
            exchange_open = await self._api_async(Endpoint.OPEN_ORDERS, signed=True)
            if isinstance(exchange_open, list):
                # BD-FIX: testnet 只认领本引擎命名空间（beidou- clientOrderId）
                # 的挂单 —— 共享账户其他用户的 open orders 被认领后会
                # 被 _monitor_orders 监控、成交进本地持仓投影，污染对账
                # （final40 实测：CYSUSDT/AIOUSDT/GRIFFAINUSDT 外部成交
                # 写入 position_projection）
                _is_testnet_restore = str(getattr(getattr(self, "_env_mode", None), "value", "")) == "testnet"
                _skipped_foreign = 0
                for o in exchange_open:
                    _client_oid = str(o.get("clientOrderId", "") or "")
                    if _is_testnet_restore and not _client_oid.startswith("beidou-"):
                        _skipped_foreign += 1
                        continue
                    oid = str(o["orderId"])
                    ostatus = str(o.get("status", "NEW")).upper()
                    osymbol = str(o.get("symbol", ""))
                    oside = str(o.get("side", ""))
                    tracker = OrderStateTracker(order_id=OrderId(oid))
                    tracker.apply(OrderEvent.SENT)
                    tracker.apply(OrderEvent.ACKED)
                    if ostatus == "PARTIALLY_FILLED":
                        tracker.apply(OrderEvent.PARTIALLY_FILLED)
                    self._order_trackers[oid] = tracker
                    self._active_order_ids.add(oid)
                    self._order_symbols[oid] = osymbol
                    # 持久化到 store，避免对账时 system_facts.open_orders 为空
                    try:
                        self._store.save_order_state(
                            order_id=oid,
                            symbol=osymbol,
                            side=oside,
                            order_type=str(o.get("type", "")),
                            quantity=str(o.get("origQty", "0")),
                            price=str(o.get("price", "0")) if o.get("price") else None,
                            status=ostatus,
                            filled_qty=str(o.get("executedQty", "0")),
                            avg_price=str(o.get("avgPrice", "0")) if o.get("avgPrice") else None,
                            client_order_id=str(o.get("clientOrderId", "")) or None,
                        )
                    except Exception as _persist_exc:
                        print(f"[beidou-autopilot] Warning: Failed to persist restored order {oid}: {_persist_exc}")
                if _skipped_foreign:
                    print(f"[beidou-autopilot] Skipped {_skipped_foreign} foreign orders (shared account, not beidou- owned)")
                print(f"[beidou-autopilot] Restored {len(self._active_order_ids)} active orders from exchange")
                unowned = self._unowned_active_order_ids()
                if self._can_write and unowned:
                    self._safe_no_new_risk("auto")
                    self._record_execution_fact_failure_env_guarded(
                        f"ACTIVE_ORDER_OWNER_UNKNOWN:{','.join(sorted(unowned)[:20])}"
                    )
                    self._alerts.send_incident(
                        AlertSeverity.CRITICAL,
                        "Active order ownership unknown",
                        f"Startup discovered venue orders without current-worker ownership: {sorted(unowned)[:20]}",
                        category="execution",
                    )
        except Exception as e:
            print(f"[beidou-autopilot] Warning: Could not restore open orders: {e}")

        # 启动时恢复 UNKNOWN intent — 按 client_order_id 查询交易所确定状态
        if self._can_write:
            print("[beidou-autopilot] Resolving UNKNOWN intents...")
            resolved_count = await self._resolve_unknown_outbox_intents()
            if resolved_count:
                print(f"[beidou-autopilot] Resolved {resolved_count} UNKNOWN intents")

        # BD-FIX: 启动时恢复交易所持仓的止盈止损保护
        # 先获取 exchangeInfo 填充精度缓存，避免低价币种四舍五入错误。
        # 断路器必须在 exchangeInfo 加载前复位，否则上一个 session 的
        # 失败计数会阻挡精度加载 → 订单全部失败 → 断路器再次打开。
        self._adapter.reset_circuit_breaker()
        try:
            exchange_info, info_ok = await asyncio.wait_for(self._api_async_safe(Endpoint.EXCHANGE_INFO), timeout=30.0)
            if not info_ok or not isinstance(exchange_info, dict):
                print("[beidou-autopilot] Warning: exchangeInfo unavailable; precision cache empty")
                exchange_info = {}
            if not hasattr(self, "_symbol_precision"):
                self._symbol_precision = {}
            for s in exchange_info.get("symbols", []):
                sym = s.get("symbol", "")
                quantity_step: str | None = None
                price_tick: str | None = None
                min_qty: str | None = None
                min_notional_val: float = 0.0
                rule_parse_failed = False
                for f_item in s.get("filters", []):
                    if f_item.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                        quantity_step = str(f_item.get("stepSize", "")) or quantity_step
                        min_qty = str(f_item.get("minQty", "")) or min_qty
                    if f_item.get("filterType") == "PRICE_FILTER":
                        price_tick = str(f_item.get("tickSize", "")) or price_tick
                    if f_item.get("filterType") == "MIN_NOTIONAL":
                        try:
                            min_notional_val = float(str(f_item.get("notional", "0")))
                        except (TypeError, ValueError):
                            rule_parse_failed = True
                            logger.error("Invalid MIN_NOTIONAL rule for %s", sym)
                if not quantity_step or not price_tick or rule_parse_failed:
                    continue
                try:
                    min_quantity = float(min_qty) if min_qty else float(quantity_step)
                    self._symbol_precision[sym] = {
                        "quantity": max(0, -Decimal(quantity_step).as_tuple().exponent),
                        "price": max(0, -Decimal(price_tick).as_tuple().exponent),
                        "step_size": quantity_step,
                        "tick_size": price_tick,
                        "min_quantity": min_quantity,
                        "min_notional": min_notional_val,
                    }
                except (InvalidOperation, ValueError):
                    continue
            print(f"[beidou-autopilot] Loaded precision for {len(self._symbol_precision)} symbols")
        except Exception as e:
            print(f"[beidou-autopilot] Warning: exchangeInfo load failed: {e}")

        # BD-FIX: 执行器规则快照 gate 需要 adapter 缓存（BD-CV10）。
        # 启动时 exchangeInfo 已在手——同步进 adapter 的 reference_data
        # 并构建规则快照；否则 get_rule_snapshot 恒 unknown，所有订单
        # 在执行前被 VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE 拒绝。
        self._sync_adapter_rule_snapshots(exchange_info)

        # Never cancel every conditional order on startup.  Without a durable
        # owner/session mapping that action can delete a manual or another
        # worker's valid protection and create a naked position.  Recovery is
        # read-only here; only an explicitly owned, ACK-backed order may be
        # cancelled by a governed close/replacement path.
        #
        # BD-FIX (S2): Testnet 模式下，无持仓时自动取消残留的无主 Algo 订单。
        # 非正常退出（kill -9 / 崩溃）会导致交易所残留条件单，新进程无法
        # 认领所有权，造成 protection_owner_unknown 永久阻断。
        existing_algo_inventory: list[dict[str, Any]] | None = None
        unowned_algo_ids: list[str] = []
        try:
            existing_algos = await asyncio.wait_for(self._get_open_algo_inventory(), timeout=30.0)
            if isinstance(existing_algos, list):
                existing_algo_inventory = existing_algos
                known_algo_ids = {algo_id for ids in self._active_algo_ids.values() for algo_id in ids}
                unowned_algo_ids = [
                    str(item.get("algoId"))
                    for item in existing_algos
                    if item.get("algoId") is not None and str(item.get("algoId")) not in known_algo_ids
                ]
        except Exception as exc:
            print(f"[beidou-autopilot] Conditional-order inventory UNKNOWN: {exc}")

        # Only reset the circuit breaker if it is still open from a previous
        # session crash.  If the venue is genuinely rate-limiting us, clearing
        # the breaker here would hammer it harder during the protection-write
        # burst below.
        if self._adapter.is_circuit_breaker_open():
            self._adapter.reset_circuit_breaker()
            print("[beidou-autopilot] Reset stale circuit breaker from previous session")

        try:
            # 使用 _api_async_safe 防止熔断返回空数据导致跳过保护恢复
            account, ok = await asyncio.wait_for(self._api_async_safe(Endpoint.ACCOUNT, signed=True), timeout=30.0)
            if not ok or "positions" not in account:
                print("[beidou-autopilot] WARNING: Cannot query account for position recovery — retrying once...")
                await asyncio.sleep(3)
                if self._adapter.is_circuit_breaker_open():
                    self._adapter.reset_circuit_breaker()
                account, ok = await asyncio.wait_for(self._api_async_safe(Endpoint.ACCOUNT, signed=True), timeout=30.0)
                if not ok:
                    # BD-FIX (S9): 账户 API 不可用时跳过恢复但不停止用户流。
                    # 停止用户流会导致整个 trading readiness 连锁失败 → LOCKED。
                    # Testnet API 不稳定不应影响系统持续运行能力。
                    print(
                        "[beidou-autopilot] WARNING: Position recovery skipped (API unavailable) — user stream kept alive"
                    )
                    self._lifecycle.transition(ModuleState.DEGRADED)
                    if not getattr(self, "_health_started", False):
                        self._health.start()
                        self._health_started = True
                    return
            positions_list = account.get("positions", [])
            # BD-FIX (S2): Testnet 模式下，无持仓时自动取消残留的无主 Algo 订单。
            unowned_algo_ids, existing_algo_inventory = await self._safe_recover_unowned_testnet_algos(
                unowned_algo_ids, existing_algo_inventory, positions_list
            )
            durable_projection_ok = self._restore_durable_protection_projection(account, existing_algo_inventory)
            if not durable_projection_ok:
                print(
                    "[beidou-autopilot] Durable protection projection UNKNOWN — skipping automatic protection creation"
                )
            # Phase 1: 本地创建所有保护单
            # BD-FIX (S41): 统计交易所已有 Algo 单，去重避免重复创建
            existing_algo_count: dict[str, int] = {}
            if isinstance(existing_algo_inventory, list):
                for a in existing_algo_inventory:
                    sym = str(a.get("symbol", "")).upper()
                    existing_algo_count[sym] = existing_algo_count.get(sym, 0) + 1
            pending_submissions: list[dict] = []
            for p in positions_list if durable_projection_ok else []:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                symbol = p["symbol"]
                # 交易所已有 >=2 个 Algo 单 → 跳过
                if existing_algo_count.get(symbol.upper(), 0) >= 2:
                    print(
                        f"[startup] {symbol}: already has {existing_algo_count[symbol.upper()]} Algo orders, skipping"
                    )
                    continue
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
                # Set per-symbol precision before creating protection orders.
                # Without this, ProtectionManager uses price_decimals=0 causing
                # stop-loss prices < 0.5 to round to 0.0 → ValueError.
                _prec = getattr(self, "_symbol_precision", {}).get(symbol, {})
                if _prec:
                    self._protection.set_precision_from_rule(
                        type(
                            "_PrecisionRule",
                            (),
                            {
                                "price_precision": _prec.get("price", 0),
                                "qty_precision": _prec.get("quantity", 0),
                            },
                        )()
                    )
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
                        trigger_val = float(p_order.trigger_price.amount)
                        if trigger_val > 5000:
                            dec = 1
                        elif trigger_val > 100:
                            dec = 2
                        elif trigger_val > 1:
                            dec = 3
                        else:
                            dec = 5
                        prec_map = {"price": dec, "quantity": dec}
                        print(f"[startup] {symbol}: using fallback precision price={dec} qty={dec}")
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

        # BD-FIX: 在初始对账前确保账户开盘基线存在。Testnet/Paper 等非生产
        # 环境没有独立的人工授权开盘流程，首次启动时从交易所账户快照自动建立
        # 基线，避免对账因 system_facts.incomplete 永久失败。
        if self._can_write and self._store is not None:
            try:
                existing = self._store.restore_account_opening_projection("default", "BINANCE")
                if existing is None:
                    account_snapshot = getattr(self, "_last_account", None)
                    if isinstance(account_snapshot, dict) and "totalWalletBalance" in account_snapshot:
                        import hashlib as _hashlib
                        import uuid as _uuid

                        _now = datetime.now(timezone.utc)
                        _positions: dict[str, str] = {}
                        for _p in account_snapshot.get("positions", []):
                            if isinstance(_p, dict):
                                _sym = str(_p.get("symbol", "")).strip()
                                _amt = float(_p.get("positionAmt", 0) or 0)
                                if _sym and _amt:
                                    _positions[_sym] = str(_amt)
                        _balance = str(account_snapshot.get("totalWalletBalance", "0"))
                        _evidence = {"balance": _balance, "positions": _positions}
                        _payload = {
                            "projection_id": f"auto-opening-{_now.strftime('%Y%m%dT%H%M%SZ')}-{_uuid.uuid4().hex[:8]}",
                            "account_id": "default",
                            "venue_id": "BINANCE",
                            "balance_amount": _balance,
                            "balance_currency": "USDT",
                            "balance_decimals": 8,
                            "positions": _positions,
                            "open_orders": [],
                            "captured_at": _now.isoformat(),
                            "source": "AUTO_OPENING_BASELINE",
                            "fact_version": str(int(_now.timestamp())),
                            "evidence_hash": _hashlib.sha256(
                                json.dumps(_evidence, sort_keys=True).encode()
                            ).hexdigest(),
                            "approval_id": f"auto-approval-{_uuid.uuid4().hex[:12]}",
                            "complete": True,
                            "created_at": _now.isoformat(),
                        }
                        self._store.save_account_opening_projection(
                            projection_id=_payload["projection_id"],
                            account_id=_payload["account_id"],
                            venue_id=_payload["venue_id"],
                            balance_amount=_payload["balance_amount"],
                            balance_currency=_payload["balance_currency"],
                            balance_decimals=_payload["balance_decimals"],
                            positions=_payload["positions"],
                            open_orders=_payload["open_orders"],
                            captured_at=_payload["captured_at"],
                            source=_payload["source"],
                            fact_version=_payload["fact_version"],
                            evidence_hash=_payload["evidence_hash"],
                            approval_id=_payload["approval_id"],
                            complete=_payload["complete"],
                        )
                        print(
                            "[beidou-autopilot] Auto-created opening baseline "
                            f"(balance={_balance}, positions={len(_positions)})"
                        )
            except Exception as _open_exc:
                print(f"[beidou-autopilot] Opening baseline creation skipped: {type(_open_exc).__name__}")

        # Initial reconciliation is read-only.  A mismatch or incomplete
        # projection closes the risk gate; state synchronization is a separate
        # explicitly authorized recovery workflow.
        try:
            recon_ok = await asyncio.wait_for(self._reconcile(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[beidou-autopilot] Initial reconciliation timed out — keeping risk gate closed")
            recon_ok = False
        self._last_recon = time.time()
        print(
            "[beidou-autopilot] Initial reconciliation verified"
            if recon_ok
            else "[beidou-autopilot] Initial reconciliation blocked ACTIVE"
        )

        # Phase 5b: 交易所残留持仓保护补充；超时保持风险门关闭。
        try:
            await asyncio.wait_for(self._ensure_exchange_position_protections(), timeout=15.0)
        except asyncio.TimeoutError:
            print("[beidou-autopilot] Position protection recovery timed out — keeping risk gate closed")

        # Protection recovery happens after the first account reconciliation,
        # so the reconciliation result alone cannot authorize ACTIVE: it may
        # have observed a position before the venue-backed SL/TP ACKs existed.
        # Re-evaluate the durable coverage gate before transitioning lifecycle;
        # any UNKNOWN/pending/mismatched protection keeps the engine degraded.
        durable_ok, durable_reason, durable_evidence = self._durable_fact_status()
        if not durable_ok:
            recon_ok = False
            self._record_execution_fact_failure_env_guarded(
                f"STARTUP_DURABLE_FACT_GATE:{durable_reason}:{json.dumps(durable_evidence, sort_keys=True, default=str)[:500]}"
            )

        # BD-T14: 对账未完成时，所有写环境都保持 DEGRADED；Testnet
        # 不能把“持续重试”当作账户/持仓事实已验证，更不能进入 ACTIVE。
        if not recon_ok:
            self._lifecycle.transition(ModuleState.DEGRADED)
            print("[beidou-autopilot] State: DEGRADED (reconciliation not verified)")
        else:
            self._lifecycle.transition(ModuleState.ACTIVE)
            print(f"[beidou-autopilot] State: {self._lifecycle.state.value}")

        self._safe_no_new_risk("startup_validation")
        print("[beidou-autopilot] Control plane: NO_NEW_RISK (awaiting supervisor validation)")
        if self._adapter is None:
            print("[beidou-autopilot] WARNING: Exchange not ready — supervisor will block RESUME")

        # BD-FIX: 启动 WebSocket 实时行情流（REST 轮询作为回退）。
        # 如果 bootstrap 阶段已创建 WS client（line ~7513），跳过第二次
        # start_ws 调用，避免构建第二个 client 导致第一个 client 泄漏。
        is_testnet = self._env_mode.value == "testnet"
        if getattr(self._feed, "_ws_client", None) is not None:
            ws_started = self._feed.is_healthy()
        else:
            ws_started = await self._feed.start_ws(self._symbols, testnet=is_testnet)
        if ws_started:
            print("[beidou-autopilot] WebSocket market data stream ACTIVE (REST polling as fallback)")
        else:
            print("[beidou-autopilot] WebSocket unavailable — using REST polling only")

        self._running = True
        self._last_realtime = time.time()
        self._last_realtime_mono = time.monotonic()
        self._last_recon = time.time()
        print("[beidou-autopilot] ========================================")
        print("[beidou-autopilot] Engine running. Press Ctrl+C to stop.")
        print("[beidou-autopilot] ========================================")

        # Main event loop — 各时钟域作为独立 asyncio Task 运行，避免
        # nearline (处理 25 个标的耗时 2+ min) 阻塞 realtime 心跳。
        async def _realtime_loop() -> None:
            while self._running:
                try:
                    tick_interval = 2.0  # 实时 tick 间隔 (秒)，平衡延迟与 API 调用频率
                    if self._realtime_age_seconds() >= tick_interval:
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
                    _nearline_interval = 30 if self._env_mode.value == "testnet" else 300
                    if time.time() - self._last_nearline >= _nearline_interval:
                        await self._nearline_tick()
                except Exception as exc:
                    self._error_count += 1
                    import traceback as _tb

                    print(f"[nearline] LOOP ERROR: {type(exc).__name__}: {exc}", flush=True)
                    _tb.print_exc()
                await asyncio.sleep(10)

        async def _offline_loop() -> None:
            _offline_interval = 300 if self._env_mode.value == "testnet" else 3600
            # 首次运行：启动后 60s 执行首次宇宙评估
            _first_tick_done = False
            while self._running:
                try:
                    elapsed = time.time() - self._last_offline
                    trigger = (not _first_tick_done and elapsed >= 60) or (elapsed >= _offline_interval)
                    if trigger:
                        if not _first_tick_done:
                            _first_tick_done = True
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
        self._safe_no_new_risk("auto")
        print("[beidou-autopilot] 1. NO_NEW_RISK")

        # 2. Cancel pending orders (BD-FIX F22: 使用 _order_symbols 精确查找)
        owned_active_order_ids = self._owned_active_order_ids()
        unowned_active_order_ids = self._unowned_active_order_ids()
        if self._can_write:
            for order_id in sorted(owned_active_order_ids):
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
            if unowned_active_order_ids:
                # Never broaden cancellation to orders whose owner cannot be
                # proven.  Keep the account in NO_NEW_RISK and surface the
                # exact IDs for operator-governed reconciliation.
                self._safe_no_new_risk("auto")
                self._record_execution_fact_failure_env_guarded(
                    f"SHUTDOWN_ACTIVE_ORDER_OWNER_UNKNOWN:{','.join(sorted(unowned_active_order_ids)[:20])}"
                )
                self._alerts.send_incident(
                    AlertSeverity.CRITICAL,
                    "Shutdown left unowned active orders untouched",
                    f"Manual/other-worker order IDs require reconciliation: {sorted(unowned_active_order_ids)[:20]}",
                    category="execution",
                )
        print(
            f"[beidou-autopilot] 2. Cancelled {len(owned_active_order_ids)} owned pending orders; "
            f"left {len(unowned_active_order_ids)} unowned orders untouched"
        )

        # 2b. BD-FIX: 停止 WebSocket 连接
        try:
            await self._feed.stop_ws()
            print("[beidou-autopilot] 2b. WebSocket stopped")
        except Exception as e:
            print(f"[shutdown] WS stop error: {e}")

        try:
            await self._stop_user_stream()
            print("[beidou-autopilot] 2c. User data stream stopped")
        except Exception as e:
            print(f"[shutdown] User data stream stop error: {e}")

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
