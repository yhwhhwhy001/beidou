"""BD-CV23: Typed Strategy Kernel — research/backtest/live 统一内核。

同一 market snapshot 产生相同 StrategyDecision hash。
Filter 不能生成 LONG/SHORT；Entry 决定方向。
策略图输出绑定 feature/factor/regime/policy/version hashes。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class StrategyAction(str, Enum):
    ACTION = "ACTION"
    NO_ACTION = "NO_ACTION"
    VETO = "VETO"
    DEGRADED = "DEGRADED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class FilterResult(str, Enum):
    PASS = "PASS"  # noqa: S105 - filter decision, not a credential
    VETO = "VETO"
    DEGRADE = "DEGRADE"


@dataclass(frozen=True)
class KernelInput:
    """BD-CV23: 内核输入 — 所有环境使用相同结构。"""

    symbol: str
    market_data: dict[str, float] = field(default_factory=dict)  # price/volume/spread
    features: dict[str, float] = field(default_factory=dict)  # RSI/SMA/etc
    regime: str = ""  # BULL/BEAR/RANGE
    current_position: float = 0.0
    portfolio_context: dict[str, float] = field(default_factory=dict)
    feature_hash: str = ""
    factor_hash: str = ""
    regime_hash: str = ""
    policy_hash: str = ""
    version_hash: str = ""

    def compute_input_hash(self) -> str:
        data = {
            "symbol": self.symbol,
            "market": self.market_data,
            "features": self.features,
            "regime": self.regime,
            "position": self.current_position,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class KernelOutput:
    """BD-CV23: 内核输出 — 确定性 hash。"""

    symbol: str
    action: StrategyAction
    direction: str = ""  # LONG/SHORT/FLAT
    confidence: float = 0.0
    target_exposure: float = 0.0
    reason: str = ""
    bound_hashes: dict[str, str] = field(default_factory=dict)
    output_hash: str = ""

    def compute_output_hash(self) -> str:
        data = {
            "symbol": self.symbol,
            "action": self.action.value,
            "direction": self.direction,
            "confidence": self.confidence,
            "target_exposure": self.target_exposure,
            "bound_hashes": self.bound_hashes,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


class TypedStrategyKernel:
    """BD-CV23 AC-23-03: research/backtest/live 使用同一个 Typed Strategy Kernel。

    同输入快照 → 同输出 hash。
    """

    def __init__(self) -> None:
        self._entry_rules: list[Any] = []
        self._filter_rules: list[Any] = []
        self._exit_rules: list[Any] = []

    def register_entry(self, rule_fn: Any) -> None:
        self._entry_rules.append(rule_fn)

    def register_filter(self, rule_fn: Any) -> None:
        self._filter_rules.append(rule_fn)

    def register_exit(self, rule_fn: Any) -> None:
        self._exit_rules.append(rule_fn)

    def execute(self, inputs: KernelInput) -> KernelOutput:
        """BD-CV23: 执行策略内核。

        1. Entry 决定方向 (LONG/SHORT)
        2. Filter 只能 PASS/VETO/DEGRADE（不能生成方向）
        3. Exit 不创建新增风险
        4. 输出绑定所有 hash
        """
        input_hash = inputs.compute_input_hash()
        direction = ""
        confidence = 0.0
        target_exposure = 0.0
        reason = "NO_ACTION"

        # Entry phase: determine direction
        for rule in self._entry_rules:
            try:
                result = rule(inputs)
                if result and hasattr(result, "direction"):
                    direction = result.direction
                    confidence = getattr(result, "confidence", 0.5)
                    target_exposure = getattr(result, "target_exposure", 0.0)
                    reason = f"ENTRY:{getattr(result, 'reason', 'signal')}"
                    break
            except Exception as exc:
                logger.error("Entry rule failed for %s: %s", inputs.symbol, type(exc).__name__)
                return KernelOutput(
                    symbol=inputs.symbol,
                    action=StrategyAction.NOT_VERIFIABLE,
                    reason=f"ENTRY_RULE_ERROR:{type(exc).__name__}",
                    output_hash="",
                )

        if not direction:
            return KernelOutput(
                symbol=inputs.symbol,
                action=StrategyAction.NO_ACTION,
                reason="NO_ENTRY_SIGNAL",
                output_hash="",
            )

        # Filter phase: PASS/VETO/DEGRADE only
        current_action = StrategyAction.ACTION
        for rule in self._filter_rules:
            try:
                filter_out = rule(inputs, direction)
                if filter_out == FilterResult.VETO:
                    return KernelOutput(
                        symbol=inputs.symbol,
                        action=StrategyAction.VETO,
                        direction=direction,
                        reason="FILTER_VETO",
                        output_hash="",
                    )
                if filter_out == FilterResult.DEGRADE:
                    confidence *= 0.5
                    target_exposure *= 0.5
                    current_action = StrategyAction.DEGRADED
                    reason = "FILTER_DEGRADE"
            except Exception as exc:
                logger.error("Filter rule failed for %s: %s", inputs.symbol, type(exc).__name__)
                return KernelOutput(
                    symbol=inputs.symbol,
                    action=StrategyAction.NOT_VERIFIABLE,
                    direction=direction,
                    reason=f"FILTER_RULE_ERROR:{type(exc).__name__}",
                    output_hash="",
                )

        # Exit phase: check if we should exit
        for rule in self._exit_rules:
            try:
                if rule(inputs):
                    return KernelOutput(
                        symbol=inputs.symbol,
                        action=StrategyAction.ACTION,
                        direction="FLAT",
                        confidence=1.0,
                        target_exposure=0.0,
                        reason="EXIT_SIGNAL",
                        output_hash="",
                    )
            except Exception as exc:
                logger.error("Exit rule failed for %s: %s", inputs.symbol, type(exc).__name__)
                return KernelOutput(
                    symbol=inputs.symbol,
                    action=StrategyAction.NOT_VERIFIABLE,
                    direction=direction,
                    reason=f"EXIT_RULE_ERROR:{type(exc).__name__}",
                    output_hash="",
                )

        output = KernelOutput(
            symbol=inputs.symbol,
            action=current_action,
            direction=direction,
            confidence=confidence,
            target_exposure=target_exposure,
            reason=reason,
            bound_hashes={
                "input_hash": input_hash,
                "feature_hash": inputs.feature_hash,
                "factor_hash": inputs.factor_hash,
                "regime_hash": inputs.regime_hash,
                "policy_hash": inputs.policy_hash,
                "version_hash": inputs.version_hash,
            },
        )
        object.__setattr__(output, "output_hash", output.compute_output_hash())
        return output


# 全局单例 — 所有环境共享
_shared_kernel: TypedStrategyKernel | None = None


def get_typed_strategy_kernel() -> TypedStrategyKernel:
    """BD-CV23: 获取共享策略内核。research/backtest/live 统一调用。"""
    global _shared_kernel
    if _shared_kernel is None:
        _shared_kernel = TypedStrategyKernel()
    return _shared_kernel


def reset_kernel() -> None:
    global _shared_kernel
    _shared_kernel = None
