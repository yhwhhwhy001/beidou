"""FW-04: Paper Shadow 运行与 Testnet Gate。

Paper Shadow: 策略在 Paper 模式下与 Backtest 同步运行，
比较预测与实际模拟的差异。

Testnet Gate: 所有 P0 Gate 通过后才能启用 Testnet。
Mainnet 保持 PROHIBITED。
"""

from __future__ import annotations

import hashlib
import json
import math
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
    INVALID = "INVALID"  # PKG28 (BDS-P1-060): 证据写失败导致运行无效


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
    # PKG28 (BDS-P1-061): 指标拆分
    total_executed: int = 0  # backward-compat: 等同于 total_predictions_with_outcome
    total_predictions_with_outcome: int = 0  # 有前向标签的预测数
    total_simulated_fills: int = 0  # 真实模拟成交数
    total_venue_acks: int = 0  # 交易所确认数（Paper 下为0）
    total_rejected: int = 0
    prediction_vs_simulation_mae: float = 0.0  # 预测vs模拟平均绝对误差
    cost_estimated_vs_actual_mae: float = 0.0  # 预测成本vs实际成本误差
    cost_observations: int = 0
    drift_events: int = 0
    p0_incidents: int = 0
    ledger_write_failures: int = 0  # PKG28 (BDS-P1-060): 账本写失败计数


@dataclass
class ShadowReport:
    """Paper Shadow 完成报告。"""

    config: ShadowConfig
    metrics: ShadowMetrics = field(default_factory=ShadowMetrics)
    status: ShadowStatus = ShadowStatus.INITIALIZING
    gate_result: GateResult = GateResult.UNVERIFIABLE
    discrepancies: list[str] = field(default_factory=list)
    evidence_hash: str = ""
    merkle_manifest: dict = field(default_factory=dict)  # PKG28 (BDS-P1-062)
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
        if m.cost_observations == 0:
            failures.append("cost_evidence_missing")
        elif m.cost_estimated_vs_actual_mae > cfg.max_cost_deviation_pct:
            failures.append(f"cost_deviation: {m.cost_estimated_vs_actual_mae:.1f}% > {cfg.max_cost_deviation_pct}%")
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
        actual_direction: str | None = None,
        actual_strength: float | None = None,
        estimated_cost_bps: float = 0.0,
        actual_cost_bps: float = 0.0,
        *,
        decision_timestamp: float | None = None,
        outcome_source: str | None = None,
        outcome_available_at: float | None = None,
    ) -> None:
        """Record a prediction and optional independently available outcome.

        Direction error and ``total_executed`` are outcome metrics, not fill
        metrics.  If an outcome is supplied it must carry a non-empty source
        and a timestamp strictly after the decision; otherwise the call is
        rejected instead of allowing a same-tick self-label.
        """
        self.metrics.total_ticks += 1
        decision_at = time.time() if decision_timestamp is None else float(decision_timestamp)
        if not math.isfinite(decision_at):
            raise ValueError("decision_timestamp must be finite")
        if actual_direction is not None or actual_strength is not None:
            if actual_direction is None or actual_strength is None:
                raise ValueError("INDEPENDENT_FORWARD_LABEL_REQUIRED")
            if not str(outcome_source or "").strip():
                raise ValueError("INDEPENDENT_FORWARD_LABEL_SOURCE_REQUIRED")
            try:
                available_at = float(outcome_available_at)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("forward outcome timestamp/strength is invalid") from exc
            if not math.isfinite(available_at) or available_at <= decision_at:
                raise ValueError("forward outcome must be available after the decision")

        if predicted_direction != "NO_ACTION":
            self.metrics.total_signals += 1

            # 记录预测
            self._predictions.append(
                {
                    "direction": predicted_direction,
                    "strength": predicted_strength,
                    "cost_bps": estimated_cost_bps,
                    "tick": self.metrics.total_ticks,
                    "decision_timestamp": decision_at,
                    "outcome_recorded": False,
                }
            )

            if actual_direction is not None or actual_strength is not None:
                self.record_forward_outcome(
                    tick=self.metrics.total_ticks,
                    actual_direction=actual_direction,
                    actual_strength=actual_strength,
                    outcome_source=outcome_source,
                    outcome_available_at=outcome_available_at,
                )

        # A fill/cost observation is independent of a future directional label.
        self._record_cost_observation(estimated_cost_bps, actual_cost_bps)

    def record_forward_outcome(
        self,
        *,
        tick: int,
        actual_direction: str | None,
        actual_strength: float | None,
        outcome_source: str | None,
        outcome_available_at: float | None,
    ) -> None:
        """Attach one independently observed future-window label to a tick."""

        if not isinstance(tick, int) or tick <= 0:
            raise ValueError("forward outcome tick must be a positive integer")
        if actual_direction is None or actual_strength is None:
            raise ValueError("INDEPENDENT_FORWARD_LABEL_REQUIRED")
        source = str(outcome_source or "").strip()
        if not source:
            raise ValueError("INDEPENDENT_FORWARD_LABEL_SOURCE_REQUIRED")
        try:
            available_at = float(outcome_available_at)
            strength = float(actual_strength)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("forward outcome timestamp/strength is invalid") from exc
        if not math.isfinite(available_at) or not math.isfinite(strength):
            raise ValueError("forward outcome timestamp/strength must be finite")

        prediction = next((item for item in self._predictions if item.get("tick") == tick), None)
        if prediction is None:
            raise ValueError(f"unknown prediction tick: {tick}")
        if prediction.get("outcome_recorded"):
            raise ValueError(f"forward outcome already recorded for tick: {tick}")
        decision_at = float(prediction["decision_timestamp"])
        if available_at <= decision_at:
            raise ValueError("forward outcome must be available after the decision")

        direction = str(actual_direction).strip().upper()
        if not direction:
            raise ValueError("forward outcome direction is required")
        prediction["outcome_recorded"] = True
        prediction["outcome_source"] = source
        prediction["outcome_available_at"] = available_at
        if direction != "NO_ACTION":
            self.metrics.total_executed += 1  # backward-compat
            self.metrics.total_predictions_with_outcome += 1  # PKG28 (BDS-P1-061)
            self._actuals.append(
                {
                    "direction": direction,
                    "strength": strength,
                    "tick": tick,
                    "outcome_source": source,
                    "outcome_available_at": available_at,
                }
            )
        else:
            self.metrics.total_rejected += 1

        predicted_direction = str(prediction["direction"])
        predicted_strength = float(prediction["strength"])
        strength_err = (
            abs(predicted_strength - strength)
            if predicted_direction == direction
            else abs(predicted_strength + strength)
        )
        labeled_count = sum(1 for item in self._predictions if item.get("outcome_recorded"))
        self.metrics.prediction_vs_simulation_mae = (
            self.metrics.prediction_vs_simulation_mae * (labeled_count - 1) + strength_err * 100
        ) / labeled_count

    def record_execution_observation(self, estimated_cost_bps: float, actual_cost_bps: float) -> None:
        """Record a fill/cost fact without inventing a future signal label.

        The execution engine observes a simulated fill at decision time.  It
        cannot know the independent forward outcome yet, so it must not call
        :meth:`record_tick` with ``actual_direction=predicted_direction``.
        Such a self-label would make the prediction MAE tautologically zero
        and could falsely satisfy the Paper→Testnet gate.
        """

        self.metrics.total_ticks += 1
        self._record_cost_observation(estimated_cost_bps, actual_cost_bps)

    def _record_cost_observation(self, estimated_cost_bps: float, actual_cost_bps: float) -> None:
        if estimated_cost_bps > 0 and actual_cost_bps > 0:
            cost_err = abs(estimated_cost_bps - actual_cost_bps) / estimated_cost_bps * 100
            self.metrics.cost_observations += 1
            n_cost = self.metrics.cost_observations
            self.metrics.cost_estimated_vs_actual_mae = (
                self.metrics.cost_estimated_vs_actual_mae * (n_cost - 1) + cost_err
            ) / n_cost

    def record_incident(self, level: str) -> None:
        """记录事件。"""
        if level == "P0":
            self.metrics.p0_incidents += 1

    def record_fill_to_ledger(
        self,
        ledger,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        spread_cost: float = 0.0,
        slippage_cost: float = 0.0,
    ) -> str | None:
        """BD-T16: 将 Paper 成交写入复式账本。

        包含手续费、价差和滑点成本的分录。
        """
        from beidou_safety.execution.ledger import (
            AccountType,
            LedgerTransaction,
            LedgerTransactionType,
            Posting,
            PostingSide,
        )
        from beidou_shared.types import AccountId, CorrelationId, InstrumentId, MonetaryValue, VenueId

        notional = str(qty * price)
        tx_id = f"paper-{int(time.time() * 1000)}-{symbol}"
        is_buy = side.upper() == "BUY"
        postings = [
            Posting(
                f"{tx_id}-1",
                AccountId("paper"),
                AccountType.POSITION_COST if is_buy else AccountType.CASH,
                VenueId("BINANCE"),
                InstrumentId(symbol),
                MonetaryValue(amount=notional),
                PostingSide.DEBIT,
                f"Paper {side} {qty} {symbol} @ {price}",
            ),
            Posting(
                f"{tx_id}-2",
                AccountId("paper"),
                AccountType.CASH if is_buy else AccountType.POSITION_COST,
                VenueId("BINANCE"),
                InstrumentId(symbol),
                MonetaryValue(amount=notional),
                PostingSide.CREDIT,
                f"Paper {side} {qty} {symbol} @ {price}",
            ),
        ]
        # 手续费
        if fee > 0:
            postings += (
                Posting(
                    f"{tx_id}-fee1",
                    AccountId("paper"),
                    AccountType.FEES,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount=str(fee)),
                    PostingSide.DEBIT,
                    "Paper trading fee",
                ),
                Posting(
                    f"{tx_id}-fee2",
                    AccountId("paper"),
                    AccountType.CASH,
                    VenueId("BINANCE"),
                    None,
                    MonetaryValue(amount=str(fee)),
                    PostingSide.CREDIT,
                    "Paper fee deduction",
                ),
            )
        tx = LedgerTransaction(
            transaction_id=tx_id,
            transaction_type=LedgerTransactionType.FILL,
            source_event_id=f"paper-fill-{tx_id}",
            postings=tuple(postings),
            correlation_id=CorrelationId(f"paper-{tx_id}"),
            metadata={"paper": True, "spread_cost": spread_cost, "slippage_cost": slippage_cost},
        )
        try:
            result = ledger.post(tx)
            self.metrics.total_simulated_fills += 1  # PKG28 (BDS-P1-061)
            return result
        except Exception:
            # PKG28 (BDS-P1-060): 账本写失败必须记录，不可静默吞掉
            self.metrics.ledger_write_failures += 1
            self.record_incident("P0")
            raise  # 重新抛出，让调用方知晓证据不可靠

    def record_drift(self) -> None:
        self.metrics.drift_events += 1

    def generate_report(self) -> ShadowReport:
        """生成 Paper Shadow 报告（含 Merkle 证据 manifest）。"""
        if self._start_time is not None:
            self.metrics.runtime_hours = (time.time() - self._start_time) / 3600.0

        report = ShadowReport(
            config=self.config,
            metrics=self.metrics,
            started_at=datetime.fromtimestamp(self._start_time, tz=timezone.utc) if self._start_time else None,
            completed_at=datetime.now(timezone.utc),
        )

        # PKG28 (BDS-P1-060): 账本写失败 → 运行无效
        if self.metrics.ledger_write_failures > 0:
            report.gate_result = GateResult.INVALID
            report.status = ShadowStatus.INVALID
            report.discrepancies.append(f"ledger_write_failures: {self.metrics.ledger_write_failures}")

        # Gate 判定（仅在无 ledger 失败时）
        elif report.gate_result == GateResult.UNVERIFIABLE:
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

        # 证据哈希 — 绑定新的拆分指标
        content = json.dumps(
            {
                "strategy_id": str(self.config.strategy_id),
                "runtime_hours": self.metrics.runtime_hours,
                "total_executed": self.metrics.total_executed,
                "total_predictions_with_outcome": self.metrics.total_predictions_with_outcome,
                "total_simulated_fills": self.metrics.total_simulated_fills,
                "deviation_pct": self.metrics.prediction_vs_simulation_mae,
                "cost_observations": self.metrics.cost_observations,
                "p0_incidents": self.metrics.p0_incidents,
                "ledger_write_failures": self.metrics.ledger_write_failures,
            },
            sort_keys=True,
        )
        report.evidence_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # PKG28 (BDS-P1-062): Merkle 证据 manifest
        report.merkle_manifest = self._build_merkle_manifest()

        if not ready and not self.metrics.ledger_write_failures:
            report.discrepancies.append(reason)

        return report

    def _build_merkle_manifest(self) -> dict:
        """PKG28 (BDS-P1-062): 构建 Merkle 证据 manifest。

        对每个 tick prediction 计算 leaf hash，构建 manifest。
        绑定全量预测→模拟→结果的证据链。
        """
        leaves = []
        for item in self._predictions:
            leaf_content = json.dumps(
                {
                    "tick": item.get("tick"),
                    "direction": item.get("direction"),
                    "strength": item.get("strength"),
                    "outcome_recorded": item.get("outcome_recorded", False),
                    "decision_timestamp": item.get("decision_timestamp"),
                },
                sort_keys=True,
            )
            leaf_hash = hashlib.sha256(leaf_content.encode()).hexdigest()
            leaves.append(leaf_hash)

        # 构建 Merkle root（简化版：直接对所有 leaf hashes 做 SHA-256）
        if not leaves:
            return {"merkle_root": "", "leaf_count": 0}

        combined = "|".join(leaves)
        merkle_root = hashlib.sha256(combined.encode()).hexdigest()

        return {
            "merkle_root": merkle_root,
            "leaf_count": len(leaves),
            "algorithm": "SHA-256",
            "leaf_hashes": leaves[:10],  # 仅包含前10个作为摘要
        }


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
        self._latency_jitter_ms: float = 30.0  # BD-CV24: 延迟抖动
        self._fill_probability: float = 0.85
        self._partial_fill_probability: float = 0.10
        self._cancel_fill_race_probability: float = 0.03  # BD-CV24: 撤单-成交竞态
        # Keep fee policy separate from the stochastic spread/slippage path;
        # Paper/Shadow cost validation must compare an estimate with an
        # independently realised execution cost.
        self._taker_fee_bps: float = 4.0
        self._race_events: list[dict] = []  # BD-CV24: 竞态事件记录

    def realized_cost_bps(self, side: str, avg_price: float, bid: float, ask: float) -> float:
        """Return realised execution cost versus the mid, including fee."""
        if not (0 < bid <= ask and avg_price > 0):
            return float("nan")
        mid = (bid + ask) / 2.0
        if side.upper() == "BUY":
            price_impact_bps = (avg_price - mid) / mid * 10000.0
        else:
            price_impact_bps = (mid - avg_price) / mid * 10000.0
        return max(0.0, price_impact_bps) + self._taker_fee_bps

    @property
    def taker_fee_bps(self) -> float:
        return self._taker_fee_bps

    def match(
        self, symbol: str, side: str, quantity: float, limit_price: float | None, bid: float, ask: float
    ) -> tuple[str, float, float, float]:
        """撮合订单。

        Returns:
            (status, filled_qty, avg_price, latency_ms)
        """
        # BD-CV24: 延迟 = base + jitter * random
        latency = self._base_latency_ms + self._latency_jitter_ms * self._rng.random()
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

        # BD-CV24: 撤单-成交竞态模拟
        if self._rng.random() < self._cancel_fill_race_probability:
            self._race_events.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "resolution": "cancel_wins",
                    "latency_ms": latency,
                }
            )
            return (PaperOrderStatus.CANCELED.value, 0.0, 0.0, latency)

        return (PaperOrderStatus.FILLED.value, quantity, exec_price, latency)

    def simulate_cancel_fill_race(self, order_id: str, symbol: str, side: str, quantity: float) -> tuple[str, float]:
        """BD-CV24: 模拟撤单-成交竞态。

        Paper 能产生 partial fill/reject/cancel-fill race。
        Returns (resolution, filled_qty)。
        """
        roll = self._rng.random()
        if roll < 0.3:
            # 撤单先到 — 取消成功
            self._race_events.append(
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "resolution": "cancel_wins",
                }
            )
            return ("CANCEL_WINS", 0.0)
        elif roll < 0.7:
            # 成交先到 — 部分成交后取消剩余
            fill_pct = 0.3 + self._rng.random() * 0.5
            filled = quantity * fill_pct
            self._race_events.append(
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "filled": filled,
                    "resolution": "fill_then_cancel",
                }
            )
            return ("FILL_THEN_CANCEL", filled)
        else:
            # 全部成交 — cancel 到达时已全部成交
            self._race_events.append(
                {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "resolution": "fill_wins",
                }
            )
            return ("FILL_WINS", quantity)

    def race_event_count(self) -> int:
        """BD-CV24: 竞态事件计数。"""
        return len(self._race_events)

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
