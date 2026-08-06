"""Data models for the Beidou one-click launcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class CheckStatus(str, Enum):
    """Normalized result of a startup or runtime check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One evidence-backed check result."""

    code: str
    subject: str
    status: CheckStatus
    message: str
    critical: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "status": self.status.value,
            "message": self.message,
            "critical": self.critical,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class CheckReport:
    """Collection of checks for one launcher phase."""

    phase: str
    mode: str
    results: list[CheckResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""

    @property
    def passed(self) -> bool:
        return not any(result.critical and result.status == CheckStatus.FAIL for result in self.results)

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.results if result.status == CheckStatus.FAIL)

    @property
    def warning_count(self) -> int:
        return sum(1 for result in self.results if result.status == CheckStatus.WARN)

    def finish(self) -> CheckReport:
        self.finished_at = datetime.now(timezone.utc).isoformat()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "mode": self.mode,
            "passed": self.passed,
            "failure_count": self.failure_count,
            "warning_count": self.warning_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "results": [result.to_dict() for result in self.results],
        }
