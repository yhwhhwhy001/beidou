"""回归测试(BD-FIX): 平稳期监控循环仍保持交易所快照刷新。

背景: M18-F01 稀疏轮优化把三路交易所快照刷新移进深度审计分支,
而 check_account_unknown 要求快照 age <= 45s。平稳期(无 blocker、
RESUME、无深度审计)600s 才刷新一次 → age 必然超限 → account P0
FAIL → trading_ready 周期性翻转(实测 ~50s 周期 FAIL 4-5s),
下单窗口被周期性关闭。

修复: 快照刷新独立于深度审计分支,每轮调用(各自内部节流)。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from beidou_control.plane import ControlAction, ControlPlane
from beidou_launcher.supervisor import BeidouSupervisor
from beidou_lifecycle.lifecycle import ModuleLifecycle, ModuleState


def test_quiescent_round_still_refreshes_snapshots(tmp_path: Path) -> None:
    """平稳期(RESUME/无 blocker/深度审计不到期)快照刷新仍被调用。"""
    supervisor = BeidouSupervisor(
        project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090
    )
    control = ControlPlane()
    control.execute_action(ControlAction.RESUME)
    lifecycle = ModuleLifecycle("test")
    lifecycle.state = ModuleState.ACTIVE
    supervisor.engine = SimpleNamespace(_control=control, _lifecycle=lifecycle)
    supervisor._resume_authorized = True
    supervisor.report.supervisor_state = "RUNNING"
    supervisor.monitor_interval = 0.01
    supervisor._runtime_checks = lambda: []  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor._record_g7_certification_evidence = lambda checks: None  # type: ignore[method-assign]
    # 强制稀疏轮: 深度审计永不到期
    supervisor._monitoring_scheduler = SimpleNamespace(  # type: ignore[assignment]
        should_run_deep_audit=lambda: False,
        tick=lambda *args, **kwargs: None,
    )

    calls: dict[str, int] = {"account": 0, "pos_mode": 0, "algo": 0}

    async def _counting(name: str) -> None:
        calls[name] += 1

    supervisor._refresh_exchange_account_snapshot = lambda: _counting("account")  # type: ignore[method-assign]
    supervisor._refresh_position_mode = lambda: _counting("pos_mode")  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = lambda: _counting("algo")  # type: ignore[method-assign]

    async def _scenario() -> int:
        engine_task = asyncio.create_task(asyncio.sleep(0.3))
        supervisor._engine_task = engine_task
        try:
            monitor_task = asyncio.create_task(supervisor._monitor())
            await asyncio.sleep(0.15)
            return await asyncio.wait_for(monitor_task, timeout=5.0)
        finally:
            engine_task.cancel()
            with suppress(asyncio.CancelledError):
                await engine_task

    assert asyncio.run(_scenario()) == 0
    assert calls["account"] > 0, "平稳期 account 快照必须持续刷新(45s 新鲜度约束)"
    assert calls["pos_mode"] > 0, "平稳期 position_mode 快照必须持续刷新"
    assert calls["algo"] > 0, "平稳期 algo 快照必须持续刷新"
