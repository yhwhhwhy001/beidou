"""FW-04: Paper Shadow 运行与 Testnet Gate。

Paper Shadow: 策略在 Paper 模式下与 Backtest 同步运行，
比较预测与实际模拟的差异。

Testnet Gate: 所有 P0 Gate 通过后才能启用 Testnet。
Mainnet 保持 PROHIBITED。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import GateResult, StrategyId


class ShadowMode(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"


class ShadowStatus(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ShadowConfig:
    """Paper Shadow 配置。"""

    strategy_id: StrategyId
    mode: ShadowMode = ShadowMode.PAPER
    min_runtime_hours: float = 168.0  # 7 days for Paper
    min_effective_events: int = 50
    max_prediction_deviation_pct: float = 30.0
    max_cost_deviation_pct: float = 20.0
    parity_check_enabled: bool = True


@dataclass
class ShadowMetrics:
    """Paper Shadow 运行指标。"""

    runtime_hours: float = 0.0
    total_ticks: int = 0
    total_signals: int = 0
    total_executed: int = 0
    total_rejected: int = 0
    prediction_vs_simulation_mae: float = 0.0  # 预测vs模拟平均绝对误差
    cost_estimated_vs_actual_mae: float = 0.0  # 预测成本vs实际成本误差
    drift_events: int = 0
    p0_incidents: int = 0


@dataclass
class ShadowReport:
    """Paper Shadow 完成报告。"""

    config: ShadowConfig
    metrics: ShadowMetrics = field(default_factory=ShadowMetrics)
    status: ShadowStatus = ShadowStatus.INITIALIZING
    gate_result: GateResult = GateResult.UNVERIFIABLE
    discrepancies: list[str] = field(default_factory=list)
    evidence_hash: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def is_ready_for_testnet(self) -> tuple[bool, str]:
        """检查是否可以进入 Testnet。"""
        cfg = self.config
        m = self.metrics

        failures = []
        if m.runtime_hours < cfg.min_runtime_hours:
            failures.append(f"runtime: {m.runtime_hours:.1f}h < {cfg.min_runtime_hours}h")
        if m.total_executed < cfg.min_effective_events:
            failures.append(f"events: {m.total_executed} < {cfg.min_effective_events}")
        if m.prediction_vs_simulation_mae > cfg.max_prediction_deviation_pct:
            failures.append(
                f"prediction_deviation: {m.prediction_vs_simulation_mae:.1f}% > {cfg.max_prediction_deviation_pct}%"
            )
        if m.p0_incidents > 0:
            failures.append(f"p0_incidents: {m.p0_incidents}")

        if failures:
            return False, "; ".join(failures)
        return True, "ready_for_testnet"


class PaperShadowRunner:
    """Paper Shadow 运行器。

    在 Paper 模式下运行策略，收集运行时指标并与
    Backtest 预测比较，验证一致性。
    """

    def __init__(self, config: ShadowConfig) -> None:
        self.config = config
        self.metrics = ShadowMetrics()
        self._start_time: float | None = None
        self._predictions: list[dict] = []
        self._actuals: list[dict] = []

    def start(self) -> None:
        self._start_time = time.time()

    def record_tick(
        self,
        predicted_direction: str,
        predicted_strength: float,
        actual_direction: str,
        actual_strength: float,
        estimated_cost_bps: float = 0.0,
        actual_cost_bps: float = 0.0,
    ) -> None:
        """记录单次 tick 的预测与实际比较。"""
        self.metrics.total_ticks += 1

        if predicted_direction != "NO_ACTION":
            self.metrics.total_signals += 1

            # 记录预测
            self._predictions.append(
                {
                    "direction": predicted_direction,
                    "strength": predicted_strength,
                    "cost_bps": estimated_cost_bps,
                    "tick": self.metrics.total_ticks,
                }
            )

            # 与实际比较
            if actual_direction != "NO_ACTION":
                self.metrics.total_executed += 1
                self._actuals.append(
                    {
                        "direction": actual_direction,
                        "strength": actual_strength,
                        "cost_bps": actual_cost_bps,
                        "tick": self.metrics.total_ticks,
                    }
                )
            else:
                self.metrics.total_rejected += 1

            # 计算偏差
            if predicted_direction == actual_direction:
                strength_err = abs(predicted_strength - actual_strength)
            else:
                strength_err = abs(predicted_strength + actual_strength)

            # 更新 MAE (指数移动平均)
            n = self.metrics.total_signals
            self.metrics.prediction_vs_simulation_mae = (
                self.metrics.prediction_vs_simulation_mae * (n - 1) + strength_err * 100
            ) / n

            # 成本偏差
            if estimated_cost_bps > 0 and actual_cost_bps > 0:
                cost_err = abs(estimated_cost_bps - actual_cost_bps) / estimated_cost_bps * 100
                self.metrics.cost_estimated_vs_actual_mae = (
                    self.metrics.cost_estimated_vs_actual_mae * (n - 1) + cost_err
                ) / n

    def record_incident(self, level: str) -> None:
        """记录事件。"""
        if level == "P0":
            self.metrics.p0_incidents += 1

    def record_drift(self) -> None:
        self.metrics.drift_events += 1

    def generate_report(self) -> ShadowReport:
        """生成 Paper Shadow 报告。"""
        if self._start_time is not None:
            self.metrics.runtime_hours = (time.time() - self._start_time) / 3600.0

        report = ShadowReport(
            config=self.config,
            metrics=self.metrics,
            started_at=datetime.fromtimestamp(self._start_time, tz=timezone.utc) if self._start_time else None,
            completed_at=datetime.now(timezone.utc),
        )

        # Gate 判定
        ready, reason = report.is_ready_for_testnet()
        if ready:
            report.gate_result = GateResult.PASS
            report.status = ShadowStatus.COMPLETED
        else:
            if self.metrics.runtime_hours > 0:
                report.gate_result = GateResult.UNVERIFIABLE
                report.status = ShadowStatus.RUNNING
            else:
                report.gate_result = GateResult.UNVERIFIABLE
                report.status = ShadowStatus.INITIALIZING

        # 证据哈希
        content = json.dumps(
            {
                "strategy_id": str(self.config.strategy_id),
                "runtime_hours": self.metrics.runtime_hours,
                "total_executed": self.metrics.total_executed,
                "deviation_pct": self.metrics.prediction_vs_simulation_mae,
                "p0_incidents": self.metrics.p0_incidents,
            },
            sort_keys=True,
        )
        report.evidence_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        if not ready:
            report.discrepancies.append(reason)

        return report


class TestnetGate:
    """Testnet 准入门禁。

    所有 P0 Gate 必须通过才能启用 Testnet。
    Mainnet 始终保持 PROHIBITED。
    """

    @staticmethod
    def check(
        shadow_report: ShadowReport,
        *,
        parity_passed: bool = False,
        all_p0_gates_passed: bool = False,
        mainnet_prohibited: bool = True,
    ) -> tuple[bool, list[str]]:
        """检查是否可以进入 Testnet。

        Returns:
            (allowed, reasons)
        """
        failures = []

        if not mainnet_prohibited:
            failures.append("MAINNET_MUST_BE_PROHIBITED")

        if not shadow_report.is_ready_for_testnet()[0]:
            failures.append("shadow_not_ready")

        if not parity_passed:
            failures.append("parity_not_verified")

        if not all_p0_gates_passed:
            failures.append("p0_gates_not_all_passed")

        if shadow_report.metrics.p0_incidents > 0:
            failures.append(f"p0_incidents: {shadow_report.metrics.p0_incidents}")

        return len(failures) == 0, failures


# ================================================================
# BD-T16: Paper 撮合引擎 — 不再直接 ACKED/FILLED
# ================================================================


class PaperOrderStatus(str, Enum):
    """Paper 订单状态 — 不直接 FILLED。"""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class PaperMatchingEngine:
    """BD-T16: Paper 撮合引擎。

    特性:
    - bid/ask 价差撮合（不做无成本直接 FILLED）
    - 部分成交、拒绝、取消竞争
    - 延迟、队列位置模拟
    - 确定性 replay（基于 seed）
    - 手续费/成本进入账本
    """

    def __init__(self, seed: int = 42):
        import random as _random

        self._rng = _random.Random(seed)
        self._base_latency_ms: float = 50.0
        self._fill_probability: float = 0.85
        self._partial_fill_probability: float = 0.10

    def match(
        self, symbol: str, side: str, quantity: float, limit_price: float | None, bid: float, ask: float
    ) -> tuple[str, float, float, float]:
        """撮合订单。

        Returns:
            (status, filled_qty, avg_price, latency_ms)
        """
        latency = self._base_latency_ms * (0.5 + self._rng.random())
        is_buy = side.upper() == "BUY"

        # 限价单撮合：限价不满足时入队
        if limit_price is not None:
            if is_buy and limit_price < ask:
                return (PaperOrderStatus.QUEUED.value, 0.0, 0.0, latency)
            if not is_buy and limit_price > bid:
                return (PaperOrderStatus.QUEUED.value, 0.0, 0.0, latency)

        # Spread slippage
        spread = ask - bid
        slippage = spread * 0.5 * self._rng.random()
        exec_price = ask + slippage if is_buy else bid - slippage

        roll = self._rng.random()
        if roll > self._fill_probability + self._partial_fill_probability:
            return (PaperOrderStatus.REJECTED.value, 0.0, 0.0, latency)

        if self._partial_fill_probability < roll <= self._fill_probability + self._partial_fill_probability:
            fill_pct = 0.3 + self._rng.random() * 0.5
            return (PaperOrderStatus.PARTIALLY_FILLED.value, quantity * fill_pct, exec_price, latency)

        return (PaperOrderStatus.FILLED.value, quantity, exec_price, latency)

    def compute_hash(self, orders: list[dict], session_id: str, model_version: str, seed: int) -> str:
        content = json.dumps(
            {"session_id": session_id, "orders": orders, "model_version": model_version, "seed": seed},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class CostPressureSimulator:
    """BD-T16: 成本压力模拟器。

    模拟压力下的 spread 扩大、滑点增加和容量下降。
    压力增加时净收益降低而非提高。
    """

    def __init__(self):
        self._pressure_level: float = 0.0

    def set_pressure(self, level: float) -> None:
        self._pressure_level = max(0.0, min(1.0, level))

    def spread_widening(self, base_bps: float) -> float:
        return base_bps * (1.0 + self._pressure_level * 2.0)

    def slippage_multiplier(self) -> float:
        return 1.0 + self._pressure_level * 3.0

    def capacity_multiplier(self) -> float:
        return max(0.1, 1.0 - self._pressure_level * 0.8)

    def is_net_positive(self, gross_return_bps: float, total_cost_bps: float) -> bool:
        """BD-T16 AC-04: 成本压力下验证净收益不增反降。"""
        return gross_return_bps > total_cost_bps * (1.0 + self._pressure_level)
