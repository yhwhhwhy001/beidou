"""G7 无人值守认证实时 SLI 追踪器。

在每个监控周期收集 SLI 样本，提供滑动窗口评估，
通过 /status 端点实时暴露 30 天认证进度。

与 beidou_certification/unattended.py 的 UnattendedCertification 互补：
- unattended.py: 离线窗口评估 + 证书签发
- g7_tracker.py: 运行时实时 SLI 追踪 + 趋势
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .models import CheckResult, CheckStatus

# 7 个 SLI 类别（与 unattended.py SLICategory 对齐）
SLI_NAMES = [
    "data_quality",
    "order_duplicates",
    "protection_slo",
    "reconciliation",
    "recovery_bounded",
    "incident_closure",
    "cost_and_pnl_reporting",
]

# 每个 SLI 的阈值（PASS 需要 >= threshold）
SLI_THRESHOLDS: dict[str, float] = {
    "data_quality": 0.99,
    "order_duplicates": 0.999,
    "protection_slo": 0.95,
    "reconciliation": 0.99,
    "recovery_bounded": 1.0,
    "incident_closure": 0.90,
    "cost_and_pnl_reporting": 0.99,
}


@dataclass
class SLISample:
    """单个 SLI 样本。"""

    sli_name: str
    value: float  # 1.0 = PASS, 0.0 = FAIL
    threshold: float
    observed_at: float = field(default_factory=time.time)
    # Wall-clock time is retained for audit display; expiry uses this
    # monotonic value so clock corrections cannot manufacture G7 elapsed time.
    observed_mono: float = field(default_factory=time.monotonic)

    @property
    def passed(self) -> bool:
        return self.value >= self.threshold


@dataclass
class SLIState:
    """单个 SLI 的滑动窗口状态。"""

    name: str
    threshold: float
    samples: deque[SLISample] = field(default_factory=deque)
    window_seconds: float = 86400.0  # 24h 窗口
    total_samples: int = 0
    total_passed: int = 0

    def record(self, value: float, *, now_wall: float | None = None, now_mono: float | None = None) -> None:
        wall = time.time() if now_wall is None else float(now_wall)
        mono = time.monotonic() if now_mono is None else float(now_mono)
        sample = SLISample(
            sli_name=self.name,
            value=value,
            threshold=self.threshold,
            observed_at=wall,
            observed_mono=mono,
        )
        self.samples.append(sample)
        self.total_samples += 1
        if sample.passed:
            self.total_passed += 1
        # 清理过期样本
        cutoff = mono - self.window_seconds
        while self.samples and self.samples[0].observed_mono < cutoff:
            self.samples.popleft()

    @property
    def window_pass_rate(self) -> float:
        if not self.samples:
            return 0.0
        passed = sum(1 for s in self.samples if s.passed)
        return passed / len(self.samples)

    @property
    def lifetime_pass_rate(self) -> float:
        if self.total_samples == 0:
            return 0.0
        return self.total_passed / self.total_samples

    @property
    def latest(self) -> SLISample | None:
        return self.samples[-1] if self.samples else None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "threshold": self.threshold,
            "window_samples": len(self.samples),
            "window_pass_rate": round(self.window_pass_rate, 4),
            "lifetime_pass_rate": round(self.lifetime_pass_rate, 4),
            "total_samples": self.total_samples,
            "total_passed": self.total_passed,
            "latest_status": "PASS" if (self.latest and self.latest.passed) else "FAIL",
            "latest_value": self.latest.value if self.latest else None,
        }


class G7LiveTracker:
    """G7 无人值守认证实时 SLI 追踪器。

    每个监控周期调用 feed() 更新 SLI，通过 summary() 获取
    当前 7 个 SLI 的实时状态，集成到 /status 端点。
    """

    def __init__(
        self,
        window_seconds: float = 86400.0,
        minimum_elapsed_seconds: float = 30 * 86400.0,
        minimum_cycles: int = 200,
    ) -> None:
        self._slis: dict[str, SLIState] = {
            name: SLIState(name=name, threshold=SLI_THRESHOLDS[name], window_seconds=window_seconds)
            for name in SLI_NAMES
        }
        self._cycle_count: int = 0
        self._started_at: float = time.monotonic()
        self._started_at_wall: float = time.time()
        self._last_feed_at: float = 0.0
        self._minimum_elapsed_seconds = minimum_elapsed_seconds
        self._minimum_cycles = minimum_cycles
        self._invalidated = False
        self._invalidated_reason = ""
        self._recovery_context_seen = False
        self._recovery_bounded = False
        # This tracker is diagnostic only.  Eligibility additionally requires
        # the durable certification producer to expose exactly one explicitly
        # started, schema-complete RUNNING window; in-process counters alone
        # can never certify an unattended period.
        self._durable_window_running = False

    def set_durable_window_state(self, *, running: bool, evidence_state_complete: bool) -> None:
        """Bind diagnostic eligibility to the durable G7 producer state."""

        self._durable_window_running = bool(running and evidence_state_complete)

    def feed(self, checks: list[CheckResult]) -> None:
        """从当前监控检查结果中提取 SLI 样本。"""
        self._cycle_count += 1
        wall = time.time()
        mono = time.monotonic()
        self._last_feed_at = wall
        if any(item.is_blocking and item.severity.value == "P0" for item in checks):
            self._invalidated = True
            self._invalidated_reason = "P0_BLOCKER"
        check_map = {c.check_id: c for c in checks}

        # 1. data_quality: market_data + algorithm_probe 状态
        market = check_map.get("runtime.health.market_data")
        probe = check_map.get("runtime.health.algorithm_probe")
        dq_ok = (market and market.status == CheckStatus.PASS) and (not probe or probe.status != CheckStatus.FAIL)
        self._slis["data_quality"].record(1.0 if dq_ok else 0.0, now_wall=wall, now_mono=mono)

        # 2. order_duplicates: order_trace 检查中是否有重复订单
        order_results = [c for c in checks if c.check_id == "runtime.execution.order_trace"]
        dup_free = bool(order_results) and all("DUP" not in c.message for c in order_results)
        self._slis["order_duplicates"].record(1.0 if dup_free else 0.0, now_wall=wall, now_mono=mono)

        # 3. protection_slo: protection_coverage 覆盖率
        protection_results = [c for c in checks if c.check_id == "runtime.safety.protection_coverage"]
        if protection_results:
            covered = sum(1 for c in protection_results if c.status == CheckStatus.PASS)
            rate = covered / len(protection_results)
        else:
            rate = 0.0  # 零样本不得证明保护 SLO
        self._slis["protection_slo"].record(rate, now_wall=wall, now_mono=mono)

        # 4. reconciliation: reconciliation 状态
        recon = check_map.get("runtime.safety.reconciliation")
        recon_ok = recon is not None and recon.status == CheckStatus.PASS
        self._slis["reconciliation"].record(1.0 if recon_ok else 0.0, now_wall=wall, now_mono=mono)

        # 5. recovery_bounded: no context is not a PASS.  The supervisor
        # must provide the restart budget result explicitly; otherwise G7
        # would certify a window whose recovery behavior was never observed.
        self._slis["recovery_bounded"].record(
            1.0 if self._recovery_context_seen and self._recovery_bounded else 0.0,
            now_wall=wall,
            now_mono=mono,
        )

        # 6. incident_closure: 直接取本周期 P0 阻断事实。
        inc_ok = not any(check.is_blocking and check.severity.value == "P0" for check in checks)
        self._slis["incident_closure"].record(1.0 if inc_ok else 0.0, now_wall=wall, now_mono=mono)

        # 7. cost_and_pnl_reporting: this is deliberately independent from
        # runtime error rate.  Until an authoritative cost/PnL evidence check
        # is produced by the ledger/reporting chain, the SLI is zero rather
        # than treating "no runtime errors" as financial evidence.
        cost_pnl = check_map.get("runtime.safety.cost_and_pnl_reporting")
        cost_pnl_ok = cost_pnl is not None and cost_pnl.status == CheckStatus.PASS
        self._slis["cost_and_pnl_reporting"].record(1.0 if cost_pnl_ok else 0.0, now_wall=wall, now_mono=mono)

    def feed_recovery_context(self, recovery_count: int, max_restarts: int) -> None:
        """补充 recovery_bounded SLI 上下文（由 supervisor 传入）。"""
        bounded = recovery_count <= max_restarts if max_restarts > 0 else recovery_count == 0
        self._recovery_context_seen = True
        self._recovery_bounded = bounded
        self._slis["recovery_bounded"].record(1.0 if bounded else 0.0)
        if not bounded:
            self._invalidated = True
            self._invalidated_reason = "RECOVERY_BUDGET_EXCEEDED"

    @property
    def overall_pass_rate(self) -> float:
        """7 个 SLI 窗口通过率的均值。"""
        rates = [s.window_pass_rate for s in self._slis.values()]
        return sum(rates) / len(rates) if rates else 0.0

    @property
    def all_slis_passing(self) -> bool:
        """G7 认证要求: 所有 SLI >= 各自的 threshold。"""
        return not self._invalidated and all(
            s.total_samples > 0 and s.window_pass_rate >= s.threshold for s in self._slis.values()
        )

    def summary(self) -> dict[str, Any]:
        """返回完整 SLI 摘要，供 /status 端点使用。"""
        sli_details = {name: self._slis[name].summary() for name in SLI_NAMES}
        elapsed = time.monotonic() - self._started_at
        return {
            "overall_pass_rate": round(self.overall_pass_rate, 4),
            "all_slis_passing": self.all_slis_passing,
            "cycles": self._cycle_count,
            "uptime_seconds": round(elapsed, 1),
            "minimum_elapsed_seconds": self._minimum_elapsed_seconds,
            "minimum_cycles": self._minimum_cycles,
            "invalidated": self._invalidated,
            "invalidated_reason": self._invalidated_reason,
            "g7_eligible": (
                self.all_slis_passing
                and self._durable_window_running
                and elapsed >= self._minimum_elapsed_seconds
                and self._cycle_count >= self._minimum_cycles
            ),
            "durable_window_running": self._durable_window_running,
            "slis": sli_details,
        }
