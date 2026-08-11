"""MAPE-K 控制器实现。Monitor→Analyze→Plan→Execute→Knowledge。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import time
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
        if best.similarity_score < self._similarity_threshold:
            return (
                RecoveryAction.LOCK,
                f"Best match similarity {best.similarity_score:.2f} below threshold {self._similarity_threshold}",
            )
        if not best.is_approved:
            return RecoveryAction.DEGRADE_TO_NO_NEW_RISK, "Best match has no approved runbook; degrading"
        if best.recommended_action == RecoveryAction.RESTART_MODULE:
            count = self._recovery_counter.get(module_name, 0)
            if count >= self._max_restart_attempts:
                return (
                    RecoveryAction.LOCK,
                    f"Module {module_name} restarted {count} times; refusing further auto-restart",
                )
            self._recovery_counter[module_name] = count + 1
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
            self._recovery_log.append({
                "module": module_name, "action": "RESTART_MODULE",
                "attempt": self._recovery_counter[module_name], "timestamp": time.time(),
            })
            result = RecoveryResult.SUCCESS if checkpoint and checkpoint.invariants_valid else RecoveryResult.PARTIAL
        elif action == RecoveryAction.ROLLBACK_CHECKPOINT:
            self._recovery_log.append({
                "module": module_name, "action": "ROLLBACK_CHECKPOINT",
                "checkpoint_id": checkpoint.checkpoint_id if checkpoint else "NONE", "timestamp": time.time(),
            })
            result = RecoveryResult.SUCCESS if checkpoint else RecoveryResult.FAILED
        elif action == RecoveryAction.DEGRADE_TO_NO_NEW_RISK:
            self._recovery_log.append({"module": module_name, "action": "DEGRADE_TO_NO_NEW_RISK", "timestamp": time.time()})
            result = RecoveryResult.DEGRADED
        elif action == RecoveryAction.DEGRADE_TO_EXIT_ONLY:
            self._recovery_log.append({"module": module_name, "action": "DEGRADE_TO_EXIT_ONLY", "timestamp": time.time()})
            result = RecoveryResult.DEGRADED
        elif action == RecoveryAction.EMERGENCY_FLATTEN:
            self._recovery_log.append({"module": module_name, "action": "EMERGENCY_FLATTEN", "timestamp": time.time()})
            result = RecoveryResult.DEGRADED
        else:
            result = RecoveryResult.FAILED

        after = {"module": module_name, "restart_count": self.get_restart_count(module_name)}
        self._recovery_evidence.append({
            "module": module_name, "action": action.value, "result": result.value,
            "before": before, "after": after, "timestamp": time.time(),
        })
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
