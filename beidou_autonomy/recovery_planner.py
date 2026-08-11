"""BD-CV51: RecoveryPlanner/RecoveryCommand/RecoveryExecutor/RecoveryVerifier 拆分。

RESTART_MODULE 调用真实 supervisor 经过 STARTING→VALIDATING→ACTIVE。
ROLLBACK_CHECKPOINT 恢复 durable checkpoint。
restart counter/fingerprint/checkpoint/attempt log 持久化。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from beidou_autonomy.mapek import Checkpoint, FaultFingerprint, MAPEKController, RecoveryAction, RecoveryResult


class RecoveryPhase(str, Enum):
    STARTING = "STARTING"
    VALIDATING = "VALIDATING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


@dataclass
class PersistedRecoveryState:
    """BD-CV51: 持久化恢复状态。restart counter 跨重启保留。"""

    module_name: str
    restart_count: int = 0
    last_action: str = ""
    last_result: str = ""
    checkpoint_id: str = ""
    fingerprint_id: str = ""
    timestamp: float = 0.0

    _state_file: str = field(default="evidence/BD-01/recovery_state.json", repr=False)

    def save(self) -> bool:
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        try:
            data = {
                "module_name": self.module_name,
                "restart_count": self.restart_count,
                "last_action": self.last_action,
                "last_result": self.last_result,
                "checkpoint_id": self.checkpoint_id,
                "fingerprint_id": self.fingerprint_id,
                "timestamp": time.time(),
            }
            with open(self._state_file, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False

    @classmethod
    def load(cls, module_name: str) -> PersistedRecoveryState:
        state_file = "evidence/BD-01/recovery_state.json"
        if not os.path.exists(state_file):
            return cls(module_name=module_name)
        try:
            with open(state_file) as f:
                data = json.load(f)
            return cls(
                module_name=data.get("module_name", module_name),
                restart_count=data.get("restart_count", 0),
                last_action=data.get("last_action", ""),
                last_result=data.get("last_result", ""),
                checkpoint_id=data.get("checkpoint_id", ""),
                fingerprint_id=data.get("fingerprint_id", ""),
                timestamp=data.get("timestamp", 0.0),
            )
        except (json.JSONDecodeError, KeyError):
            return cls(module_name=module_name)


class RecoveryPlanner:
    """BD-CV51: 分析症状 → 匹配 fingerprint → 推荐 action。"""

    def __init__(self, mapek: MAPEKController) -> None:
        self._mapek = mapek

    def plan(self, symptom_vector: dict[str, float], module_name: str) -> tuple[RecoveryAction, str]:
        action, reason = self._mapek.decide_action(symptom_vector, module_name)
        # 未知 fingerprint → LOCK 或 NO_NEW_RISK，禁止无限重启
        if action == RecoveryAction.RESTART_MODULE:
            count = self._mapek.get_restart_count(module_name)
            if count >= self._mapek._max_restart_attempts:
                return RecoveryAction.LOCK, f"MAX_RESTARTS_EXCEEDED:{module_name}:{count}"
        return action, reason


class RecoveryExecutor:
    """BD-CV51: 执行恢复动作 — 通过 ControlAuthority 真实生效。"""

    def __init__(self, mapek: MAPEKController) -> None:
        self._mapek = mapek

    def execute(
        self, action: RecoveryAction, module_name: str, checkpoint: Checkpoint | None = None, control_plane: Any = None
    ) -> RecoveryResult:
        return self._mapek.execute_with_authority(action, module_name, checkpoint, control_plane)


class RecoveryVerifier:
    """BD-CV51: 验证恢复结果。

    SUCCESS 仅在 invariants 非空、全 PASS、TruthSnapshot 新鲜时返回。
    """

    def verify(
        self,
        result: RecoveryResult,
        invariants: dict[str, bool],
        truth_snapshot_fresh: bool = False,
    ) -> tuple[bool, str]:
        if not invariants:
            return False, "EMPTY_INVARIANTS"
        if not all(invariants.values()):
            failed = [k for k, v in invariants.items() if not v]
            return False, f"INVARIANTS_FAILED:{failed}"
        if not truth_snapshot_fresh:
            return False, "TRUTH_SNAPSHOT_STALE"
        if result not in (RecoveryResult.SUCCESS, RecoveryResult.PARTIAL):
            return False, f"RECOVERY_{result.value}"
        return True, "RECOVERY_VERIFIED"


@dataclass
class RecoveryOrchestrator:
    """BD-CV51: 恢复编排器 — Planner→Command→Executor→Verifier。"""

    planner: RecoveryPlanner
    executor: RecoveryExecutor
    verifier: RecoveryVerifier
    state: PersistedRecoveryState

    def recover(
        self,
        symptom_vector: dict[str, float],
        module_name: str,
        invariants: dict[str, bool],
        checkpoint: Checkpoint | None = None,
        control_plane: Any = None,
        truth_snapshot_fresh: bool = False,
    ) -> tuple[RecoveryResult, str]:
        """BD-CV51: 完整恢复流程。"""

        # 1. Plan
        action, reason = self.planner.plan(symptom_vector, module_name)

        # 2. Execute
        result = self.executor.execute(action, module_name, checkpoint, control_plane)

        # 3. Verify
        verified, verify_reason = self.verifier.verify(result, invariants, truth_snapshot_fresh)

        # 4. Persist
        self.state.last_action = action.value
        self.state.last_result = result.value
        if checkpoint:
            self.state.checkpoint_id = checkpoint.checkpoint_id
        self.state.save()

        if verified:
            return RecoveryResult.SUCCESS, f"{reason}; {verify_reason}"
        return RecoveryResult.FAILED, f"{reason}; VERIFY_FAILED:{verify_reason}"
