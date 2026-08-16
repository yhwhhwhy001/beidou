"""监督器 blocker 聚合与降级告警转移门控的回归测试（M00-F03）。

背景: 保护覆盖类检查按持仓逐条产出相同 (check_id, message) 的 P0 blocker
（例如 15 个持仓 MISSING_SL/MISSING_TP = 15 条重复条目）。旧逻辑每个
监督周期（5s）重跑 DEGRADED 分支：重复 _fail_closed 巨型理由串 + 重复
HIGH 告警 → 日志与 webhook 风暴（P0-19）。

修复:
1. summarize_blockers 把 (check_id, message) 相同条目聚合为 check_id(×N)，
   保留 entity_id 计数，输出短摘要。
2. 降级/告警只在新进入 DEGRADED/LOCKED 状态时执行一次；已在 DEGRADED
   期间保留静默 fail-closed 背压（控制面若被意外 RESUME 则拉回
   NO_NEW_RISK，不打印不告警）。
3. StartupReport.to_dict 增加非破坏性 blocker_summary 聚合字段。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction, ControlPlane
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from beidou_launcher.supervisor import BeidouSupervisor, summarize_blockers
from beidou_lifecycle.lifecycle import ModuleLifecycle


def _protection_blockers(count: int) -> list[CheckResult]:
    return [
        CheckResult(
            check_id="runtime.safety.protection_coverage",
            name="持仓保护覆盖 (PKG-MON-04)",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Protection: MISSING_SL, MISSING_TP",
            evidence={"entity_type": "position", "entity_id": f"SYM{i}USDT"},
        )
        for i in range(count)
    ]


def test_summarize_blockers_aggregates_duplicates() -> None:
    blockers = _protection_blockers(15)
    summary = summarize_blockers(blockers)
    assert "runtime.safety.protection_coverage(×15)" in summary
    assert "MISSING_SL, MISSING_TP" in summary
    assert len(summary) < 200  # 短摘要，而非 15 条拼接
    assert summary.count("protection_coverage(×15)") == 1


def test_summarize_blockers_separates_distinct_checks() -> None:
    blockers = [
        *_protection_blockers(2),
        CheckResult(
            check_id="runtime.safety.reconciliation",
            name="深度对账",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="MISMATCHED",
        ),
    ]
    summary = summarize_blockers(blockers)
    assert "protection_coverage(×2)" in summary
    assert "reconciliation" in summary


def test_summarize_blockers_empty() -> None:
    assert summarize_blockers([]) == "(none)"


def test_report_to_dict_includes_blocker_summary() -> None:
    report = StartupReport(mode="testnet", symbols=["BTCUSDT"], port=19090, commit="abc")
    report.replace_phase_checks("runtime.", _protection_blockers(15))
    payload = report.to_dict()
    assert len(payload["blockers"]) == 15  # 原始明细保留
    summary = payload["blocker_summary"]
    assert len(summary) == 1
    assert summary[0]["check_id"] == "runtime.safety.protection_coverage"
    assert summary[0]["count"] == 15
    assert len(summary[0]["entity_ids"]) == 15
    assert summary[0]["severity"] == "P0"


class _FakeAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_incident(self, *, severity, title, description, category: str = "runtime"):
        self.sent.append((str(severity), title, description))
        return SimpleNamespace()


def _supervisor_with_fake_engine(tmp_path: Path) -> tuple[BeidouSupervisor, _FakeAlerts, ControlPlane]:
    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090)
    control = ControlPlane()
    lifecycle = ModuleLifecycle("test")
    alerts = _FakeAlerts()
    supervisor.engine = SimpleNamespace(_control=control, _lifecycle=lifecycle, _alerts=alerts, _running=True)
    return supervisor, alerts, control


@pytest.mark.asyncio
async def test_degrade_alert_fires_once_on_transition_only(tmp_path: Path) -> None:
    """同一 DEGRADED 状态重复周期不得重复告警（转移门控）。"""
    supervisor, alerts, _control = _supervisor_with_fake_engine(tmp_path)
    blockers = _protection_blockers(15)
    await supervisor._apply_debounce_action("DEGRADED", blockers, has_persistent=True)
    await supervisor._apply_debounce_action("DEGRADED", blockers, has_persistent=True)
    await supervisor._apply_debounce_action("DEGRADED", blockers, has_persistent=True)
    assert len(alerts.sent) == 1
    assert supervisor.report.supervisor_state == "DEGRADED"
    # 告警文本为聚合摘要，非 15 条拼接
    assert "protection_coverage(×15)" in alerts.sent[0][2]


@pytest.mark.asyncio
async def test_degraded_cycle_reasserts_fail_closed_silently(tmp_path: Path) -> None:
    """已在 DEGRADED 期间控制面若被意外 RESUME，静默拉回 NO_NEW_RISK（不新增告警）。"""
    supervisor, alerts, control = _supervisor_with_fake_engine(tmp_path)
    blockers = _protection_blockers(1)
    await supervisor._apply_debounce_action("DEGRADED", blockers, has_persistent=True)
    assert supervisor._control_state() == "NO_NEW_RISK"

    # 模拟外部意外 RESUME（无 interlock 时理论上可达）
    control.execute_action(ControlAction.RESUME)
    assert supervisor._control_state() == "RESUME"

    await supervisor._apply_debounce_action("DEGRADED", blockers, has_persistent=True)
    assert supervisor._control_state() == "NO_NEW_RISK"  # 背压生效
    assert len(alerts.sent) == 1  # 无新增告警


@pytest.mark.asyncio
async def test_lock_transition_still_alerts_and_stays_silent_after(tmp_path: Path) -> None:
    supervisor, alerts, _control = _supervisor_with_fake_engine(tmp_path)
    blockers = _protection_blockers(3)
    await supervisor._apply_debounce_action("LOCKED", blockers, has_persistent=True)
    await supervisor._apply_debounce_action("LOCKED", blockers, has_persistent=True)
    assert len(alerts.sent) == 1
    assert supervisor.report.supervisor_state == "LOCKED"


@pytest.mark.asyncio
async def test_new_blocker_type_during_degraded_triggers_alert(tmp_path: Path) -> None:
    """对抗审查反例 C: DEGRADED 期间出现新类型 P0 必须告警（指纹变化检测）。"""
    supervisor, alerts, _control = _supervisor_with_fake_engine(tmp_path)
    initial = _protection_blockers(2)
    await supervisor._apply_debounce_action("DEGRADED", initial, has_persistent=True)
    assert len(alerts.sent) == 1
    # 相同指纹重复周期 → 无新增告警（不回归告警风暴）
    await supervisor._apply_debounce_action("DEGRADED", initial, has_persistent=True)
    assert len(alerts.sent) == 1
    # 新类型 blocker（对账 MISMATCHED）→ 指纹变化 → 立即告警
    new_blockers = [
        *initial,
        CheckResult(
            check_id="runtime.safety.reconciliation",
            name="深度对账",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="MISMATCHED",
        ),
    ]
    await supervisor._apply_debounce_action("DEGRADED", new_blockers, has_persistent=True)
    assert len(alerts.sent) == 2
    assert "reconciliation" in alerts.sent[-1][2]


def test_supervisor_debounce_window_supports_lock_after(tmp_path: Path) -> None:
    """对抗审查反例 D: 防抖窗口必须能容纳 lock_after 个样本，否则 LOCKED 死代码。"""
    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090)
    debounce = supervisor._health_debounce
    assert debounce.window_seconds >= debounce.lock_after * supervisor.monitor_interval
    # 模拟 lock_after 次连续持久阻断 → LOCKED 可达
    base = 1000.0
    result = None
    for i in range(debounce.lock_after):
        result = debounce.feed(True, now=base + i * supervisor.monitor_interval)
    assert result == "LOCKED"
