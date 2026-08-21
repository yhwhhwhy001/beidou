"""testnet 故障自愈后自动重新授权 RESUME 的回归测试（BD-FIX）。

背景: testnet 运行时故障（demo user stream 掉线等）触发 ``_fail_closed``
→ 撤销 ``_resume_authorized=False``；故障自愈后 ``_recover_if_validated``
的前提“仍有有效授权”已不成立 → 永不自动 RESUME → 控制面永久 NO_NEW_RISK。
demo 环境故障频发、人工授权不现实；live/canary 保持“必须人工授权”。

修复: 防抖器连续清洁（``debounce_action == "RUNNING"``）且无 blocker 时，
testnet 环境自动重新授权并补发 RESUME（复用 ``_recover_if_validated`` 的
RECOVERING→VALIDATING→ACTIVE 序列）；live/canary/paper 不进入该路径，
“撤销后必须人工/重启重新授权”的生产安全语义不变。

覆盖:
1. testnet + 清洁防抖 + 授权已撤销 → 经真实 _monitor 循环自动重新授权 RESUME。
2. live 同条件 → 不自动授权（保持 NO_NEW_RISK / DEGRADED）。
3. testnet 有 blocker → 不授权。
4. 致命降级（lifecycle LOCKED）→ 不授权（人工处置保留）。
5. 授权仍有效 + 外部手动 NO_NEW_RISK → 不覆盖外部下发。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlPlane
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor
from beidou_lifecycle.lifecycle import ModuleLifecycle, ModuleState


def _post_fail_closed_supervisor(
    tmp_path: Path,
    *,
    mode: str,
    lifecycle_state: ModuleState = ModuleState.DEGRADED,
    with_blocker: bool = False,
    self_heal: bool = True,
) -> tuple[BeidouSupervisor, ControlPlane, ModuleLifecycle]:
    """构造处于 ``_fail_closed`` 之后状态的监督器（授权已撤销、NO_NEW_RISK）。"""
    supervisor = BeidouSupervisor(
        project_root=tmp_path, mode=mode, symbols=["BTCUSDT"], port=19090, self_heal=self_heal
    )
    control = ControlPlane()  # 构造即 NO_NEW_RISK
    lifecycle = ModuleLifecycle("test")
    lifecycle.state = lifecycle_state
    supervisor.engine = SimpleNamespace(_control=control, _lifecycle=lifecycle)
    supervisor._resume_authorized = False
    if with_blocker:
        supervisor.report.checks = [
            CheckResult(
                "runtime.health.realtime_heartbeat",
                "实时循环心跳",
                CheckStatus.FAIL,
                CheckSeverity.P0,
                "stale",
            )
        ]
    return supervisor, control, lifecycle


def test_testnet_clean_debounce_auto_reauthorizes_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """testnet + 清洁防抖 + 授权已撤销 → 经真实 _monitor 循环自动重新授权 RESUME。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(tmp_path, mode="testnet")
    supervisor.monitor_interval = 0.01
    supervisor.report.supervisor_state = "DEGRADED"

    async def _noop() -> None:
        return None

    # 故障已自愈: 全部运行时检查清洁
    supervisor._runtime_checks = lambda: []  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor._record_g7_certification_evidence = lambda checks: None  # type: ignore[method-assign]
    supervisor._refresh_exchange_account_snapshot = _noop  # type: ignore[method-assign]
    supervisor._refresh_position_mode = _noop  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = _noop  # type: ignore[method-assign]

    sampled: dict[str, object] = {}

    async def _scenario() -> int:
        engine_task = asyncio.create_task(asyncio.sleep(0.5))
        supervisor._engine_task = engine_task
        try:
            monitor_task = asyncio.create_task(supervisor._monitor())
            # 防抖器达到 RUNNING（6+ 轮清洁）并完成自动重新授权后再采样。
            await asyncio.sleep(0.25)
            sampled["supervisor_state"] = supervisor.report.supervisor_state
            sampled["trading_ready"] = supervisor.report.trading_ready
            sampled["control_state"] = supervisor._control_state()
            sampled["authorized"] = supervisor._resume_authorized
            return await asyncio.wait_for(monitor_task, timeout=5.0)
        finally:
            engine_task.cancel()
            with suppress(asyncio.CancelledError):
                await engine_task

    assert asyncio.run(_scenario()) == 0
    # 采样点（循环运行中）即应处于自动恢复后的状态
    assert sampled["authorized"] is True
    assert sampled["control_state"] == "RESUME"
    assert sampled["supervisor_state"] == "RUNNING"
    assert sampled["trading_ready"] is True
    # 持久事实: 授权保留、lifecycle 回到 ACTIVE
    assert supervisor._resume_authorized is True
    assert lifecycle.state is ModuleState.ACTIVE
    assert "testnet auto re-authorized RESUME after clean recovery" in capsys.readouterr().out


def test_live_mode_keeps_manual_authorization_semantics(tmp_path: Path) -> None:
    """live 同条件 → 不自动授权，保持 NO_NEW_RISK / DEGRADED（生产语义不变）。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(tmp_path, mode="live")

    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._resume_authorized is False
    assert supervisor._control_state() == "NO_NEW_RISK"
    assert lifecycle.state is ModuleState.DEGRADED
    assert supervisor.report.supervisor_state != "RUNNING"


def test_blocker_present_prevents_auto_authorize(tmp_path: Path) -> None:
    """testnet 有 blocker → 不授权（清洁窗口必须同时满足无 blocker）。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(tmp_path, mode="testnet", with_blocker=True)

    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._resume_authorized is False
    assert supervisor._control_state() == "NO_NEW_RISK"
    assert lifecycle.state is ModuleState.DEGRADED


def test_critical_active_incident_prevents_auto_authorize(tmp_path: Path) -> None:
    """活动 CRITICAL 事故未关闭时，testnet 不能自动恢复 RESUME。"""
    supervisor, control, lifecycle = _post_fail_closed_supervisor(
        tmp_path, mode="testnet", lifecycle_state=ModuleState.DEGRADED
    )
    supervisor.engine._alerts = SimpleNamespace(
        get_active_incidents=lambda: [
            {
                "incident_id": "inc-reconciliation",
                "severity": "CRITICAL",
                "status": "DETECTED",
                "title": "Reconciliation blocked",
            }
        ]
    )

    assert supervisor._has_active_trading_incident() is True
    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._resume_authorized is False
    assert control.get_status().value == "NO_NEW_RISK"
    assert lifecycle.state is ModuleState.DEGRADED


def test_locked_lifecycle_never_auto_authorizes(tmp_path: Path) -> None:
    """致命降级（_fail_closed(fatal=True) → LOCKED）不自动恢复，必须人工处置。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(
        tmp_path, mode="testnet", lifecycle_state=ModuleState.LOCKED
    )

    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._resume_authorized is False
    assert lifecycle.state is ModuleState.LOCKED


def test_manual_no_new_risk_with_intact_authority_is_not_overridden(tmp_path: Path) -> None:
    """授权仍有效但外部 API 手动下发 NO_NEW_RISK: 自动恢复不得覆盖外部处置。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(tmp_path, mode="testnet")
    supervisor._resume_authorized = True  # 授权未撤销（无 _fail_closed）

    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._control_state() == "NO_NEW_RISK"
    assert lifecycle.state is ModuleState.DEGRADED


def test_no_self_heal_engine_side_resume_blocked_by_interlock(tmp_path: Path) -> None:
    """--no-self-heal 时引擎侧 EXEMPT-07 自动 RESUME 必须被 interlock 拦截（对抗审查反例 A）。

    引擎存在第三条自动 RESUME 路径（durable_ok 后直接 execute_action(RESUME)），
    其防线是 _install_resume_interlock 对授权状态的守卫 —— 该隐式耦合必须
    有测试固化，防止未来接线绕过。
    """
    from beidou_control.plane import ControlAction

    supervisor = BeidouSupervisor(
        project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090, self_heal=False
    )
    control = ControlPlane()
    lifecycle = ModuleLifecycle("test")
    supervisor.engine = SimpleNamespace(_control=control, _lifecycle=lifecycle)
    supervisor._resume_authorized = False
    supervisor._install_resume_interlock()

    # 引擎侧尝试自动 RESUME（EXEMPT-07 路径）→ 被改写为 NO_NEW_RISK
    control.execute_action(ControlAction.RESUME)
    assert supervisor._control_state() == "NO_NEW_RISK"

    # 授权有效时 RESUME 放行（对照：interlock 本身不破坏正常恢复）
    supervisor._resume_authorized = True
    control.execute_action(ControlAction.RESUME)
    assert supervisor._control_state() == "RESUME"


def test_no_self_heal_disables_testnet_auto_reauthorize(tmp_path: Path) -> None:
    """--no-self-heal 时 testnet 不得自动重新授权（显式关闭自愈优先于环境便利）。"""
    supervisor, _control, lifecycle = _post_fail_closed_supervisor(tmp_path, mode="testnet", self_heal=False)

    assert supervisor._maybe_testnet_auto_reauthorize() is False
    assert supervisor._resume_authorized is False
    assert supervisor._control_state() == "NO_NEW_RISK"
    assert lifecycle.state is ModuleState.DEGRADED
    assert supervisor.report.supervisor_state != "RUNNING"


def test_no_self_heal_disables_active_shortcut_resume(tmp_path: Path) -> None:
    """--no-self-heal 时 _recover_if_validated 的 ACTIVE 捷径不得补发 RESUME。"""
    supervisor, _control, _lifecycle = _post_fail_closed_supervisor(
        tmp_path, mode="testnet", lifecycle_state=ModuleState.ACTIVE, self_heal=False
    )
    supervisor._resume_authorized = True  # 授权未撤销（例如外部下发 NO_NEW_RISK）

    assert asyncio.run(supervisor._recover_if_validated([])) is False
    assert supervisor._control_state() == "NO_NEW_RISK"
    assert supervisor.report.supervisor_state != "RUNNING"


def test_self_heal_active_shortcut_resume_preserved(tmp_path: Path) -> None:
    """对照: self_heal=True 时 ACTIVE 捷径补发 RESUME 的既有行为保持不变。"""
    supervisor, _control, _lifecycle = _post_fail_closed_supervisor(
        tmp_path, mode="testnet", lifecycle_state=ModuleState.ACTIVE, self_heal=True
    )
    supervisor._resume_authorized = True

    assert asyncio.run(supervisor._recover_if_validated([])) is True
    assert supervisor._control_state() == "RESUME"
    assert supervisor.report.supervisor_state == "RUNNING"
