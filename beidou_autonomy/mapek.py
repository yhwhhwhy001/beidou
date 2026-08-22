"""MAPE-K 控制器实现。Monitor→Analyze→Plan→Execute→Knowledge。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from beidou_shared.types import CorrelationId


class RecoveryAction(str, Enum):
    NOOP = "NOOP"
    RESTART_MODULE = "RESTART_MODULE"
    ROLLBACK_CHECKPOINT = "ROLLBACK_CHECKPOINT"
    FAILOVER = "FAILOVER"
    DEGRADE_TO_NO_NEW_RISK = "DEGRADE_TO_NO_NEW_RISK"
    DEGRADE_TO_EXIT_ONLY = "DEGRADE_TO_EXIT_ONLY"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    LOCK = "LOCK"


class RecoveryResult(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    module_name: str
    state_snapshot: dict[str, Any]
    sequence_number: int
    invariants_valid: bool
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: CorrelationId | None = None


@dataclass
class FaultFingerprint:
    fingerprint_id: str = field(default_factory=lambda: str(uuid4()))
    symptom_vector: dict[str, float] = field(default_factory=dict)
    root_cause: str | None = None
    effective_actions: list[RecoveryAction] = field(default_factory=list)
    ineffective_actions: list[RecoveryAction] = field(default_factory=list)
    recovery_time_seconds: float | None = None
    module_version: str = ""
    incident_count: int = 0
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    approved_runbook: str | None = None

    def similarity(self, other: FaultFingerprint) -> float:
        if not self.symptom_vector or not other.symptom_vector:
            return 0.0
        common = set(self.symptom_vector.keys()) & set(other.symptom_vector.keys())
        if not common:
            return 0.0
        all_keys = set(self.symptom_vector.keys()) | set(other.symptom_vector.keys())
        matches = sum(1 for k in common if abs(self.symptom_vector[k] - other.symptom_vector[k]) < 0.1)
        return matches / len(all_keys)


@dataclass
class FingerprintMatch:
    fingerprint: FaultFingerprint
    similarity_score: float
    recommended_action: RecoveryAction
    is_approved: bool


class MAPEKController:
    """MAPE-K 自主运维控制器。自愈≠无限重启。"""

    def __init__(self) -> None:
        self._fingerprints: dict[str, FaultFingerprint] = {}
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        self._recovery_counter: dict[str, int] = {}
        self._recovery_log: list[dict] = []  # P1-046: 恢复动作执行日志
        self._recovery_evidence: list[dict] = []  # P1-046: 前后状态证据
        self._max_restart_attempts = 3
        self._similarity_threshold = 0.6

    def register_fingerprint(self, fp: FaultFingerprint) -> None:
        self._fingerprints[fp.fingerprint_id] = fp

    def find_similar(
        self, symptom_vector: dict[str, float], min_similarity: float | None = None
    ) -> list[FingerprintMatch]:
        threshold = min_similarity if min_similarity is not None else self._similarity_threshold
        query = FaultFingerprint(symptom_vector=symptom_vector)
        matches: list[FingerprintMatch] = []
        for fp in self._fingerprints.values():
            sim = fp.similarity(query)
            if sim >= threshold:
                rec_action = fp.effective_actions[0] if fp.effective_actions else RecoveryAction.NOOP
                matches.append(
                    FingerprintMatch(
                        fingerprint=fp,
                        similarity_score=sim,
                        recommended_action=rec_action,
                        is_approved=fp.approved_runbook is not None,
                    )
                )
        return sorted(matches, key=lambda m: m.similarity_score, reverse=True)

    def decide_action(self, symptom_vector: dict[str, float], module_name: str) -> tuple[RecoveryAction, str]:
        matches = self.find_similar(symptom_vector)
        if not matches:
            return RecoveryAction.LOCK, "No similar fingerprint found; safe default is LOCK"
        best = matches[0]
        if not best.is_approved:
            return RecoveryAction.DEGRADE_TO_NO_NEW_RISK, "Best match has no approved runbook; degrading"
        if best.recommended_action == RecoveryAction.RESTART_MODULE:
            count = self._recovery_counter.get(module_name, 0)
            if count >= self._max_restart_attempts:
                return (
                    RecoveryAction.LOCK,
                    f"Module {module_name} restarted {count} times; refusing further auto-restart",
                )
        return (
            best.recommended_action,
            f"Matched fingerprint {best.fingerprint.fingerprint_id} with similarity {best.similarity_score:.2f}",
        )

    def save_checkpoint(self, module_name: str, state: dict[str, Any], invariants_valid: bool) -> Checkpoint:
        cp = Checkpoint(
            checkpoint_id=str(uuid4()),
            module_name=module_name,
            state_snapshot=state,
            sequence_number=len(self._checkpoints.get(module_name, [])),
            invariants_valid=invariants_valid,
        )
        if module_name not in self._checkpoints:
            self._checkpoints[module_name] = []
        self._checkpoints[module_name].append(cp)
        return cp

    def get_latest_valid_checkpoint(self, module_name: str) -> Checkpoint | None:
        cps = self._checkpoints.get(module_name, [])
        for cp in reversed(cps):
            if cp.invariants_valid:
                return cp
        return None

    def execute_recovery(
        self, action: RecoveryAction, module_name: str, checkpoint: Checkpoint | None = None
    ) -> RecoveryResult:
        """PKG24 (BDS-P1-046): 执行恢复动作并产出前后事实证据。

        修复前: 仅做 action→result 映射，不实际执行或记录证据。
        修复后: 记录恢复尝试次数、前后状态和证据日志。
        """
        before = {"module": module_name, "restart_count": self.get_restart_count(module_name)}

        if action == RecoveryAction.NOOP:
            result = RecoveryResult.SUCCESS
        elif action == RecoveryAction.LOCK:
            self._recovery_log.append({"module": module_name, "action": "LOCK", "timestamp": time.time()})
            result = RecoveryResult.DEGRADED
        elif action == RecoveryAction.RESTART_MODULE:
            self._recovery_counter[module_name] = self._recovery_counter.get(module_name, 0) + 1
            self._recovery_log.append(
                {
                    "module": module_name,
                    "action": "RESTART_MODULE",
                    "attempt": self._recovery_counter[module_name],
                    "timestamp": time.time(),
                }
            )
            result = RecoveryResult.SUCCESS if checkpoint and checkpoint.invariants_valid else RecoveryResult.PARTIAL
        elif action == RecoveryAction.ROLLBACK_CHECKPOINT:
            self._recovery_log.append(
                {
                    "module": module_name,
                    "action": "ROLLBACK_CHECKPOINT",
                    "checkpoint_id": checkpoint.checkpoint_id if checkpoint else "NONE",
                    "timestamp": time.time(),
                }
            )
            result = RecoveryResult.SUCCESS if checkpoint else RecoveryResult.FAILED
        elif action == RecoveryAction.DEGRADE_TO_NO_NEW_RISK:
            self._recovery_log.append(
                {"module": module_name, "action": "DEGRADE_TO_NO_NEW_RISK", "timestamp": time.time()}
            )
            result = RecoveryResult.DEGRADED
        elif action == RecoveryAction.DEGRADE_TO_EXIT_ONLY:
            self._recovery_log.append(
                {"module": module_name, "action": "DEGRADE_TO_EXIT_ONLY", "timestamp": time.time()}
            )
            result = RecoveryResult.DEGRADED
        elif action == RecoveryAction.EMERGENCY_FLATTEN:
            self._recovery_log.append({"module": module_name, "action": "EMERGENCY_FLATTEN", "timestamp": time.time()})
            result = RecoveryResult.DEGRADED
        else:
            result = RecoveryResult.FAILED

        after = {"module": module_name, "restart_count": self.get_restart_count(module_name)}
        self._recovery_evidence.append(
            {
                "module": module_name,
                "action": action.value,
                "result": result.value,
                "before": before,
                "after": after,
                "timestamp": time.time(),
            }
        )
        return result

    def verify_recovery(self, module_name: str, invariants: dict[str, bool]) -> bool:
        """PKG24 (BDS-P0-024): 验证恢复 — 期望不变量非空且全部为 True。

        修复前: all([]) == True → 空不变量可伪装恢复成功。
        修复后: 必须至少有一个不变量且全部为 True。
        """
        if not invariants:
            return False  # Empty invariants → UNKNOWN/FAIL
        return all(invariants.values())

    def reset_restart_counter(self, module_name: str) -> None:
        self._recovery_counter.pop(module_name, None)

    def get_restart_count(self, module_name: str) -> int:
        """PKG24 (BDS-P1-047): 获取重启计数（供持久化使用）。"""
        return self._recovery_counter.get(module_name, 0)

    def restore_restart_counter(self, module_name: str, count: int) -> None:
        """PKG24 (BDS-P1-047): 从持久化存储恢复重启计数。"""
        if count > 0:
            self._recovery_counter[module_name] = count

    # --- BD-CV51: 真实控制面集成 ---

    def execute_with_authority(
        self,
        action: RecoveryAction,
        module_name: str,
        checkpoint: Checkpoint | None = None,
        control_plane: Any | None = None,
    ) -> RecoveryResult:
        """BD-CV51: 执行恢复动作并真实调用控制面。

        LOCK/NO_NEW_RISK/EXIT_ONLY/EMERGENCY_FLATTEN 必须通过
        ControlAuthority 真实生效，不能仅写日志。

        M15-F01 (P0-13) 语义诚实化:控制面动作真实执行后返回 DEGRADED
        (降级方向 fail-closed,安全);RESTART_MODULE 无真实重启执行器,
        仅计数 + checkpoint 判定(SUCCESS=checkpoint 有效,否则
        PARTIAL/FAILED)。恢复后的不变量/TruthSnapshot 验证由调用方
        通过 verify_recovery 执行(见 engine offline autopilot)。
        """
        from beidou_control.plane import ControlAction

        status_getter = getattr(control_plane, "get_status", None)
        before = {
            "module": module_name,
            "restart_count": self.get_restart_count(module_name),
            "control_state": str(status_getter() if callable(status_getter) else "UNKNOWN"),
        }

        # 映射 RecoveryAction → ControlAction
        _action_map: dict[RecoveryAction, ControlAction | None] = {
            RecoveryAction.LOCK: ControlAction.LOCK,
            RecoveryAction.DEGRADE_TO_NO_NEW_RISK: ControlAction.NO_NEW_RISK,
            RecoveryAction.DEGRADE_TO_EXIT_ONLY: ControlAction.EXIT_ONLY,
            RecoveryAction.EMERGENCY_FLATTEN: ControlAction.EMERGENCY_FLATTEN,
        }

        ctrl_action = _action_map.get(action)
        side_effect_observed = False

        # 真实执行控制面状态变更
        if ctrl_action is not None and control_plane is not None:
            try:
                control_plane.execute_action(ctrl_action)
                side_effect_observed = True
            except Exception as exc:
                self._recovery_log.append(
                    {
                        "module": module_name,
                        "action": f"{action.value}_FAILED",
                        "error": str(exc),
                        "timestamp": time.time(),
                    }
                )
                return RecoveryResult.FAILED

        if action == RecoveryAction.RESTART_MODULE:
            self._recovery_counter[module_name] = self._recovery_counter.get(module_name, 0) + 1
            # BD-CV51: SUCCESS 仅在 invariants 非空且 checkpoint 有效时
            if checkpoint and checkpoint.invariants_valid:
                side_effect_observed = True
                result = RecoveryResult.SUCCESS
            elif checkpoint:
                result = RecoveryResult.PARTIAL
            else:
                result = RecoveryResult.FAILED
        elif action == RecoveryAction.ROLLBACK_CHECKPOINT:
            result = RecoveryResult.SUCCESS if (checkpoint and checkpoint.invariants_valid) else RecoveryResult.FAILED
            if checkpoint:
                side_effect_observed = True
        elif action == RecoveryAction.NOOP:
            result = RecoveryResult.SUCCESS
        elif ctrl_action is not None:
            # 控制面动作 — 真实 side effect 已观察
            result = RecoveryResult.DEGRADED
        else:
            result = RecoveryResult.FAILED

        self._recovery_log.append(
            {
                "module": module_name,
                "action": action.value,
                "result": result.value,
                "side_effect_observed": side_effect_observed,
                "timestamp": time.time(),
            }
        )

        after = {
            "module": module_name,
            "restart_count": self.get_restart_count(module_name),
            "side_effect": side_effect_observed,
        }
        self._recovery_evidence.append(
            {
                "module": module_name,
                "action": action.value,
                "result": result.value,
                "before": before,
                "after": after,
                "timestamp": time.time(),
            }
        )

        return result

    def execute_recovery_governed(
        self,
        action: RecoveryAction,
        module_name: str,
        *,
        reason: str = "",
        control_plane: Any | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> RecoveryResult:
        """M15-F01 (P0-13): 受治理的恢复执行 —— 真实控制面动作 + LOCK 安全过滤。

        decide_action 的 LOCK 默认语义(无指纹/低相似)在 log-only 时代
        无害;真实接线后未知故障模式会永久 LOCK(终态,需人工签名
        恢复)。治理规则:
        - LOCK 仅在超重启保护(reason 含 "refusing further auto-restart")
          时真实执行 —— 循环崩溃是确定的失控模式,必须终态阻断;
        - 其余 LOCK(无指纹/相似度不足)改写为 DEGRADE_TO_NO_NEW_RISK
          真实执行 —— fail-closed 但不锁死,事实恢复后由授权链
          (M19-F01)重新 RESUME。
        RESUME 方向的动作不存在于 RecoveryAction 枚举 —— 恢复执行
        只降级不升级,升级永远走 execute_authorized_resume 门禁。
        """
        if action == RecoveryAction.LOCK and "refusing further auto-restart" not in reason:
            self._recovery_log.append(
                {
                    "module": module_name,
                    "action": "LOCK_DOWNGRADED",
                    "to": RecoveryAction.DEGRADE_TO_NO_NEW_RISK.value,
                    "reason": reason,
                    "timestamp": time.time(),
                }
            )
            action = RecoveryAction.DEGRADE_TO_NO_NEW_RISK
        return self.execute_with_authority(action, module_name, checkpoint=checkpoint, control_plane=control_plane)
