"""模块生命周期状态机。PROVISIONING→...→ACTIVE/DEGRADED/FAILED/LOCKED。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import CorrelationId, ResultStatus


class ModuleState(str, Enum):
    PROVISIONING = "PROVISIONING"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    WARMING = "WARMING"
    VALIDATING = "VALIDATING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"
    LOCKED = "LOCKED"


class DegradationLevel(str, Enum):
    ACTIVE = "ACTIVE"
    NO_NEW_RISK = "NO_NEW_RISK"
    EXIT_ONLY = "EXIT_ONLY"
    LOCKED = "LOCKED"


DEGRADATION_PRIORITY = {
    DegradationLevel.ACTIVE: 0,
    DegradationLevel.NO_NEW_RISK: 1,
    DegradationLevel.EXIT_ONLY: 2,
    DegradationLevel.LOCKED: 3,
}

DEGRADATION_FROM_STATE: dict[ModuleState, DegradationLevel] = {
    ModuleState.ACTIVE: DegradationLevel.ACTIVE,
    ModuleState.DEGRADED: DegradationLevel.NO_NEW_RISK,
    ModuleState.QUARANTINED: DegradationLevel.EXIT_ONLY,
    ModuleState.LOCKED: DegradationLevel.LOCKED,
    ModuleState.FAILED: DegradationLevel.LOCKED,
}

VALID_TRANSITIONS: dict[ModuleState, set[ModuleState]] = {
    ModuleState.PROVISIONING: {ModuleState.BOOTSTRAPPING, ModuleState.FAILED},
    ModuleState.BOOTSTRAPPING: {ModuleState.WARMING, ModuleState.FAILED},
    ModuleState.WARMING: {ModuleState.VALIDATING, ModuleState.FAILED},
    ModuleState.VALIDATING: {ModuleState.ACTIVE, ModuleState.DEGRADED, ModuleState.FAILED},
    ModuleState.ACTIVE: {ModuleState.DEGRADED, ModuleState.QUARANTINED, ModuleState.LOCKED},
    ModuleState.DEGRADED: {ModuleState.ACTIVE, ModuleState.QUARANTINED, ModuleState.RECOVERING, ModuleState.LOCKED},
    ModuleState.QUARANTINED: {ModuleState.RECOVERING, ModuleState.LOCKED, ModuleState.FAILED},
    ModuleState.RECOVERING: {ModuleState.VALIDATING, ModuleState.FAILED},
    ModuleState.FAILED: {ModuleState.PROVISIONING},
    ModuleState.LOCKED: set(),
}


@dataclass(frozen=True, slots=True)
class HealthEvidence:
    module_name: str
    state: ModuleState
    dependencies_healthy: dict[str, bool]
    data_freshness_seconds: dict[str, float]
    checkpoint_lag: int
    invariants_valid: bool
    schema_version: str
    leadership_status: str
    active_incidents: list[str] = field(default_factory=list)
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_healthy(self) -> bool:
        if not self.invariants_valid:
            return False
        if self.state != ModuleState.ACTIVE:
            return False
        if not all(self.dependencies_healthy.values()):
            return False
        return not self.active_incidents


class ModuleLifecycle:
    """模块生命周期管理器。恢复后必须经过验证，不得直接 ACTIVE。"""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.state = ModuleState.PROVISIONING
        self._evidence_history: list[HealthEvidence] = []

    def transition(self, target: ModuleState) -> ResultStatus:
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            return ResultStatus.ERROR
        self.state = target
        return ResultStatus.SUCCESS

    def get_degradation_level(self) -> DegradationLevel:
        return DEGRADATION_FROM_STATE.get(self.state, DegradationLevel.LOCKED)

    def resolve_degradation(self, levels: list[DegradationLevel]) -> DegradationLevel:
        if not levels:
            return DegradationLevel.ACTIVE
        return max(levels, key=lambda l: DEGRADATION_PRIORITY.get(l, 99))

    def record_evidence(self, evidence: HealthEvidence) -> None:
        self._evidence_history.append(evidence)

    def latest_evidence(self) -> HealthEvidence | None:
        return self._evidence_history[-1] if self._evidence_history else None

    def should_restart_directly_to_active(self) -> bool:
        """禁止重启后直接 ACTIVE。必须先 VALIDATING。"""
        return False
