"""重启组场景二/三单元测试:ws_reconnect_verdict 判定纯函数(brief 单测 verbatim)、
database_restart 全流程(假时钟 + 假 PG/brew/引擎状态机驱动,禁止真实运维)、
user_stream_reconnect 探针全流程(假客户端 + 假 ws 探针 + 假引擎 user_stream
状态机)、dry_run NOT_VERIFIABLE 且不触碰任何真实资源、前置不健康
NOT_VERIFIABLE、PG 重连超时 FAIL、引擎自愈超时 FAIL、ws 判定三路径与异常
FAIL 自捕获。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.restart.database_restart import DatabaseRestartScenario
from beidou_certification.g5_scenarios.restart.process_restart import StatusUnreachableError
from beidou_certification.g5_scenarios.restart.user_stream_reconnect import (
    UserStreamReconnectScenario,
    ws_reconnect_verdict,
)
from beidou_certification.g5_scenarios.runner import RESTART_GROUP, SCENARIO_REGISTRY

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_ws_reconnect_verdict():
    assert ws_reconnect_verdict("HEALTHY", "HEALTHY", 123) == (True, "healthy_after_reconnect")
    assert ws_reconnect_verdict("RECONNECTING", "HEALTHY", 10) == (True, "reconnected")
    assert ws_reconnect_verdict("HEALTHY", "STALE", 999) == (False, "stale_after_reconnect")


# ---- 判定纯函数边界 ----


def test_ws_reconnect_verdict_edges() -> None:
    # after=CONNECTED 视为健康;before 处于过渡态 → reconnected
    assert ws_reconnect_verdict("RECONNECTING", "CONNECTED", 5) == (True, "reconnected")
    assert ws_reconnect_verdict("CONNECTING", "HEALTHY", 0) == (True, "reconnected")
    # 事件年龄超过引擎 testnet 豁免窗口(300s)即使状态 HEALTHY 也判停流
    assert ws_reconnect_verdict("HEALTHY", "HEALTHY", 999.0) == (False, "stale_after_reconnect")
    # after 非健康态一律 stale
    assert ws_reconnect_verdict("HEALTHY", "STOPPED", 10) == (False, "stale_after_reconnect")
    assert ws_reconnect_verdict("HEALTHY", "DEGRADED", 10) == (False, "stale_after_reconnect")
    assert ws_reconnect_verdict("HEALTHY", "", 10) == (False, "stale_after_reconnect")
    # before 非过渡态且引擎保持健康 → healthy_after_reconnect
    assert ws_reconnect_verdict("CONNECTED", "HEALTHY", 30) == (True, "healthy_after_reconnect")


# ---- 注册接线 ----


def test_registered() -> None:
    assert SCENARIO_REGISTRY["database_restart"] is DatabaseRestartScenario
    assert SCENARIO_REGISTRY["user_stream_reconnect"] is UserStreamReconnectScenario
    assert "database_restart" in RESTART_GROUP
    assert "user_stream_reconnect" in RESTART_GROUP


# ---- 假时钟 / 假运维状态机(全部运维动作注入,禁止真实执行) ----


class _FakeClock:
    """假时钟:now() 返回当前假时间,sleep() 直接推进假时间。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakePGConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeDBOps:
    """database_restart 假运维状态机:brew restart 后 pg_down_seconds 内 PG
    不可连且 /status state_backend_error 非空;之后 PG 恢复;再经过
    heal_delay_seconds 后 recon 回 MATCHED。"""

    def __init__(
        self,
        clock: _FakeClock,
        *,
        pid: int = 7777,
        no_engine: bool = False,
        pg_down_seconds: float = 30.0,
        heal_delay_seconds: float = 45.0,
        baseline_ready: bool = True,
        baseline_recon: str = "MATCHED",
        baseline_state_error: Any = None,
        baseline_unreachable: bool = False,
        pg_never: bool = False,
        engine_never_ready: bool = False,
        brew_raises: bool = False,
    ) -> None:
        self.clock = clock
        self.pid = pid
        self.no_engine = no_engine
        self.pg_down_seconds = pg_down_seconds
        self.heal_delay_seconds = heal_delay_seconds
        self.baseline_ready = baseline_ready
        self.baseline_recon = baseline_recon
        self.baseline_state_error = baseline_state_error
        self.baseline_unreachable = baseline_unreachable
        self.pg_never = pg_never
        self.engine_never_ready = engine_never_ready
        self.brew_raises = brew_raises
        self.restart_at: float | None = None
        self.brew_calls = 0

    def engine_pid(self) -> int | None:
        if self.no_engine:
            return None
        return self.pid

    def fetch_status(self) -> dict[str, Any]:
        if self.baseline_unreachable and self.restart_at is None:
            raise StatusUnreachableError("engine down")
        if self.restart_at is None:
            return {
                "trading_ready": self.baseline_ready,
                "last_reconciliation": {"status": self.baseline_recon},
                "state_backend_error": self.baseline_state_error,
            }
        elapsed = self.clock.now() - self.restart_at
        if elapsed < self.pg_down_seconds:
            return {
                "trading_ready": False,
                "last_reconciliation": {"status": "UNKNOWN"},
                "state_backend_error": "PG connection refused",
            }
        if self.engine_never_ready:
            return {"trading_ready": False, "last_reconciliation": {"status": "UNKNOWN"}, "state_backend_error": None}
        if elapsed < self.pg_down_seconds + self.heal_delay_seconds:
            return {"trading_ready": False, "last_reconciliation": {"status": "UNKNOWN"}, "state_backend_error": None}
        return {"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}, "state_backend_error": None}

    def pg_connect(self, dsn: str) -> _FakePGConn:
        if self.pg_never:
            raise ConnectionError("PG down forever")
        if self.restart_at is None or self.clock.now() - self.restart_at >= self.pg_down_seconds:
            return _FakePGConn()
        raise ConnectionError("PG down")

    def brew_restart(self) -> None:
        if self.brew_raises:
            raise RuntimeError("fake brew failure")
        self.brew_calls += 1
        self.restart_at = self.clock.now()


class _FakeWSRuntimeOps:
    """user_stream_reconnect 假引擎状态机:第 1 次 /status 为探针前状态,
    第 2 次为探针中状态,第 3 次为探针后状态(after_age_s 对应
    last_event_mono 距 now 的年龄)。"""

    def __init__(
        self,
        clock: _FakeClock,
        *,
        before_status: str = "HEALTHY",
        after_status: str = "HEALTHY",
        after_age_s: float = 10.0,
        before_unreachable: bool = False,
        after_unreachable: bool = False,
        missing_runtime: bool = False,
    ) -> None:
        self.clock = clock
        self.before_status = before_status
        self.after_status = after_status
        self.after_age_s = after_age_s
        self.before_unreachable = before_unreachable
        self.after_unreachable = after_unreachable
        self.missing_runtime = missing_runtime
        self.calls = 0

    def _payload(self, status: str, age_s: float) -> dict[str, Any]:
        if self.missing_runtime:
            return {"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}}
        return {
            "trading_ready": True,
            "last_reconciliation": {"status": "MATCHED"},
            "user_stream_runtime": {
                "status": status,
                "last_event_mono": self.clock.now() - age_s,
                "listen_key_active": True,
            },
        }

    def fetch_status(self) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            if self.before_unreachable:
                raise StatusUnreachableError("engine down")
            return self._payload(self.before_status, 0.0)
        if self.calls == 2:
            return self._payload(self.before_status, 0.0)
        if self.after_unreachable:
            raise StatusUnreachableError("engine down")
        return self._payload(self.after_status, self.after_age_s)


class _FakeProbeClient:
    """ctx.client 假驱动:create_listen_key 顺序吐出 keys;close_listen_key
    记录关闭动作(对应真实路径 DELETE /fapi/v1/listenKey)。"""

    def __init__(self, keys: list[str], *, create_raises: bool = False, close_raises: bool = False) -> None:
        self.keys = list(keys)
        self.created: list[str] = []
        self.closed: list[str] = []
        self.create_raises = create_raises
        self.close_raises = close_raises

    async def create_listen_key(self) -> str:
        """注入契约是直接返回 key 字符串(真实路径由场景内 impl 解包 Result)。"""
        if self.create_raises:
            raise RuntimeError("fake create failure")
        if not self.keys:
            raise RuntimeError("no keys left")
        key = self.keys.pop(0)
        self.created.append(key)
        return key

    async def close_listen_key(self, key: str) -> dict[str, Any]:
        if self.close_raises:
            return {"ok": False, "error": "fake close failure"}
        self.closed.append(key)
        return {"ok": True, "error": None}


class _FakeWSProbe:
    """假 ws 探针:按调用顺序返回预设结果,记录调用 key 与窗口时长。"""

    def __init__(self, results: list[dict[str, Any]], default: dict[str, Any] | None = None) -> None:
        self.results = list(results)
        self.default = default or {"connected": True, "frames": 2, "ack": True}
        self.calls: list[tuple[str, float]] = []

    async def probe(self, listen_key: str, window_s: float) -> dict[str, Any]:
        self.calls.append((listen_key, window_s))
        if self.results:
            return dict(self.results.pop(0))
        return dict(self.default)


def _ctx(tmp_path: Path, *, dry_run: bool = False, client: Any = None) -> ScenarioContext:
    return ScenarioContext(
        client=client,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


def _db_scenario(clock: _FakeClock, fake: _FakeDBOps) -> DatabaseRestartScenario:
    return DatabaseRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        fetch_status=fake.fetch_status,
        pg_connect=fake.pg_connect,
        brew_restart=fake.brew_restart,
        engine_pid=fake.engine_pid,
        poll_interval=5.0,
        pg_restore_deadline=120.0,
        ready_deadline=180.0,
    )


def _ws_scenario(
    clock: _FakeClock, engine: _FakeWSRuntimeOps, client: _FakeProbeClient, probe: _FakeWSProbe
) -> UserStreamReconnectScenario:
    return UserStreamReconnectScenario(
        now=clock.now,
        fetch_status=engine.fetch_status,
        create_listen_key=client.create_listen_key,
        close_listen_key=client.close_listen_key,
        ws_probe=probe.probe,
        probe_window=3.0,
    )


# ---- dry_run:NOT_VERIFIABLE 且不触碰任何真实资源 ----


def test_database_restart_dry_run_not_verifiable_no_real_ops(tmp_path: Path) -> None:
    def _forbidden_pid() -> int | None:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_status() -> dict[str, Any]:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_connect(dsn: str) -> Any:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_brew() -> None:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    scenario = DatabaseRestartScenario(
        engine_pid=_forbidden_pid,
        fetch_status=_forbidden_status,
        pg_connect=_forbidden_connect,
        brew_restart=_forbidden_brew,
    )
    ctx = _ctx(tmp_path, dry_run=True)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "RESTART_REQUIRES_REAL_EXECUTION"
    assert result.error_message == "restart requires real execution"
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


def test_user_stream_reconnect_dry_run_not_verifiable_no_real_ops(tmp_path: Path) -> None:
    def _forbidden_status() -> dict[str, Any]:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    async def _forbidden_create() -> str:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    async def _forbidden_close(key: str) -> dict[str, Any]:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    async def _forbidden_probe(key: str, window_s: float) -> dict[str, Any]:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    scenario = UserStreamReconnectScenario(
        fetch_status=_forbidden_status,
        create_listen_key=_forbidden_create,
        close_listen_key=_forbidden_close,
        ws_probe=_forbidden_probe,
    )
    ctx = _ctx(tmp_path, dry_run=True, client=None)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "RESTART_REQUIRES_REAL_EXECUTION"
    assert result.error_message == "restart requires real execution"
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


# ---- database_restart 全流程:brew restart → PG 恢复 → recon MATCHED → PASS ----


def test_database_restart_full_flow_recovered(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, pg_down_seconds=30.0, heal_delay_seconds=45.0)
    scenario = _db_scenario(clock, fake)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "database_restart_recovered"
    assert evidence["notional_usdt"] == 0.0
    assert evidence["pg_restart_elapsed_seconds"] == 30.0
    assert evidence["self_heal_elapsed_seconds"] == 45.0
    steps = evidence["steps"]

    baseline_pid = next(s for s in steps if s.get("action") == "baseline_pid")
    assert baseline_pid["pid"] == 7777
    baseline_status = next(s for s in steps if s.get("action") == "baseline_status")
    assert baseline_status["ready"] is True and baseline_status["reason"] == "READY"
    assert baseline_status["state_backend_error"] is None

    assert fake.brew_calls == 1 and fake.restart_at == t0
    brew_step = next(s for s in steps if s.get("action") == "brew_restart")
    assert brew_step["service"] == "postgresql@16"

    # 30s 后 PG 可连 + state_backend_error 清空 + 进程存活(第 7 次查询)
    pg_restored = next(s for s in steps if s.get("action") == "pg_restored")
    assert pg_restored["polls"] == 7 and pg_restored["engine_pid"] == 7777
    # 再 45s 后 recon 回 MATCHED(第 10 次查询)
    ready = next(s for s in steps if s.get("action") == "ready_observed")
    assert ready["polls"] == 10
    assert ready["status"]["trading_ready"] is True
    assert clock.t - t0 == 75.0

    summary = next(s for s in steps if s.get("action") == "post_restart_summary")
    assert summary["pg_restart_elapsed_seconds"] == 30.0
    assert summary["self_heal_elapsed_seconds"] == 45.0
    assert summary["trading_ready"] is True and summary["recon_status"] == "MATCHED"
    assert summary["state_backend_error"] is None and summary["engine_pid"] == 7777


# ---- database_restart 前置不健康 → NOT_VERIFIABLE,不执行 brew ----


def test_database_restart_engine_not_ready_before_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, baseline_ready=False, baseline_recon="MISMATCHED")
    scenario = _db_scenario(clock, fake)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_not_ready_before_restart"
    assert "MISMATCHED" in result.error_message
    assert fake.brew_calls == 0  # 未执行任何破坏性操作


def test_database_restart_state_backend_error_before_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, baseline_state_error="PG stale")
    scenario = _db_scenario(clock, fake)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "state_backend_error_before_restart"
    assert "PG stale" in result.error_message
    assert fake.brew_calls == 0


def test_database_restart_status_unreachable_before_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, baseline_unreachable=True)
    scenario = _db_scenario(clock, fake)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_not_ready_before_restart"
    assert "不可达" in result.error_message
    assert fake.brew_calls == 0


def test_database_restart_engine_process_not_found_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, no_engine=True)
    scenario = _db_scenario(clock, fake)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_process_not_found"
    assert fake.brew_calls == 0


# ---- database_restart PG 重连超时 → FAIL ----


def test_database_restart_pg_never_restored_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, pg_never=True)
    scenario = _db_scenario(clock, fake)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "PgRestartTimeoutError"
    assert "PG_RESTART_TIMEOUT" in result.error_message
    steps = result.evidence["steps"]
    assert fake.brew_calls == 1
    timeout = next(s for s in steps if s.get("action") == "pg_poll_timeout")
    assert timeout["deadline_seconds"] == 120.0
    # 轮询跑满 120s 窗口(5s 步进,第 25 次查询触发超时)
    assert clock.t - t0 == 120.0


# ---- database_restart 引擎自愈超时(PG 恢复但 recon 未回 MATCHED)→ FAIL ----


def test_database_restart_self_heal_timeout_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, engine_never_ready=True)  # PG 恢复但引擎永远不就绪
    scenario = _db_scenario(clock, fake)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "EngineRecoveryTimeoutError"
    assert "ENGINE_RECOVERY_TIMEOUT" in result.error_message
    steps = result.evidence["steps"]
    pg_restored = next(s for s in steps if s.get("action") == "pg_restored")
    assert pg_restored["polls"] == 7  # PG 已恢复,卡在 recon 恢复阶段
    timeout = next(s for s in steps if s.get("action") == "ready_poll_timeout")
    assert timeout["deadline_seconds"] == 180.0
    # 30s PG 恢复 + 180s ready 窗口跑满
    assert clock.t - t0 == 210.0


# ---- database_restart 异常自捕获 → FAIL,框架不崩溃 ----


def test_database_restart_brew_exception_self_captured_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeDBOps(clock, brew_raises=True)
    scenario = _db_scenario(clock, fake)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "fake brew failure" in result.error_message
    steps = result.evidence["steps"]
    # 前置基线已记录,但未到 brew_restart 步骤
    assert any(s.get("action") == "baseline_status" for s in steps)
    assert not any(s.get("action") == "brew_restart" for s in steps)


def test_database_restart_brew_timeout_self_captured_fail(tmp_path: Path) -> None:
    """brew 命令超时(真实路径受 120s timeout 约束,防全局锁无限挂起)→ FAIL,
    错误类型 TimeoutExpired 在证据中可见。"""

    def _brew_timeout() -> None:
        raise subprocess.TimeoutExpired(
            cmd=["/opt/homebrew/bin/brew", "services", "restart", "postgresql@16"], timeout=120
        )

    clock = _FakeClock()
    fake = _FakeDBOps(clock)
    scenario = DatabaseRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        fetch_status=fake.fetch_status,
        pg_connect=fake.pg_connect,
        brew_restart=_brew_timeout,
        engine_pid=fake.engine_pid,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "TimeoutExpired"
    assert "timed out after 120 seconds" in result.error_message
    assert result.evidence["steps"] != []



# ---- user_stream_reconnect 全流程:独立 listen key 探针 → 引擎不受影响 → PASS ----


def test_user_stream_reconnect_full_flow_pass(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, before_status="HEALTHY", after_status="HEALTHY", after_age_s=10.0)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([])  # 默认:两次连接都成功且有数据帧
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "healthy_after_reconnect"
    assert evidence["engine_user_stream_before"] == "HEALTHY"
    assert evidence["engine_user_stream_after"] == "HEALTHY"
    assert evidence["notional_usdt"] == 0.0
    steps = evidence["steps"]

    created = next(s for s in steps if s.get("action") == "listen_key_created")
    assert created["key"] == "prob...-1"  # 证据脱敏:仅保留前后片段,无完整凭据
    recreated = next(s for s in steps if s.get("action") == "listen_key_recreated")
    assert recreated["key"] == "prob...-2"
    assert "probe-key-1" not in str(steps) and "probe-key-2" not in str(steps)
    first = next(s for s in steps if s.get("action") == "ws_probe_first")
    assert first["connected"] is True
    second = next(s for s in steps if s.get("action") == "ws_probe_second")
    assert second["connected"] is True and second["frames"] == 2
    assert next(s for s in steps if s.get("action") == "reconnect_assert")["data_or_ack"] is True
    # 探针窗口 3s
    assert probe.calls == [("probe-key-1", 3.0), ("probe-key-2", 3.0)]
    assert client.created == ["probe-key-1", "probe-key-2"]
    assert client.closed == ["probe-key-1", "probe-key-2"]
    cleanup = next(s for s in steps if s.get("action") == "cleanup_close")
    assert cleanup["first"]["ok"] is True and cleanup["second"]["ok"] is True
    # 引擎观察:探针前后各一次 + 探针中一次
    assert engine.calls == 3
    assert next(s for s in steps if s.get("action") == "engine_user_stream_before")["status"] == "HEALTHY"
    assert next(s for s in steps if s.get("action") == "engine_user_stream_during")["status"] == "HEALTHY"
    assert next(s for s in steps if s.get("action") == "engine_user_stream_after")["status"] == "HEALTHY"
    verdict = next(s for s in steps if s.get("action") == "verdict")
    assert verdict["ok"] is True and verdict["reason"] == "healthy_after_reconnect"
    assert verdict["last_event_age_s"] == 10.0


# ---- user_stream_reconnect ws 判定三路径 ----

def test_user_stream_reconnect_engine_reconnecting_then_healthy_pass(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, before_status="RECONNECTING", after_status="HEALTHY", after_age_s=10.0)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["verdict"] == "reconnected"


def test_user_stream_reconnect_engine_stale_after_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, before_status="HEALTHY", after_status="STALE", after_age_s=999.0)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "STALE_AFTER_RECONNECT"
    assert result.error_message == "stale_after_reconnect"
    steps = result.evidence["steps"]
    assert next(s for s in steps if s.get("action") == "verdict")["ok"] is False


def test_user_stream_reconnect_stale_event_age_fail(tmp_path: Path) -> None:
    # after 状态 HEALTHY 但事件年龄超过 300s 豁免窗口 → 停流判 stale
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, before_status="HEALTHY", after_status="HEALTHY", after_age_s=999.0)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "STALE_AFTER_RECONNECT"


# ---- user_stream_reconnect 探针失败路径 → FAIL ----


def test_user_stream_reconnect_first_connect_failure_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([{"connected": False, "frames": 0, "ack": False, "window_seconds": 3.0}])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "首次" in result.error_message


def test_user_stream_reconnect_second_connect_no_data_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    # 第二次连接成功但窗口内无数据帧且无 ACK
    probe = _FakeWSProbe(
        [
            {"connected": True, "frames": 1, "ack": True, "window_seconds": 3.0},
            {"connected": True, "frames": 0, "ack": False, "window_seconds": 3.0},
        ]
    )
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "数据帧" in result.error_message


def test_user_stream_reconnect_create_listen_key_failure_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock)
    client = _FakeProbeClient(["probe-key-1"], create_raises=True)
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "fake create failure" in result.error_message
    assert probe.calls == []  # 建 key 失败后不触碰 ws


def test_user_stream_reconnect_client_unavailable_not_verifiable(tmp_path: Path) -> None:
    async def _dummy() -> None:
        raise AssertionError("CLIENT_UNAVAILABLE 时应先行返回,不得调用探针依赖")

    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock)
    probe = _FakeWSProbe([])
    scenario = UserStreamReconnectScenario(
        now=clock.now,
        fetch_status=engine.fetch_status,
        create_listen_key=_dummy,
        close_listen_key=_dummy,
        ws_probe=probe.probe,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=None)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "CLIENT_UNAVAILABLE"
    assert probe.calls == [] and engine.calls == 0


# ---- user_stream_reconnect 引擎观察异常路径 ----


def test_user_stream_reconnect_engine_unreachable_before_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, before_unreachable=True)
    client = _FakeProbeClient(["probe-key-1"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_status_unreachable_before_probe"
    assert client.created == [] and probe.calls == []  # 探针未开始


def test_user_stream_reconnect_engine_status_missing_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, missing_runtime=True)
    client = _FakeProbeClient(["probe-key-1"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_user_stream_status_missing"
    assert client.created == [] and probe.calls == []


def test_user_stream_reconnect_engine_unreachable_after_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock, after_unreachable=True)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"])
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "engine_status_unreachable_after_probe"
    # 探针完整执行过,清理也已完成
    assert len(probe.calls) == 2 and len(client.closed) == 2


# ---- user_stream_reconnect 清理非致命(删除失败仍 PASS,证据记录原因) ----


def test_user_stream_reconnect_cleanup_close_failure_still_pass(tmp_path: Path) -> None:
    clock = _FakeClock()
    engine = _FakeWSRuntimeOps(clock)
    client = _FakeProbeClient(["probe-key-1", "probe-key-2"], close_raises=True)
    probe = _FakeWSProbe([])
    scenario = _ws_scenario(clock, engine, client, probe)
    result = asyncio.run(scenario.run(_ctx(tmp_path, client=client)))
    assert result.status == ScenarioStatus.PASS
    steps = result.evidence["steps"]
    cleanup = next(s for s in steps if s.get("action") == "cleanup_close")
    assert cleanup["first"]["ok"] is False and cleanup["second"]["ok"] is False
    policy = next(s for s in steps if s.get("action") == "cleanup_keepalive_policy")
    assert policy["keepalive_issued"] is False  # 探针 key 从不续期,60 分钟 TTL 自然过期
