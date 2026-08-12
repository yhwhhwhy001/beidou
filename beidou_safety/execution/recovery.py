"""确定性恢复 — BD-10。

恢复流程: checkpoint → replay → exchange snapshot → reconcile → invariant validator → VALIDATING → ACTIVE。
差异未闭合时 NO_NEW_RISK/EXIT_ONLY；超时升级 P0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class RecoveryPhase(str, Enum):
    CHECKPOINT = "CHECKPOINT"
    REPLAY = "REPLAY"
    EXCHANGE_SNAPSHOT = "EXCHANGE_SNAPSHOT"
    RECONCILE = "RECONCILE"
    INVARIANT_CHECK = "INVARIANT_CHECK"
    VALIDATING = "VALIDATING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


@dataclass
class ReconciliationDiff:
    """对账差异。"""

    field: str
    local_value: str
    exchange_value: str
    severity: str  # P0 / P1 / WARNING
    resolved: bool = False
    resolved_at: datetime | None = None


@dataclass
class RecoveryState:
    """恢复状态。"""

    phase: RecoveryPhase = RecoveryPhase.CHECKPOINT
    checkpoint_id: str = ""
    events_replayed: int = 0
    diffs: list[ReconciliationDiff] = field(default_factory=list)
    invariants_valid: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.phase == RecoveryPhase.ACTIVE

    @property
    def has_blocking_diffs(self) -> bool:
        return any(d.severity == "P0" and not d.resolved for d in self.diffs)


class RecoveryEngine:
    """确定性恢复引擎。

    Recovery 期间:
    - 不允许新增风险 (NO_NEW_RISK)
    - 差异未闭合 → EXIT_ONLY
    - 超时 → P0 升级
    """

    RECOVERY_TIMEOUT_SECONDS = 300  # 5分钟

    def __init__(self) -> None:
        self._state = RecoveryState()

    def start_recovery(self, checkpoint_id: str) -> RecoveryState:
        if not checkpoint_id.strip():
            raise ValueError("Recovery requires a durable checkpoint id")
        self._state = RecoveryState(
            phase=RecoveryPhase.CHECKPOINT,
            checkpoint_id=checkpoint_id,
        )
        return self._state

    def advance(self, next_phase: RecoveryPhase) -> bool:
        transitions = {
            RecoveryPhase.CHECKPOINT: RecoveryPhase.REPLAY,
            RecoveryPhase.REPLAY: RecoveryPhase.EXCHANGE_SNAPSHOT,
            RecoveryPhase.EXCHANGE_SNAPSHOT: RecoveryPhase.RECONCILE,
            RecoveryPhase.RECONCILE: RecoveryPhase.INVARIANT_CHECK,
            RecoveryPhase.INVARIANT_CHECK: RecoveryPhase.VALIDATING,
            RecoveryPhase.VALIDATING: RecoveryPhase.ACTIVE,
        }
        expected = transitions.get(self._state.phase)
        if next_phase != expected:
            return False
        if next_phase is RecoveryPhase.ACTIVE and (not self._state.invariants_valid or self._state.has_blocking_diffs):
            return False
        self._state.phase = next_phase
        if next_phase is RecoveryPhase.ACTIVE:
            self._state.completed_at = datetime.now(timezone.utc)
        return True

    def can_accept_new_risk(self) -> bool:
        """差异已闭合且 invariant 验证通过才能接受新风险。"""
        return (
            self._state.phase == RecoveryPhase.ACTIVE
            and not self._state.has_blocking_diffs
            and self._state.invariants_valid
        )

    def is_timed_out(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self._state.started_at).total_seconds()
        return elapsed > self.RECOVERY_TIMEOUT_SECONDS and self._state.phase != RecoveryPhase.ACTIVE

    def fail(self, reason: str) -> None:
        self._state.phase = RecoveryPhase.FAILED
        self._state.completed_at = datetime.now(timezone.utc)
        self._state.diffs.append(
            ReconciliationDiff(
                field="recovery",
                local_value="FAILED",
                exchange_value=reason,
                severity="P0",
            )
        )
