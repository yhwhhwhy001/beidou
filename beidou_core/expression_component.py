"""通用表达式组件：挖掘因子运行时解释器。

表达式由 PrimitiveRegistry.parse 解析并缓存；组件维护自身环形价格历史，
构建与离线 runner 同构的 feature_dict 后 evaluate_series 求值。
信号方向由因子值滚动 z-score 决定（|z|<0.5 → NO_ACTION）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
from collections import deque
from typing import TYPE_CHECKING, Any

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import (
    AlphaComponent,
    AlphaComponentType,
    AlphaSignal,
    SignalDirection,
)

if TYPE_CHECKING:
    from beidou_research.mining.expression_ast import Expression

EXPRESSION_BINDINGS: dict[str, tuple[str, str]] = {}  # factor_id -> (expression_string, role)

# 表达式求值专用线程池：与 rest_client 的 asyncio.to_thread 默认线程池隔离。
# 默认池 max_workers≈12，nearline 每 tick 37 组件求值批次会占满默认池数秒，
# 导致行情/对账/池评分的网络请求排队、循环周期恶化到 90s+；独立小池保证
# 求值（4 线程）与网络 IO 互不挤占。
_EVAL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="expr-eval",
)

_HISTORY_LIMIT = 800  # 历史窗口上限：挖掘因子最大滚动窗口 100、z 窗口 100，800 根余量充足；
# 相比 3000 根，逐 bar 求值量 ÷3.75，缓解同步 evaluate_series 阻塞共享 event loop（P0 STALL）
_MIN_BARS = 30
_Z_WINDOW = 100
_Z_MIN = 30
# BD-FIX: 信号阈值环境可调（默认 0.5 生产语义不变）。demo 验证期
# RANGING 市场 + z 窗口 100 根预热（1m bar 约 100 分钟）使信号触发
# 极慢 —— BEIDOU_Z_ACTION_THRESHOLD 允许 testnet 调低加速端到端验证。
_Z_ACTION_THRESHOLD = float(os.getenv("BEIDOU_Z_ACTION_THRESHOLD", "0.5"))


class ExpressionComponent(AlphaComponent):
    """把挖掘表达式实时解释为 AlphaSignal 的通用组件。

    构造签名全部 keyword-only 且可选，便于在 Alpha DAG 中以
    functools.partial 无参实例化；未绑定时按 factor_id 查类级
    EXPRESSION_BINDINGS 兜底。任何求值异常或数据不足一律返回
    NO_ACTION（confidence 0），不向上抛异常。
    """

    def __init__(
        self,
        *,
        factor_id: str = "",
        expression_string: str = "",
        role: str = "ENTRY",
        strategy_id: str = "autopilot",
    ) -> None:
        if not factor_id and expression_string:
            raise ValueError("ExpressionComponent requires factor_id when expression_string is bound")
        if not expression_string and factor_id:
            binding = EXPRESSION_BINDINGS.get(factor_id)
            if binding is not None:
                expression_string, role = binding
        component_type = {
            "ENTRY": AlphaComponentType.ENTRY,
            "FILTER": AlphaComponentType.FILTER,
            "EXIT": AlphaComponentType.EXIT,
        }.get((role or "ENTRY").upper(), AlphaComponentType.ENTRY)
        super().__init__(
            component_type=component_type,
            component_id=factor_id or "expression_component",
            version=SchemaVersion("2.0.0"),
        )
        self._factor_id = factor_id
        self._expression_string = expression_string
        self._strategy_id = strategy_id
        self._expr: Expression | None = None
        # BD-FIX: 历史按 timeframe 隔离 —— 单例组件被 1m/5m/1h/1d 四个
        # 近线循环交错喂收盘价，混合粒度序列上的 z-score 完全失真
        # （信号触发慢/不稳定的根因）。每 tf 独立历史与 z 窗口。
        self._histories: dict[str, dict[str, deque[float]]] = {}
        # BD-FIX (final83i): 重启回填 —— 引擎注入异步 K 线源,首轮求值前
        # 用闭合历史 bar 重建价格/因子值历史,不再实时裸等约 60 分钟预热
        # (_MIN_BARS + _Z_MIN 逐 bar 积累)。每 (symbol, timeframe) 至多
        # 尝试一次,失败继续实时积累且不回退重试。
        self._backfill_source: Any = None
        self._backfill_attempted: set[tuple[str, str]] = set()
        try:
            self._backfill_lookback: int = int(os.getenv("BEIDOU_FACTOR_BACKFILL_BARS", "400"))
        except (TypeError, ValueError):
            self._backfill_lookback = 400
        if expression_string:
            try:
                from beidou_research.mining.primitive_library import PrimitiveRegistry

                self._expr = PrimitiveRegistry().parse(expression_string)
            except Exception:
                self._expr = None

    def _hist_for(self, timeframe: str) -> dict[str, deque[float]]:
        hist = self._histories.get(timeframe)
        if hist is None:
            hist = {
                "close": deque(maxlen=_HISTORY_LIMIT),
                "high": deque(maxlen=_HISTORY_LIMIT),
                "low": deque(maxlen=_HISTORY_LIMIT),
                "volume": deque(maxlen=_HISTORY_LIMIT),
                "value": deque(maxlen=_Z_WINDOW),
            }
            self._histories[timeframe] = hist
        return hist

    @classmethod
    def bind(cls, factor_id: str, expression_string: str, role: str = "ENTRY") -> None:
        """注册类级表达式绑定，供无参构造按 factor_id 兜底。"""
        EXPRESSION_BINDINGS[factor_id] = (expression_string, role)

    def set_backfill_source(self, source: Any) -> None:
        """注入异步 K 线回填源：``async (symbol, timeframe) -> list[dict]``。

        每行含 close/high/low/volume（open_time 升序、仅闭合 bar）。引擎在
        Alpha DAG 组装后为每个挖掘因子组件注入（final83i）。
        """
        self._backfill_source = source

    def validate(self) -> bool:
        return bool(self._factor_id) and self._expr is not None

    async def _maybe_backfill(self, symbol: str, timeframe: str, hist: dict[str, deque[float]]) -> bool:
        """首轮求值前用闭合历史 K 线重建价格/因子值历史（幂等）。

        返回 True 表示本轮完成了回填（调用方可对与回填尾部重合的当前
        闭合 bar 去重一次）；False 表示未回填（正常实时积累）。
        """
        _key = (symbol, timeframe)
        if self._backfill_source is None or _key in self._backfill_attempted:
            return False
        self._backfill_attempted.add(_key)
        try:
            bars = await self._backfill_source(symbol, timeframe, self._backfill_lookback)
            rows = [b for b in bars if isinstance(b, dict)] if isinstance(bars, (list, tuple)) else []
            if len(rows) < _MIN_BARS:
                return False
            hist["close"].clear()
            hist["high"].clear()
            hist["low"].clear()
            hist["volume"].clear()
            for row in rows:
                try:
                    _close = float(row["close"])
                    _high = float(row.get("high", _close))
                    _low = float(row.get("low", _close))
                    _volume = float(row.get("volume", 0) or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if not all(math.isfinite(v) for v in (_close, _high, _low, _volume)):
                    continue
                hist["close"].append(_close)
                hist["high"].append(_high)
                hist["low"].append(_low)
                hist["volume"].append(max(_volume, 0.0))
            if self._expr is None or len(hist["close"]) < _MIN_BARS:
                return False
            # 与离线 runner 同构地对全序列求值,重建因子值历史(z 窗口)
            feature_dict = self._build_feature_dict(hist)
            loop = asyncio.get_running_loop()
            values = await loop.run_in_executor(_EVAL_EXECUTOR, self._expr.evaluate_series, feature_dict)
            hist["value"].clear()
            for raw_value in values[-_Z_WINDOW:]:
                try:
                    parsed = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    hist["value"].append(parsed)
            return True
        except Exception:
            # 回填失败(API 抖动/数据不足):继续实时积累;本 (symbol,tf)
            # 不再重试,避免每 tick 打 API。
            return False

    async def generate(self, context: dict[str, Any]) -> AlphaSignal:
        features = context.get("features", {}) or {}
        _timeframe = str(context.get("timeframe", "1m"))
        _hist = self._hist_for(_timeframe)
        # BD-FIX (final83i): 首轮求值前回填闭合历史(重启免 60 分钟预热)
        _seeded_now = False
        if len(_hist["close"]) < _MIN_BARS and self._backfill_source is not None:
            _seeded_now = await self._maybe_backfill(str(context.get("instrument_id", "UNKNOWN")), _timeframe, _hist)
        try:
            close = float(features["close"])
            high = float(features.get("high", close))
            low = float(features.get("low", close))
            volume = float(features.get("volume", 0.0))
        except (KeyError, TypeError, ValueError):
            return self._no_action(context)
        # 回填当轮若当前闭合 bar 与回填尾部重合则去重一次(同一根 bar 不双计);
        # 其余轮次保持原始终追加语义。
        _dup_of_seed = bool(_seeded_now and _hist["close"] and _hist["close"][-1] == close)
        if not _dup_of_seed:
            _hist["close"].append(close)
            _hist["high"].append(high)
            _hist["low"].append(low)
            _hist["volume"].append(volume)

        if self._expr is None or len(_hist["close"]) < _MIN_BARS:
            return self._no_action(context)
        try:
            feature_dict = self._build_feature_dict(_hist)
            # 同步 CPU 密集求值（37 组件 × 逐 bar 全历史）移入专用求值线程池，
            # 避免阻塞引擎与 supervisor 共享的 asyncio event loop，且与
            # rest_client 的网络线程（asyncio.to_thread 默认池）隔离；
            # 异常在 await 处抛出，由下方 except Exception 捕获走 NO_ACTION。
            loop = asyncio.get_running_loop()
            values = await loop.run_in_executor(_EVAL_EXECUTOR, self._expr.evaluate_series, feature_dict)
            last = float(values[-1]) if values else math.nan
        except Exception:
            return self._no_action(context)
        if not math.isfinite(last):
            return self._no_action(context)
        _hist["value"].append(last)
        if len(_hist["value"]) < _Z_MIN:
            return self._no_action(context)
        vals = list(_hist["value"])
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        std = math.sqrt(max(var, 0.0))
        z = (last - mean) / std if std > 1e-12 else 0.0
        if abs(z) < _Z_ACTION_THRESHOLD:
            return self._no_action(context)
        direction = SignalDirection.LONG if z > 0 else SignalDirection.SHORT
        strength = min(1.0, abs(z) / 2.0)
        return AlphaSignal(
            strategy_id=StrategyId(self._strategy_id),
            component_type=self.component_type,
            direction=direction,
            strength=strength,
            confidence=min(0.9, strength),
            instrument_id=InstrumentId(str(context.get("instrument_id", "UNKNOWN"))),
            venue_id=VenueId(str(context.get("venue_id", "BINANCE"))),
            model_version=SchemaVersion("2.0.0"),
            metadata={"factor_id": self._factor_id, "expression": self._expression_string, "z": round(z, 4)},
        )

    def _build_feature_dict(self, hist: dict[str, deque[float]]) -> dict[str, list[float]]:
        """构建与 runner._build_feature_dict 同构的特征列（close/log_return/volume/spread）。"""
        closes = list(hist["close"])
        n = len(closes)
        log_return = [math.nan] * n
        for i in range(1, n):
            if closes[i] > 0 and closes[i - 1] > 0:
                log_return[i] = math.log(closes[i] / closes[i - 1])
        highs = list(hist["high"])
        lows = list(hist["low"])
        spread = [math.nan] * n
        for i in range(n):
            if closes[i] > 0:
                spread[i] = (highs[i] - lows[i]) / closes[i]
        return {
            "close": closes,
            "log_return": log_return,
            "volume": list(hist["volume"]),
            "spread": spread,
        }

    def _no_action(self, context: dict[str, Any]) -> AlphaSignal:
        return AlphaSignal(
            strategy_id=StrategyId(self._strategy_id),
            component_type=self.component_type,
            direction=SignalDirection.NO_ACTION,
            strength=0.0,
            confidence=0.0,
            instrument_id=InstrumentId(str(context.get("instrument_id", "UNKNOWN"))),
            venue_id=VenueId(str(context.get("venue_id", "BINANCE"))),
            model_version=SchemaVersion("2.0.0"),
        )
