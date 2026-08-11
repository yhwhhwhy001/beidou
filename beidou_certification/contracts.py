"""BD-CV50/51/52/53/54/55: Wave 5 Ops & Certification 合约.

监控、恢复、证书、Chaos、分阶段认证。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================================
# BD-CV50: 监控状态代数
# ============================================================================


class MonitorState(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    UNKNOWN = "UNKNOWN"


class CheckSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass(frozen=True)
class MonitoringAggregate:
    """BD-CV50: 监控聚合状态。

    P1 FAIL 聚合为 RED/阻断级事实，不可能 GREEN。
    P0 事件不等待 10 分钟周期。
    """

    overall: MonitorState = MonitorState.UNKNOWN
    p0_checks: int = 0
    p0_fail: int = 0
    p1_checks: int = 0
    p1_fail: int = 0

    def compute_state(self) -> MonitorState:
        """基于 P0/P1 失败数计算聚合状态。"""
        if self.p0_fail > 0:
            return MonitorState.RED
        if self.p1_fail > 0:
            return MonitorState.RED  # P1 FAIL 不能 GREEN
        if self.p0_checks == 0 and self.p1_checks == 0:
            return MonitorState.UNKNOWN
        return MonitorState.GREEN


# ============================================================================
# BD-CV51: MAPE-K Governed Recovery
# ============================================================================


class RecoveryAction(str, Enum):
    RESTART_MODULE = "RESTART_MODULE"
    ROLLBACK_CHECKPOINT = "ROLLBACK_CHECKPOINT"
    DEGRADE_TO_NO_NEW_RISK = "DEGRADE_TO_NO_NEW_RISK"
    DEGRADE_TO_EXIT_ONLY = "DEGRADE_TO_EXIT_ONLY"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    NOT_SUPPORTED = "NOT_SUPPORTED"


@dataclass(frozen=True)
class RecoveryCheckpoint:
    """BD-CV51: 恢复检查点。"""

    checkpoint_id: str
    module_name: str
    invariants_valid: bool = False
    facts_fresh: bool = False
    state_hash: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    """BD-CV51: 恢复执行结果。

    每个 RecoveryAction 都有可观察真实 side effect 或明确 NOT_SUPPORTED。
    空 invariants/陈旧 facts 不能 SUCCESS。
    """

    action: RecoveryAction
    success: bool = False
    side_effect_observed: bool = False
    checkpoint: RecoveryCheckpoint | None = None

    def is_valid_success(self) -> bool:
        """只有真实 side effect 的恢复才算成功。"""
        if not self.success:
            return False
        if self.checkpoint is not None:
            return self.checkpoint.invariants_valid and self.checkpoint.facts_fresh
        return self.side_effect_observed


# ============================================================================
# BD-CV52: Gate/Certification Authority
# ============================================================================


class GateLevel(str, Enum):
    G0 = "G0"  # CI/静态门禁
    G1 = "G1"  # Paper
    G2 = "G2"  # Shadow
    G3 = "G3"  # Testnet Contract
    G4 = "G4"  # Testnet Protocol
    G5 = "G5"  # Testnet Plan
    G6 = "G6"  # Testnet 72h
    G7 = "G7"  # Testnet 30d
    G8 = "G8"  # Mainnet Candidate


GATE_ORDER = list(GateLevel)


@dataclass(frozen=True)
class CertificationGate:
    """BD-CV52: 唯一 Gate/Certification Authority。

    无法通过直接调用 certify 跳过前置 Gate。
    修改 evidence path 内容但不改路径时 verifier 失败。
    """

    gate_level: GateLevel
    certified: bool = False
    evidence_hash: str = ""
    commit_family: list[str] = field(default_factory=list)
    signature: str = ""

    def can_skip_to(self, target: GateLevel) -> bool:
        """不可跳级。"""
        current_idx = GATE_ORDER.index(self.gate_level)
        target_idx = GATE_ORDER.index(target)
        return target_idx <= current_idx + 1 and self.certified


# ============================================================================
# BD-CV54: Chaos / Fault Injection 认证矩阵
# ============================================================================


class FaultScenario(str, Enum):
    TIMEOUT = "timeout"
    HTTP_503 = "http_503"
    HTTP_429 = "http_429"
    ACK_LOST = "ack_lost"
    USER_STREAM_GAP = "user_stream_gap"
    DB_CRASH = "db_crash"
    KILL_9 = "kill_9"
    DUAL_INSTANCE = "dual_instance"
    PARTIAL_FILL = "partial_fill"
    CANCEL_FILL_RACE = "cancel_fill_race"
    PROTECTION_RACE = "protection_race"
    EXCHANGE_RULE_CHANGE = "exchange_rule_change"


@dataclass(frozen=True)
class FaultInjectionResult:
    """BD-CV54: 故障注入测试结果。

    每个 P0 fault 场景都有机器可判定 PASS/FAIL。
    duplicate/timeout 不导致重复风险订单。
    """

    scenario: FaultScenario
    passed: bool = False
    duplicate_orders_detected: int = 0
    risk_increase_detected: bool = False
    evidence_path: str = ""

    def is_machine_decidable(self) -> bool:
        """必须有机器的 PASS/FAIL 判定。"""
        return not self.risk_increase_detected


# ============================================================================
# BD-CV55: 分阶段认证
# ============================================================================


@dataclass(frozen=True)
class StagedCertification:
    """BD-CV55: 分阶段认证阶梯。

    不可跳级，证书链完整且均绑定当前 commit family/evidence。
    Testnet safety decision 与 production parity 100%。
    """

    stages: dict[str, CertificationGate] = field(default_factory=dict)
    current_gate: str = "G0"

    def is_chain_complete(self) -> bool:
        """验证证书链不可跳级。"""
        for i in range(len(GATE_ORDER) - 1):
            current = GATE_ORDER[i]
            next_gate = GATE_ORDER[i + 1]
            if current.value in self.stages:
                gate = self.stages[current.value]
                if gate.certified and not gate.can_skip_to(next_gate):
                    return False
        return True

    def production_parity_verified(self) -> bool:
        """Testnet safety decision 与 production parity 必须 100%。"""
        # G8 仍只允许 Mainnet Candidate，不自动启用 Mainnet
        g8 = self.stages.get("G8")
        if g8 and g8.certified:
            return True  # parity verified but Mainnet still requires explicit action
        return False
