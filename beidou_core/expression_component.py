"""通用表达式组件：挖掘因子运行时解释器。

表达式由 PrimitiveRegistry.parse 解析并缓存；组件维护自身环形价格历史，
构建与离线 runner 同构的 feature_dict 后 evaluate_series 求值。
信号方向由因子值滚动 z-score 决定（|z|<0.5 → NO_ACTION）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
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
_Z_ACTION_THRESHOLD = 0.5


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
        self._history: deque[float] = deque(maxlen=_HISTORY_LIMIT)  # close
        self._high_history: deque[float] = deque(maxlen=_HISTORY_LIMIT)
        self._low_history: deque[float] = deque(maxlen=_HISTORY_LIMIT)
        self._volume_history: deque[float] = deque(maxlen=_HISTORY_LIMIT)
        self._value_history: deque[float] = deque(maxlen=_Z_WINDOW)
        if expression_string:
            try:
                from beidou_research.mining.primitive_library import PrimitiveRegistry

                self._expr = PrimitiveRegistry().parse(expression_string)
            except Exception:
                self._expr = None

    @classmethod
    def bind(cls, factor_id: str, expression_string: str, role: str = "ENTRY") -> None:
        """注册类级表达式绑定，供无参构造按 factor_id 兜底。"""
        EXPRESSION_BINDINGS[factor_id] = (expression_string, role)

    def validate(self) -> bool:
        return bool(self._factor_id) and self._expr is not None

    async def generate(self, context: dict[str, Any]) -> AlphaSignal:
        features = context.get("features", {}) or {}
        try:
            close = float(features["close"])
            high = float(features.get("high", close))
            low = float(features.get("low", close))
            volume = float(features.get("volume", 0.0))
        except (KeyError, TypeError, ValueError):
            return self._no_action(context)
        self._history.append(close)
        self._high_history.append(high)
        self._low_history.append(low)
        self._volume_history.append(volume)

        if self._expr is None or len(self._history) < _MIN_BARS:
            return self._no_action(context)
        try:
            feature_dict = self._build_feature_dict()
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
        self._value_history.append(last)
        if len(self._value_history) < _Z_MIN:
            return self._no_action(context)
        vals = list(self._value_history)
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

    def _build_feature_dict(self) -> dict[str, list[float]]:
        """构建与 runner._build_feature_dict 同构的特征列（close/log_return/volume/spread）。"""
        closes = list(self._history)
        n = len(closes)
        log_return = [math.nan] * n
        for i in range(1, n):
            if closes[i] > 0 and closes[i - 1] > 0:
                log_return[i] = math.log(closes[i] / closes[i - 1])
        highs = list(self._high_history)
        lows = list(self._low_history)
        spread = [math.nan] * n
        for i in range(n):
            if closes[i] > 0:
                spread[i] = (highs[i] - lows[i]) / closes[i]
        return {
            "close": closes,
            "log_return": log_return,
            "volume": list(self._volume_history),
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
