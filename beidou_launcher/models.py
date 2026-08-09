"""启动检查与监督证据模型。"""

from __future__ import annotations

from collections import deque
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
        # An empty check list during PREFLIGHT/ENGINE_STARTING is not a
        # successful run.  ``passed`` is a certificate field, so it requires
        # the same live authority conditions as readiness rather than merely
        # absence of currently collected blockers.
        return self.trading_ready and self.supervisor_state == "RUNNING" and not self.blockers

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


@dataclass
class HealthDebounce:
    """健康状态防抖器 — 滑动窗口消除瞬时抖动。

    问题背景:
    - 瞬时 PASS→FAIL→PASS 抖动（如启动阶段 market_data 数据积累、
      algorithm_probe 重试、对账瞬时 MATCHED→MISMATCHED→MATCHED）
      直接触发 DEGRADED/LOCKED 状态切换，导致误报。
    - EnvironmentGuard 是一次性启动门禁，运行时健康评估需要时间窗口。

    防抖策略:
    - 连续 degrade_after 次持久阻断 → DEGRADED
    - 连续 lock_after 次持久阻断 → LOCKED
    - 连续 recover_after 次干净 → 恢复为 RUNNING
    - 未达到阈值 → 保持当前状态不变

    使用方式:
        debounce = HealthDebounce()
        for has_blocker in check_results:
            new_state = debounce.feed(has_blocker)
            if new_state:
                supervisor.state = new_state
    """

    window: deque[tuple[float, bool]] = field(default_factory=deque)
    window_seconds: float = 60.0
    degrade_after: int = 3
    lock_after: int = 5
    recover_after: int = 3

    def feed(self, has_persistent_blocker: bool, now: float | None = None) -> str | None:
        """记录一次检查结果，返回建议状态变更或 None（保持不变）。

        Returns:
            "DEGRADED" | "LOCKED" | "RUNNING" | None
        """
        import time

        ts = now if now is not None else time.monotonic()
        self.window.append((ts, has_persistent_blocker))

        # 清理过期样本
        cutoff = ts - self.window_seconds
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()

        recent = [blocked for _, blocked in self.window]

        # 必须有一定样本量才判定（避免启动初期单次 FAIL 触发降级）
        if len(recent) < self.degrade_after:
            return None

        # LOCKED: 连续 lock_after 次全部持久阻断
        if len(recent) >= self.lock_after and all(recent[-self.lock_after :]):
            return "LOCKED"

        # DEGRADED: 连续 degrade_after 次全部持久阻断
        if all(recent[-self.degrade_after :]):
            return "DEGRADED"

        # 恢复: 连续 recover_after 次全部干净
        if len(recent) >= self.recover_after and not any(recent[-self.recover_after :]):
            return "RUNNING"

        return None  # 保持当前状态

    def reset(self) -> None:
        """重置防抖窗口（系统状态变更时调用）。"""
        self.window.clear()
