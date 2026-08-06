"""实盘认证框架 — G5 Testnet 到 G8 无人值守的完整认证阶梯。

PKG-35~40: Gate 独立发证。P0 立即停止并回退。
每个场景独立 PASS/FAIL/NOT_VERIFIABLE。
G5: Testnet 协议认证。G6: Shadow 连续运行。G7-L2~L5: 实盘阶梯。
G8: 30天无人值守 + Owner失联安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import (
    GateResult,
)


class CertificationGate(str, Enum):
    """认证 Gate 等级。"""

    G5_TESTNET = "G5_TESTNET"
    G6_SHADOW = "G6_SHADOW"
    G7_L2_CANARY = "G7_L2_CANARY"
    G7_L3_RAMP = "G7_L3_RAMP"
    G7_L4_NORMAL = "G7_L4_NORMAL"
    G7_L5_CHAMPION = "G7_L5_CHAMPION"
    G8_UNATTENDED = "G8_UNATTENDED"


class ScenarioStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True, slots=True)
class CertificationScenario:
    """认证场景定义 — 每个场景独立评估。"""

    scenario_id: str
    name: str
    description: str
    gate: CertificationGate
    category: str  # e.g., "idempotency", "reconciliation", "recovery"
    required_evidence: list[str] = field(default_factory=list)
    max_duration_seconds: float = 300.0
    is_blocking: bool = True  # P0=Fail 时整个 Gate 失败


@dataclass
class ScenarioResult:
    """单个场景的认证结果。"""

    scenario: CertificationScenario
    status: ScenarioStatus = ScenarioStatus.NOT_RUN
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    error_detail: str = ""
    observation_period_seconds: float = 0.0

    def is_pass(self) -> bool:
        return self.status == ScenarioStatus.PASS

    def summary(self) -> str:
        return f"[{self.status.value}] {self.scenario.name}: {self.error_detail or 'OK'}"


@dataclass
class GateCertificate:
    """Gate 独立证书。P0 立即停止并回退至上一 Gate。"""

    certificate_id: str
    gate: CertificationGate
    result: GateResult
    scenarios: list[ScenarioResult] = field(default_factory=list)
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    capital_limit: float = 0.0
    max_positions: int = 0
    max_concurrent_orders: int = 0
    degradation_conditions: list[str] = field(default_factory=list)
    evidence_manifest: list[str] = field(default_factory=list)
    signer: str = ""
    signature: str = ""
    previous_certificate_id: str | None = None
    blocking_failures: list[str] = field(default_factory=list)

    def is_pass(self) -> bool:
        return self.result == GateResult.PASS

    def blocking_p0_count(self) -> int:
        return len([s for s in self.scenarios if s.scenario.is_blocking and s.status == ScenarioStatus.FAIL])


class CertificationFramework:
    """认证框架基类 — 管理场景、运行认证、生成证书。"""

    def __init__(self, gate: CertificationGate) -> None:
        self.gate = gate
        self._scenarios: list[CertificationScenario] = []
        self._results: dict[str, ScenarioResult] = {}
        self._certificates: list[GateCertificate] = []

    def register_scenario(self, scenario: CertificationScenario) -> None:
        if scenario.gate != self.gate:
            raise ValueError(f"Scenario gate {scenario.gate} != framework gate {self.gate}")
        self._scenarios.append(scenario)

    def get_scenarios(self) -> list[CertificationScenario]:
        return list(self._scenarios)

    def record_result(self, result: ScenarioResult) -> None:
        self._results[result.scenario.scenario_id] = result

    def get_result(self, scenario_id: str) -> ScenarioResult | None:
        return self._results.get(scenario_id)

    def all_scenarios_complete(self) -> bool:
        return all(scenario.scenario_id in self._results for scenario in self._scenarios)

    def evaluate(self) -> GateCertificate:
        """评估所有场景并生成 Gate 证书。P0 立即 FAIL。"""
        scenarios_completed: list[ScenarioResult] = []
        blocking_p0 = False
        blocking_failures: list[str] = []

        for scenario in self._scenarios:
            result = self._results.get(scenario.scenario_id)
            if result is None:
                result = ScenarioResult(
                    scenario=scenario,
                    status=ScenarioStatus.NOT_RUN,
                    error_detail="Not executed",
                )
            scenarios_completed.append(result)

            if scenario.is_blocking and result.status == ScenarioStatus.FAIL:
                blocking_p0 = True
                blocking_failures.append(scenario.scenario_id)

        gate_result = GateResult.FAIL if blocking_p0 else GateResult.PASS

        # 检查 NOT_VERIFIABLE
        not_verifiable = [r for r in scenarios_completed if r.status == ScenarioStatus.NOT_VERIFIABLE]
        if not_verifiable and not blocking_p0:
            # 有 NOT_VERIFIABLE 但没有 P0，至少要求所有 blocking 场景通过
            all_blocking_pass = all(
                r.status == ScenarioStatus.PASS for r in scenarios_completed if r.scenario.is_blocking
            )
            gate_result = GateResult.PASS if all_blocking_pass else GateResult.FAIL

        cert = GateCertificate(
            certificate_id=f"cert-{self.gate.value}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            gate=self.gate,
            result=gate_result,
            scenarios=scenarios_completed,
            blocking_failures=blocking_failures,
            degradation_conditions=["P0_FAILURE"] if blocking_p0 else [],
        )
        self._certificates.append(cert)
        return cert

    def latest_certificate(self) -> GateCertificate | None:
        return self._certificates[-1] if self._certificates else None


# ============================================================
# G5 Testnet 认证
# ============================================================


class G5TestnetCertification(CertificationFramework):
    """G5 Testnet 协议认证。使用 Testnet/Demo 认证协议层正确性。"""

    def __init__(self) -> None:
        super().__init__(CertificationGate.G5_TESTNET)
        self._register_default_scenarios()

    def _register_default_scenarios(self) -> None:
        """注册 G5 默认认证场景。"""
        scenarios = [
            CertificationScenario(
                scenario_id="g5-client-idempotency",
                name="Client Order ID Idempotency",
                description="验证 clientOrderId 幂等 — 相同 ID 重复发送不产生重复订单",
                gate=CertificationGate.G5_TESTNET,
                category="idempotency",
                required_evidence=["order_log", "exchange_response", "ledger_entries"],
            ),
            CertificationScenario(
                scenario_id="g5-rate-limiting",
                name="Rate Limit Handling",
                description="验证限频处理 — 被限频后重试不丢失订单、不产生重复",
                gate=CertificationGate.G5_TESTNET,
                category="rate_limit",
                required_evidence=["rate_limit_event", "retry_log", "order_result"],
            ),
            CertificationScenario(
                scenario_id="g5-partial-fill",
                name="Partial Fill Handling",
                description="验证部分成交 — 订单状态正确转换、未成交部分正确处理",
                gate=CertificationGate.G5_TESTNET,
                category="order_lifecycle",
                required_evidence=["fill_events", "order_state_transition", "ledger_balance"],
            ),
            CertificationScenario(
                scenario_id="g5-cancel-race",
                name="Cancel vs Fill Race Condition",
                description="验证撤单竞态 — 撤单与成交并发时的正确行为",
                gate=CertificationGate.G5_TESTNET,
                category="order_lifecycle",
                required_evidence=["cancel_request", "fill_event", "final_order_state"],
            ),
            CertificationScenario(
                scenario_id="g5-conditional-orders",
                name="Conditional Orders (Stop/TakeProfit)",
                description="验证条件单 — Stop Loss 和 Take Profit 正确触发",
                gate=CertificationGate.G5_TESTNET,
                category="conditional",
                required_evidence=["trigger_event", "order_creation", "fill_result"],
            ),
            CertificationScenario(
                scenario_id="g5-user-data-stream",
                name="User Data Stream Events",
                description="验证用户数据流 — 订单/成交/余额更新正确接收",
                gate=CertificationGate.G5_TESTNET,
                category="user_stream",
                required_evidence=["stream_events", "event_sequence", "no_gaps"],
            ),
            CertificationScenario(
                scenario_id="g5-reconciliation",
                name="Testnet Account Reconciliation",
                description="验证账户对账 — 系统账本与交易所余额匹配",
                gate=CertificationGate.G5_TESTNET,
                category="reconciliation",
                required_evidence=["system_balance", "exchange_balance", "differences"],
            ),
        ]
        for s in scenarios:
            self.register_scenario(s)

    def run_idempotency_check(self, client_order_id: str, order_count: int, duplicate_orders: int) -> ScenarioResult:
        s = next(s for s in self._scenarios if s.scenario_id == "g5-client-idempotency")
        result = ScenarioResult(scenario=s, started_at=datetime.now(timezone.utc))
        if duplicate_orders == 0 and order_count > 0:
            result.status = ScenarioStatus.PASS
            result.evidence = {
                "client_order_id": client_order_id,
                "order_count": order_count,
                "duplicate_orders": duplicate_orders,
            }
        elif duplicate_orders > 0:
            result.status = ScenarioStatus.FAIL
            result.error_detail = f"Found {duplicate_orders} duplicate orders for {client_order_id}"
        else:
            result.status = ScenarioStatus.NOT_VERIFIABLE
            result.error_detail = "No orders placed — cannot verify"
        result.completed_at = datetime.now(timezone.utc)
        self.record_result(result)
        return result


# ============================================================
# G6 Shadow 认证
# ============================================================


class G6ShadowCertification(CertificationFramework):
    """G6 实时 Shadow 认证。理论订单不调用交易 API，使用真实市场数据验证。"""

    def __init__(self) -> None:
        super().__init__(CertificationGate.G6_SHADOW)
        self._register_default_scenarios()

    def _register_default_scenarios(self) -> None:
        scenarios = [
            CertificationScenario(
                scenario_id="g6-signal-freshness",
                name="Signal Freshness Calibration",
                description="验证信号新鲜度 — 理论订单与实际行情时间对齐",
                gate=CertificationGate.G6_SHADOW,
                category="signal_quality",
                required_evidence=["signal_timestamps", "market_timestamps", "latency_distribution"],
            ),
            CertificationScenario(
                scenario_id="g6-cost-estimation",
                name="Cost Model Accuracy",
                description="验证成本模型 — 理论成交 vs 实际成交成本对比",
                gate=CertificationGate.G6_SHADOW,
                category="cost_model",
                required_evidence=["predicted_cost", "actual_cost", "deviation_bps"],
            ),
            CertificationScenario(
                scenario_id="g6-strategy-conflict",
                name="Strategy Conflict Detection",
                description="验证冲突检测 — 多策略同时理论执行无冲突",
                gate=CertificationGate.G6_SHADOW,
                category="strategy",
                required_evidence=["conflict_log", "resolution_log"],
            ),
            CertificationScenario(
                scenario_id="g6-recovery",
                name="Shadow Recovery from Disconnection",
                description="验证断线恢复 — Shadow 断线后正确恢复并继续理论执行",
                gate=CertificationGate.G6_SHADOW,
                category="recovery",
                required_evidence=["disconnect_event", "recovery_event", "state_consistency"],
            ),
            CertificationScenario(
                scenario_id="g6-continuous-runtime",
                name="Continuous Runtime (Policy Duration)",
                description="验证连续运行 ≥ Policy 要求时间，不使用时间压缩",
                gate=CertificationGate.G6_SHADOW,
                category="runtime",
                required_evidence=["runtime_duration", "policy_duration", "no_time_compression"],
                max_duration_seconds=86400.0,  # 至少24小时
            ),
        ]
        for s in scenarios:
            self.register_scenario(s)

    def record_runtime_check(
        self, actual_duration_seconds: float, policy_duration_seconds: float, time_compressed: bool
    ) -> ScenarioResult:
        s = next(s for s in self._scenarios if s.scenario_id == "g6-continuous-runtime")
        result = ScenarioResult(scenario=s, started_at=datetime.now(timezone.utc))
        if time_compressed:
            result.status = ScenarioStatus.FAIL
            result.error_detail = "Time compression detected — real elapsed time required"
        elif actual_duration_seconds >= policy_duration_seconds:
            result.status = ScenarioStatus.PASS
            result.evidence = {"actual_duration": actual_duration_seconds, "required": policy_duration_seconds}
        else:
            result.status = ScenarioStatus.NOT_VERIFIABLE
            result.error_detail = f"Only {actual_duration_seconds}s of required {policy_duration_seconds}s"
        result.completed_at = datetime.now(timezone.utc)
        self.record_result(result)
        return result


# ============================================================
# 实盘阶梯认证框架 (状态: PIVOT — 待算法收敛后重新激活)
# ============================================================


class G7LiveCertification(CertificationFramework):
    """G7 实盘阶梯认证。L2→L3→L4→L5 逐级晋级。"""

    def __init__(self, level: CertificationGate) -> None:
        super().__init__(level)
        self._register_default_scenarios()

    def _register_default_scenarios(self) -> None:
        scenarios = [
            CertificationScenario(
                scenario_id="g7-capital-limit",
                name="Capital Limit Compliance",
                description="验证资金始终低于当前 Level 的硬限制",
                gate=self.gate,
                category="capital",
                required_evidence=["capital_usage_history", "limit_violations", "max_usage"],
            ),
            CertificationScenario(
                scenario_id="g7-order-integrity",
                name="Order Integrity (No Duplicate/Orphan)",
                description="验证无重复订单、孤儿订单、未保护仓位",
                gate=self.gate,
                category="order_integrity",
                required_evidence=["order_audit_log", "duplicate_check", "orphan_check"],
            ),
            CertificationScenario(
                scenario_id="g7-cost-realization",
                name="Realized Cost vs Prediction",
                description="验证真实成交、手续费、滑点与预测一致",
                gate=self.gate,
                category="cost",
                required_evidence=["predicted_cost", "realized_cost", "slippage_actual"],
            ),
            CertificationScenario(
                scenario_id="g7-protection-orders",
                name="Protection Order Execution",
                description="验证保护订单（止损/条件单）正确触发",
                gate=self.gate,
                category="protection",
                required_evidence=["protection_order_log", "trigger_event", "execution_result"],
            ),
            CertificationScenario(
                scenario_id="g7-reconciliation-live",
                name="Live Reconciliation Daily",
                description="验证每日对账 — 系统账本与交易所完全匹配",
                gate=self.gate,
                category="reconciliation",
                required_evidence=["daily_reconciliation", "differences_log", "resolution_log"],
            ),
            CertificationScenario(
                scenario_id="g7-degradation-triggers",
                name="Degradation & Stop Triggers",
                description="验证降级和停止条件正确触发 — 硬停止/最大交易次数/资金边界",
                gate=self.gate,
                category="risk",
                required_evidence=["degradation_event", "stop_reason", "action_log"],
            ),
        ]
        for s in scenarios:
            self.register_scenario(s)


def create_l2_canary_certification() -> G7LiveCertification:
    """G7-L2 Canary: 极小资金、单品种、有限并发。"""
    cert = G7LiveCertification(CertificationGate.G7_L2_CANARY)
    cert.register_scenario(
        CertificationScenario(
            scenario_id="g7-l2-single-instrument",
            name="Single Instrument Constraint",
            description="Canary 仅使用批准的单个或少量 instrument",
            gate=CertificationGate.G7_L2_CANARY,
            category="constraints",
            required_evidence=["instrument_list", "trade_log"],
        )
    )
    cert.register_scenario(
        CertificationScenario(
            scenario_id="g7-l2-hard-stop",
            name="Hard Stop Enforcement",
            description="设置硬停止、最大交易次数，不可自动提高资金边界",
            gate=CertificationGate.G7_L2_CANARY,
            category="constraints",
            required_evidence=["stop_config", "stop_trigger_log"],
        )
    )
    cert.register_scenario(
        CertificationScenario(
            scenario_id="g7-l2-any-discrepancy-locks",
            name="Account Discrepancy → Immediate LOCK",
            description="任何未解释账户差异立即 LOCKED",
            gate=CertificationGate.G7_L2_CANARY,
            category="risk",
            required_evidence=["discrepancy_event", "lock_action"],
        )
    )
    return cert


def create_l3_ramp_certification() -> G7LiveCertification:
    """G7-L3 Ramp: 渐进增加资金比例。"""
    return G7LiveCertification(CertificationGate.G7_L3_RAMP)


def create_l4_normal_certification() -> G7LiveCertification:
    """G7-L4 Normal: 标准生产运行。"""
    return G7LiveCertification(CertificationGate.G7_L4_NORMAL)


def create_l5_champion_certification() -> G7LiveCertification:
    """G7-L5 Champion: 最高级别生产运行。"""
    return G7LiveCertification(CertificationGate.G7_L5_CHAMPION)


# ============================================================
# G8 30天无人值守认证
# ============================================================


class G8UnattendedCertification(CertificationFramework):
    """G8 实际30天无人值守 + Owner失联安全认证。"""

    def __init__(self) -> None:
        super().__init__(CertificationGate.G8_UNATTENDED)
        self._register_default_scenarios()

    def _register_default_scenarios(self) -> None:
        scenarios = [
            CertificationScenario(
                scenario_id="g8-real-elapsed-30d",
                name="Real 30-Day Continuous Runtime",
                description="按真实经过时间连续运行至少30天，不得压缩或伪造",
                gate=CertificationGate.G8_UNATTENDED,
                category="runtime",
                required_evidence=["start_timestamp", "end_timestamp", "total_elapsed_seconds", "no_gaps"],
                max_duration_seconds=2592000.0,  # 30 days
            ),
            CertificationScenario(
                scenario_id="g8-data-integrity",
                name="Data Chain Integrity (30 days)",
                description="30天数据、交易、风险、保护、账本、对账连续完整无缺",
                gate=CertificationGate.G8_UNATTENDED,
                category="integrity",
                required_evidence=["daily_reports", "ledger_continuity", "reconciliation_log"],
            ),
            CertificationScenario(
                scenario_id="g8-drift-self-heal",
                name="Drift Detection & Self-Healing",
                description="漂移检测和自愈全部自动化，无需Owner介入",
                gate=CertificationGate.G8_UNATTENDED,
                category="autonomy",
                required_evidence=["drift_events", "self_heal_actions", "recovery_log"],
            ),
            CertificationScenario(
                scenario_id="g8-owner-disconnected",
                name="Owner Disconnection Safety",
                description="Owner失联时系统安全运行，LOCK保持安全状态需人工解除",
                gate=CertificationGate.G8_UNATTENDED,
                category="safety",
                required_evidence=["owner_disconnect_event", "system_behavior", "lock_state"],
            ),
            CertificationScenario(
                scenario_id="g8-incident-auto-resolve",
                name="Incident Auto-Detection & Resolution",
                description="所有事故记录检测、自动动作、恢复和最终状态完整",
                gate=CertificationGate.G8_UNATTENDED,
                category="incident",
                required_evidence=["incident_log", "auto_action_log", "resolution_log"],
            ),
            CertificationScenario(
                scenario_id="g8-backup-verify",
                name="Backup & PITR Verification (30 days)",
                description="备份和 PITR 在每个周期验证通过",
                gate=CertificationGate.G8_UNATTENDED,
                category="backup",
                required_evidence=["backup_verification_log", "pitr_test_results"],
            ),
            CertificationScenario(
                scenario_id="g8-no-human-disguise",
                name="No Human Intervention Disguised as Automation",
                description="不得把人工操作伪装成自动化 — 所有操作有归属记录",
                gate=CertificationGate.G8_UNATTENDED,
                category="integrity",
                required_evidence=["operation_audit_log", "automation_vs_manual"],
            ),
        ]
        for s in scenarios:
            self.register_scenario(s)

    def verify_elapsed_time(self, start: datetime, end: datetime, min_days: int = 30) -> ScenarioResult:
        s = next(s for s in self._scenarios if s.scenario_id == "g8-real-elapsed-30d")
        result = ScenarioResult(scenario=s, started_at=datetime.now(timezone.utc))
        elapsed = end - start
        if elapsed < timedelta(days=min_days):
            result.status = ScenarioStatus.FAIL
            result.error_detail = f"Only {elapsed.total_seconds() / 86400:.1f} days elapsed (need {min_days})"
        else:
            result.status = ScenarioStatus.PASS
            result.evidence = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "elapsed_days": elapsed.total_seconds() / 86400,
            }
        result.completed_at = datetime.now(timezone.utc)
        self.record_result(result)
        return result

    def verify_owner_disconnect(self, last_owner_action: datetime, system_behavior: str) -> ScenarioResult:
        s = next(s for s in self._scenarios if s.scenario_id == "g8-owner-disconnected")
        result = ScenarioResult(scenario=s, started_at=datetime.now(timezone.utc))
        if system_behavior == "LOCKED_SAFE":
            result.status = ScenarioStatus.PASS
            result.evidence = {"last_owner_action": last_owner_action.isoformat(), "behavior": system_behavior}
        elif system_behavior == "AUTO_RESUMED":
            result.status = ScenarioStatus.FAIL
            result.error_detail = "System auto-resumed after owner disconnect — must LOCK"
        else:
            result.status = ScenarioStatus.NOT_VERIFIABLE
        result.completed_at = datetime.now(timezone.utc)
        self.record_result(result)
        return result


# ============================================================
# 认证管理器 — 统筹所有 Gate 认证
# ============================================================


class CertificationManager:
    """认证管理器 — 管理从 G5 到 G8 的完整认证流程。"""

    def __init__(self) -> None:
        self._frameworks: dict[CertificationGate, CertificationFramework] = {}
        self._all_certificates: list[GateCertificate] = []

    def register_framework(self, framework: CertificationFramework) -> None:
        self._frameworks[framework.gate] = framework

    def get_framework(self, gate: CertificationGate) -> CertificationFramework | None:
        return self._frameworks.get(gate)

    def evaluate_all(self) -> list[GateCertificate]:
        certs: list[GateCertificate] = []
        for _gate, fw in self._frameworks.items():
            cert = fw.evaluate()
            certs.append(cert)
            self._all_certificates.append(cert)
        return certs

    def can_promote(self, from_gate: CertificationGate, to_gate: CertificationGate) -> bool:
        """检查是否可以从一个 Gate 晋升到下一个。

        from_gate 必须有 PASS 证书。
        所有 from_gate 和 to_gate 之间的 Gate 必须有 PASS 证书。
        to_gate 的框架必须已注册。
        """
        gate_order = list(CertificationGate)
        if to_gate not in gate_order or from_gate not in gate_order:
            return False
        from_idx = gate_order.index(from_gate)
        to_idx = gate_order.index(to_gate)
        if to_idx <= from_idx:
            return False

        # from_gate 必须有 PASS 证书
        from_fw = self._frameworks.get(from_gate)
        if from_fw is None:
            return False
        from_cert = from_fw.latest_certificate()
        if from_cert is None or not from_cert.is_pass():
            return False

        # to_gate 框架必须已注册
        to_fw = self._frameworks.get(to_gate)
        if to_fw is None:
            return False

        # 检查中间 Gate（不含 to_gate）
        for i in range(from_idx + 1, to_idx):
            intermediate_gate = gate_order[i]
            fw = self._frameworks.get(intermediate_gate)
            if fw is None:
                return False
            cert = fw.latest_certificate()
            if cert is None or not cert.is_pass():
                return False

        return True

    def any_p0_failure(self) -> bool:
        return any(cert.blocking_p0_count() > 0 for cert in self._all_certificates)

    def should_degrade_to(self, gate: CertificationGate) -> bool:
        """P0 失败立即回退到指定 Gate。"""
        return self.any_p0_failure()


# ============================================================
# BD-P2-18: 生产资本阶梯 — 不可跳过、自动回退
# ============================================================


@dataclass
class CapitalLevel:
    """BD-P2-18: 资本阶梯级别定义。

    每个级别有资本上限、杠杆上限、最低运行时间和 Gate 要求。
    """

    level: str  # "shadow", "canary", "ramp", "normal", "champion"
    gate: CertificationGate
    max_capital: float  # USD
    max_leverage: float
    min_unattended_hours: float = 0.0
    auto_rollback: bool = True  # P0 失败自动回退上一级


# BD-P2-18: 资本阶梯级别定义
CAPITAL_LADDER: list[CapitalLevel] = [
    CapitalLevel("shadow", CertificationGate.G6_SHADOW, max_capital=0.0, max_leverage=0.0),
    CapitalLevel("canary", CertificationGate.G7_L2_CANARY, max_capital=500.0, max_leverage=1.0, min_unattended_hours=24.0),
    CapitalLevel("ramp", CertificationGate.G7_L3_RAMP, max_capital=2000.0, max_leverage=2.0, min_unattended_hours=72.0),
    CapitalLevel("normal", CertificationGate.G7_L4_NORMAL, max_capital=10000.0, max_leverage=3.0, min_unattended_hours=168.0),
    CapitalLevel("champion", CertificationGate.G7_L5_CHAMPION, max_capital=50000.0, max_leverage=3.0, min_unattended_hours=720.0),
]


class ProductionLadder:
    """BD-P2-18: 生产资本阶梯 — 不可跳过，自动回退。

    规则:
    - Gates 不可跳过 (AC-18-01)
    - Canary 资本和杠杆永不超过证书上限 (AC-18-02)
    - 无人值守证据使用真实经过时间 (AC-18-03)
    - 任何 Gate 失败自动回退上一级 (AC-18-04)
    """

    def __init__(self, cert_manager: CertificationManager):
        self._cert_manager = cert_manager
        self._current_level_index: int = 0  # 起始于 shadow (level 0)
        self._level_history: list[tuple[str, str, datetime]] = []  # (level, reason, timestamp)

    @property
    def current_level(self) -> CapitalLevel:
        return CAPITAL_LADDER[self._current_level_index]

    @property
    def max_allowed_capital(self) -> float:
        return self.current_level.max_capital

    @property
    def max_allowed_leverage(self) -> float:
        return self.current_level.max_leverage

    def can_advance_to(self, target_level: str) -> tuple[bool, str]:
        """检查是否可以晋升到目标级别。

        BD-P2-18 AC-18-01: 不可跳过任何 Gate。
        """
        target_idx = next(
            (i for i, lvl in enumerate(CAPITAL_LADDER) if lvl.level == target_level), -1
        )
        if target_idx == -1:
            return False, f"Unknown level: {target_level}"
        if target_idx <= self._current_level_index:
            return False, f"Already at or above {target_level}"

        # 检查所有中间 Gate
        for i in range(self._current_level_index + 1, target_idx + 1):
            level = CAPITAL_LADDER[i]
            if not self._cert_manager.can_advance_to(level.gate):
                return False, f"Gate {level.gate.value} not passed — cannot skip to {target_level}"
        return True, "OK"

    def advance(self, target_level: str) -> bool:
        """晋升到目标级别。失败自动回退。"""
        ok, reason = self.can_advance_to(target_level)
        if not ok:
            self._log_level_change(f"FAILED: {target_level}", reason)
            return False

        target_idx = next(i for i, lvl in enumerate(CAPITAL_LADDER) if lvl.level == target_level)
        old_level = self.current_level.level
        self._current_level_index = target_idx
        self._log_level_change(target_level, f"Promoted from {old_level}")
        return True

    def check_and_rollback(self) -> str | None:
        """BD-P2-18 AC-18-04: 检查 P0 失败并自动回退。"""
        if self._cert_manager.any_p0_failure() and self._current_level_index > 0:
            old_level = self.current_level.level
            self._current_level_index -= 1
            new_level = self.current_level.level
            reason = f"P0 failure detected — auto rollback from {old_level} to {new_level}"
            self._log_level_change(new_level, reason)
            return reason
        return None

    def force_rollback(self, reason: str) -> str:
        """强制回退到上一级。"""
        if self._current_level_index <= 0:
            return "Already at shadow (lowest level)"
        old = self.current_level.level
        self._current_level_index -= 1
        msg = f"Forced rollback from {old} to {self.current_level.level}: {reason}"
        self._log_level_change(self.current_level.level, msg)
        return msg

    def validate_capital(self, amount: float, leverage: float) -> tuple[bool, str]:
        """BD-P2-18 AC-18-02: 验证资本和杠杆不超证书上限。"""
        level = self.current_level
        if amount > level.max_capital:
            return False, f"Capital {amount} exceeds {level.level} cap of {level.max_capital}"
        if leverage > level.max_leverage:
            return False, f"Leverage {leverage}x exceeds {level.level} cap of {level.max_leverage}x"
        return True, "OK"

    def _log_level_change(self, level: str, reason: str) -> None:
        self._level_history.append((level, reason, datetime.now(timezone.utc)))
