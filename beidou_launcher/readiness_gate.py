"""BD-CV53: 启动 Readiness Truth Gate。

启动阶段: CONFIG→DEPENDENCY→MODULE→FACT REBUILD→RECONCILIATION→PROTECTION→RISK→READINESS。
进程启动完成默认 NO_NEW_RISK。
readiness 与 liveness 分离。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class StartupPhase(str, Enum):
    CONFIG = "CONFIG"
    DEPENDENCY = "DEPENDENCY"
    MODULE_START = "MODULE_START"
    FACT_REBUILD = "FACT_REBUILD"
    RECONCILIATION = "RECONCILIATION"
    PROTECTION_VERIFY = "PROTECTION_VERIFY"
    RISK_SNAPSHOT = "RISK_SNAPSHOT"
    READINESS = "READINESS"


STARTUP_ORDER = list(StartupPhase)


@dataclass
class PhaseResult:
    phase: StartupPhase
    passed: bool = False
    details: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0


@dataclass
class ReadinessGate:
    """BD-CV53: 启动就绪门禁。

    进程启动完成默认 NO_NEW_RISK。
    只有 TruthSnapshot + Gate 满足才可 ELIGIBLE。
    readiness 与 liveness 分离。
    """

    phases: dict[StartupPhase, PhaseResult] = field(default_factory=dict)
    _current_phase_idx: int = 0
    _started_at: float = 0.0

    def start(self) -> None:
        self._started_at = time.monotonic()
        for phase in STARTUP_ORDER:
            self.phases[phase] = PhaseResult(phase=phase)

    def complete_phase(self, phase: StartupPhase, passed: bool, details: str = "") -> bool:
        now = time.monotonic()
        result = self.phases.get(phase)
        if result is None:
            return False
        result.passed = passed
        result.details = details
        result.completed_at = now
        if passed:
            idx = STARTUP_ORDER.index(phase)
            self._current_phase_idx = max(self._current_phase_idx, idx + 1)
        return passed

    def current_phase(self) -> StartupPhase:
        idx = min(self._current_phase_idx, len(STARTUP_ORDER) - 1)
        return STARTUP_ORDER[idx]

    def is_ready(self) -> bool:
        """BD-CV53 AC-53-01: 进程 alive 但关键模块 UNKNOWN → readiness FAIL。"""
        critical_phases = [
            StartupPhase.RECONCILIATION,
            StartupPhase.PROTECTION_VERIFY,
            StartupPhase.RISK_SNAPSHOT,
        ]
        return all(self.phases.get(p, PhaseResult(phase=p)).passed for p in critical_phases)

    def can_resume(self) -> bool:
        """BD-CV53 AC-53-02: 启动失败不会自动 RESUME。"""
        if not self.is_ready():
            return False
        # 所有关键阶段必须通过
        for phase in STARTUP_ORDER:
            result = self.phases.get(phase)
            if result is None or not result.passed:
                return False
        return True

    def elapsed_seconds(self) -> float:
        if self._started_at == 0.0:
            return 0.0
        return time.monotonic() - self._started_at
