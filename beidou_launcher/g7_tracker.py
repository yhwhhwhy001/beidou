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
    "error_rate",
]

# 每个 SLI 的阈值（PASS 需要 >= threshold）
SLI_THRESHOLDS: dict[str, float] = {
    "data_quality": 0.99,
    "order_duplicates": 0.999,
    "protection_slo": 0.95,
    "reconciliation": 0.99,
    "recovery_bounded": 1.0,
    "incident_closure": 0.90,
    "error_rate": 0.99,
}


@dataclass
class SLISample:
    """单个 SLI 样本。"""

    sli_name: str
    value: float  # 1.0 = PASS, 0.0 = FAIL
    threshold: float
    observed_at: float = field(default_factory=time.time)

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

    def record(self, value: float) -> None:
        now = time.time()
        sample = SLISample(sli_name=self.name, value=value, threshold=self.threshold, observed_at=now)
        self.samples.append(sample)
        self.total_samples += 1
        if sample.passed:
            self.total_passed += 1
        # 清理过期样本
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0].observed_at < cutoff:
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
        self._started_at: float = time.time()
        self._last_feed_at: float = 0.0
        self._minimum_elapsed_seconds = minimum_elapsed_seconds
        self._minimum_cycles = minimum_cycles
        self._invalidated = False
        self._invalidated_reason = ""

    def feed(self, checks: list[CheckResult]) -> None:
        """从当前监控检查结果中提取 SLI 样本。"""
        self._cycle_count += 1
        self._last_feed_at = time.time()
        if any(item.is_blocking and item.severity.value == "P0" for item in checks):
            self._invalidated = True
            self._invalidated_reason = "P0_BLOCKER"
        check_map = {c.check_id: c for c in checks}

        # 1. data_quality: market_data + algorithm_probe 状态
        market = check_map.get("runtime.health.market_data")
        probe = check_map.get("runtime.health.algorithm_probe")
        dq_ok = (market and market.status == CheckStatus.PASS) and (not probe or probe.status != CheckStatus.FAIL)
        self._slis["data_quality"].record(1.0 if dq_ok else 0.0)

        # 2. order_duplicates: order_trace 检查中是否有重复订单
        order_results = [c for c in checks if c.check_id == "runtime.execution.order_trace"]
        dup_free = bool(order_results) and all("DUP" not in c.message for c in order_results)
        self._slis["order_duplicates"].record(1.0 if dup_free else 0.0)

        # 3. protection_slo: protection_coverage 覆盖率
        protection_results = [c for c in checks if c.check_id == "runtime.safety.protection_coverage"]
        if protection_results:
            covered = sum(1 for c in protection_results if c.status == CheckStatus.PASS)
            rate = covered / len(protection_results)
        else:
            rate = 0.0  # 零样本不得证明保护 SLO
        self._slis["protection_slo"].record(rate)

        # 4. reconciliation: reconciliation 状态
        recon = check_map.get("runtime.safety.reconciliation")
        recon_ok = recon is not None and recon.status == CheckStatus.PASS
        self._slis["reconciliation"].record(1.0 if recon_ok else 0.0)

        # 5. recovery_bounded: recovery 受限时记录
        # （由 supervisor._recovery_timestamps 长度 + max_restarts 判定，
        #  此处默认 PASS，由 supervisor 传入额外上下文）
        self._slis["recovery_bounded"].record(1.0)

        # 6. incident_closure: 活动事故计数
        incidents = check_map.get("runtime.health.incidents")
        inc_ok = incidents is not None and incidents.status == CheckStatus.PASS
        self._slis["incident_closure"].record(1.0 if inc_ok else 0.0)

        # 7. error_rate: 错误计数
        errors = check_map.get("runtime.health.errors")
        err_ok = errors is not None and errors.status == CheckStatus.PASS
        self._slis["error_rate"].record(1.0 if err_ok else 0.0)

    def feed_recovery_context(self, recovery_count: int, max_restarts: int) -> None:
        """补充 recovery_bounded SLI 上下文（由 supervisor 传入）。"""
        bounded = recovery_count <= max_restarts if max_restarts > 0 else recovery_count == 0
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
        elapsed = time.time() - self._started_at
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
                and elapsed >= self._minimum_elapsed_seconds
                and self._cycle_count >= self._minimum_cycles
            ),
            "slis": sli_details,
        }
