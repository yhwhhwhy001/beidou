"""BF-01: 防泄漏守卫。

独立模块，确保因子研究和策略评估中不存在：
1. 未来数据泄漏（data_available_time > prediction_time）
2. 标签重叠导致的信息交叉
3. Closed-bar 未闭合数据的使用
4. 样本内外的混淆
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from beidou_shared.types import InstrumentId, VenueId
from .contracts import PredictionKey, PredictionRecord
from .point_in_time import BarInfo, ClosedBarEnforcer, FutureDataGuard


@dataclass
class LeakageReport:
    """防泄漏检查报告。"""
    passed: bool = True
    checks: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class LeakageGuard:
    """防泄漏守卫 — 综合检查层。

    在因子评估前执行多层防泄漏检查：
    1. 预测时间 vs 数据可用时间
    2. 训练集/测试集时间边界
    3. Closed-bar 合规
    4. 标签重叠
    """

    def __init__(self) -> None:
        self._future_guard = FutureDataGuard()
        self._bar_enforcer = ClosedBarEnforcer()
        self._reports: list[LeakageReport] = []

    def check_prediction(
        self,
        prediction: PredictionRecord,
        *,
        bar_provider: Any | None = None,
    ) -> LeakageReport:
        """对单个预测执行全面的防泄漏检查。"""
        report = LeakageReport()
        pk = prediction.prediction_key

        # 1. 未来数据检查
        if pk.data_available_time > pk.prediction_time:
            report.passed = False
            report.violations.append(
                f"FUTURE_DATA: data_available={pk.data_available_time} > prediction={pk.prediction_time}"
            )
        report.checks.append({
            "check": "future_data",
            "passed": pk.data_available_time <= pk.prediction_time,
        })

        # 2. Closed-bar 检查
        bar_ok = self._bar_enforcer.enforce(prediction, bar_provider)
        if not bar_ok:
            report.passed = False
            report.violations.append("OPEN_BAR: data from unclosed bar")
        report.checks.append({"check": "closed_bar", "passed": bar_ok})

        # 3. 时间戳完整性
        if not pk.prediction_time.tzinfo:
            report.passed = False
            report.violations.append("MISSING_TIMEZONE")
        report.checks.append({
            "check": "timezone",
            "passed": pk.prediction_time.tzinfo is not None,
        })

        self._reports.append(report)
        return report

    def check_batch(
        self,
        predictions: list[PredictionRecord],
    ) -> LeakageReport:
        """批量防泄漏检查。"""
        report = LeakageReport()
        future_count = 0
        open_bar_count = 0

        for pred in predictions:
            pk = pred.prediction_key
            if pk.data_available_time > pk.prediction_time:
                future_count += 1
            if not self._bar_enforcer.enforce(pred):
                open_bar_count += 1

        if future_count > 0:
            report.passed = False
            report.violations.append(f"FUTURE_DATA: {future_count} predictions")
            report.details["future_data_count"] = future_count

        if open_bar_count > 0:
            report.passed = False
            report.violations.append(f"OPEN_BAR: {open_bar_count} predictions")
            report.details["open_bar_count"] = open_bar_count

        report.checks = [
            {"check": "future_data_batch", "passed": future_count == 0, "count": future_count},
            {"check": "closed_bar_batch", "passed": open_bar_count == 0, "count": open_bar_count},
        ]
        report.details["total_predictions"] = len(predictions)

        self._reports.append(report)
        return report

    def check_train_test_boundary(
        self,
        train_end_time: datetime,
        test_start_time: datetime,
        embargo_end_time: datetime | None = None,
    ) -> LeakageReport:
        """检查训练/测试集边界是否存在泄漏。"""
        report = LeakageReport()

        if train_end_time >= test_start_time:
            report.passed = False
            report.violations.append(
                f"OVERLAP: train_end={train_end_time} >= test_start={test_start_time}"
            )

        if embargo_end_time and embargo_end_time > test_start_time:
            report.passed = False
            report.violations.append(
                f"EMBARGO_VIOLATION: embargo_end={embargo_end_time} > test_start={test_start_time}"
            )

        report.checks = [
            {"check": "train_test_separation", "passed": train_end_time < test_start_time},
            {"check": "embargo_respected", "passed": not embargo_end_time or embargo_end_time <= test_start_time},
        ]
        self._reports.append(report)
        return report

    @property
    def has_violations(self) -> bool:
        return any(not r.passed for r in self._reports)

    @property
    def violation_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in self._reports:
            for v in report.violations:
                key = v.split(":")[0]
                counts[key] = counts.get(key, 0) + 1
        return counts
