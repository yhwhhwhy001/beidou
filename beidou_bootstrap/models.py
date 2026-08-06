"""启动检查与监督证据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class CheckSeverity(str, Enum):
    INFO = "INFO"
    P2 = "P2"
    P1 = "P1"
    P0 = "P0"


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    name: str
    status: CheckStatus
    severity: CheckSeverity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_blocking(self) -> bool:
        return self.status == CheckStatus.FAIL and self.severity in {CheckSeverity.P0, CheckSeverity.P1}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["severity"] = self.severity.value
        data["is_blocking"] = self.is_blocking
        return data


@dataclass(slots=True)
class StartupReport:
    mode: str
    symbols: list[str]
    port: int
    commit: str
    checks: list[CheckResult] = field(default_factory=list)
    phase: str = "PREFLIGHT"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    trading_ready: bool = False
    supervisor_state: str = "STARTING"

    @property
    def blockers(self) -> list[CheckResult]:
        return [item for item in self.checks if item.is_blocking]

    @property
    def passed(self) -> bool:
        return not self.blockers

    def replace_phase_checks(self, phase_prefix: str, checks: list[CheckResult]) -> None:
        self.checks = [item for item in self.checks if not item.check_id.startswith(phase_prefix)] + checks
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "symbols": self.symbols,
            "port": self.port,
            "commit": self.commit,
            "phase": self.phase,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "trading_ready": self.trading_ready,
            "supervisor_state": self.supervisor_state,
            "passed": self.passed,
            "blockers": [item.to_dict() for item in self.blockers],
            "checks": [item.to_dict() for item in self.checks],
        }
