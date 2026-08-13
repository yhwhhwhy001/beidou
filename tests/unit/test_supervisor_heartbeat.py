"""Supervisor 心跳与快照超时回归测试（BD-FIX: Main loop STALL 误报）。

背景: testnet 运行 24 分钟时出现 "Main loop STALL: 42.6s>30.0s" P0 blocker。
根因: ``_monitor`` 在 gather(三个网络快照) + checks 之后才更新
``_last_monitor_loop_ts``；demo-fapi 网络慢时心跳停更，被
``check_monitor_loop_health`` 误判为循环 STALL。循环自身健康（每次 sleep
醒来都继续执行），只是 IO 慢。

覆盖:
1. ``_refresh_snapshot_safe`` 快路径: 快速 coroutine 正常完成，副作用保留。
2. ``_refresh_snapshot_safe`` 超时路径: 慢 coroutine 在 timeout 后被取消且不抛异常。
3. ``_refresh_snapshot_safe`` 取消传播: 外部取消（监督器关停）不被吞掉。
4. ``check_monitor_loop_health`` STALL 语义不变: 42s 陈旧心跳仍 FAIL/P0。
5. ``_monitor`` 最小循环回归: 慢快照在途时心跳仍持续更新（修复后的时序）。
"""

from __future__ import annotations

import asyncio
import functools
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_launcher import supervisor as supervisor_module
from beidou_launcher.supervisor import BeidouSupervisor, _refresh_snapshot_safe
from beidou_observability.monitoring.checks.monitor_self import check_monitor_loop_health
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus


def test_refresh_snapshot_safe_fast_coroutine_completes() -> None:
    """快路径: 0.2s 内完成的 coroutine 正常返回并保留副作用。"""
    finished: list[str] = []

    async def fast() -> None:
        await asyncio.sleep(0.01)
        finished.append("done")

    asyncio.run(_refresh_snapshot_safe(fast(), timeout=1.0))
    assert finished == ["done"]


def test_refresh_snapshot_safe_times_out_slow_coroutine() -> None:
    """超时路径: sleep(60) 的 coroutine 在 timeout 后被取消，调用方不抛异常。"""

    async def slow() -> None:
        await asyncio.sleep(60.0)

    start = time.monotonic()
    asyncio.run(_refresh_snapshot_safe(slow(), timeout=0.1))
    assert time.monotonic() - start < 5.0


def test_refresh_snapshot_safe_propagates_outer_cancellation() -> None:
    """外部取消（监督器关停）必须传播，不能被宽捕获吞掉。"""

    async def slow() -> None:
        await asyncio.sleep(60.0)

    async def scenario() -> None:
        task = asyncio.create_task(_refresh_snapshot_safe(slow(), timeout=60.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_check_monitor_loop_health_stall_semantics_unchanged() -> None:
    """回归: 本次误报场景（42s 陈旧心跳, max_stall=30）必须仍判 FAIL/P0。"""
    now = time.monotonic()
    fresh = check_monitor_loop_health(now - 1.0, max_stall=30.0)
    assert fresh.status is CheckStatus.PASS
    stale = check_monitor_loop_health(now - 42.0, max_stall=30.0)
    assert stale.status is CheckStatus.FAIL
    assert stale.severity is CheckSeverity.P0


def test_monitor_loop_heartbeat_advances_while_snapshot_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归: 挂死的网络快照不再冻结循环心跳（Main loop STALL 误报场景）。

    三个快照全部挂死（sleep(60) 模拟 demo-fapi 慢请求）。若没有
    _refresh_snapshot_safe 的超时兜底，gather 会阻塞 60s，心跳在期间停更，
    check_monitor_loop_health 把网络等待误判为循环 STALL（本次事故根因）。
    修复后每轮被 wait_for(timeout=0.3) 取消，循环按 monitor_interval 持续
    醒来并更新心跳 → 采样期间心跳持续推进。
    """
    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19094)
    supervisor.monitor_interval = 0.05
    supervisor.engine = SimpleNamespace(
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME")),
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
    )

    async def hung_snapshot() -> None:
        await asyncio.sleep(60.0)  # 模拟挂死的网络快照

    async def recover_stub(checks: list[object]) -> bool:
        return False

    # 用短 timeout 实例化真实的 _refresh_snapshot_safe：挂死的快照每轮被
    # wait_for 取消，循环心跳按 monitor_interval 继续推进。
    monkeypatch.setattr(
        supervisor_module,
        "_refresh_snapshot_safe",
        functools.partial(_refresh_snapshot_safe, timeout=0.3),
    )
    monkeypatch.setattr(supervisor, "_refresh_exchange_account_snapshot", hung_snapshot)
    monkeypatch.setattr(supervisor, "_refresh_position_mode", hung_snapshot)
    monkeypatch.setattr(supervisor, "_refresh_exchange_algo_snapshot", hung_snapshot)
    monkeypatch.setattr(supervisor, "_runtime_checks", lambda: [])
    monkeypatch.setattr(supervisor, "_merge_monitoring_checks", lambda checks: checks)
    monkeypatch.setattr(supervisor, "_record_g7_certification_evidence", lambda checks: None)
    monkeypatch.setattr(supervisor, "_recover_if_validated", recover_stub)
    monkeypatch.setattr(supervisor, "_is_trading_ready", lambda: True)

    async def scenario() -> None:
        engine_task = asyncio.create_task(asyncio.sleep(5.0))
        supervisor._engine_task = engine_task
        monitor_task = asyncio.create_task(supervisor._monitor())
        samples: list[float] = []
        for _ in range(9):
            await asyncio.sleep(0.15)
            samples.append(supervisor._last_monitor_loop_ts)
        engine_task.cancel()
        try:
            await asyncio.wait_for(monitor_task, timeout=5.0)
        except asyncio.TimeoutError:
            monitor_task.cancel()

        # 心跳在慢快照（未完成）期间持续推进；修复前 gather 阻塞期间心跳冻结。
        assert samples[-1] > samples[0], (
            "挂死的网络快照必须由 wait_for 超时兜底；若心跳停滞（gather 阻塞），会重演 Main loop STALL 误报"
        )
        assert len(set(samples)) >= 2
        assert time.monotonic() - samples[-1] < 0.6

    asyncio.run(scenario())
