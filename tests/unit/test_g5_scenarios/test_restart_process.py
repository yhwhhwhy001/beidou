"""重启组场景一单元测试:parse_engine_status 判定纯函数(brief 单测 verbatim)、
场景全流程(假时钟 + 假引擎状态机驱动:pgrep/kill/kickstart/status/PG 全部
注入,禁止真实运维动作)、dry_run NOT_VERIFIABLE 且不触碰任何真实资源、前置
未就绪 NOT_VERIFIABLE、恢复超时 FAIL、durable 漂移 FAIL 与异常自捕获路径。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.restart.process_restart import (
    EnginePidAmbiguousError,
    ProcessRestartScenario,
    StatusUnreachableError,
    engine_pid_os,
    parse_engine_status,
)
from beidou_certification.g5_scenarios.runner import RESTART_GROUP, SCENARIO_REGISTRY

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_parse_engine_status():
    ok, _ = parse_engine_status({"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}})
    assert ok is True
    ok, reason = parse_engine_status({"trading_ready": False, "last_reconciliation": {"status": "MISMATCHED"}})
    assert ok is False and "MISMATCHED" in reason


def test_parse_engine_status_missing_fields():
    ok, reason = parse_engine_status({})
    assert ok is False and reason == "MALFORMED"


# ---- 判定纯函数边界 ----


def test_parse_engine_status_edges() -> None:
    # trading_ready=True 但 recon 未 MATCHED → 未就绪,reason 带 recon 状态
    ok, reason = parse_engine_status({"trading_ready": True, "last_reconciliation": {"status": "UNKNOWN"}})
    assert ok is False and reason == "RECON_UNKNOWN"
    # trading_ready=False 且 recon=MATCHED → 引擎其它原因未就绪
    ok, reason = parse_engine_status({"trading_ready": False, "last_reconciliation": {"status": "MATCHED"}})
    assert ok is False and reason == "ENGINE_NOT_READY"
    # 字段缺失/类型错误 → MALFORMED
    assert parse_engine_status({"trading_ready": True}) == (False, "MALFORMED")
    assert parse_engine_status({"last_reconciliation": {"status": "MATCHED"}}) == (False, "MALFORMED")
    assert parse_engine_status({"trading_ready": True, "last_reconciliation": {}}) == (False, "MALFORMED")
    assert parse_engine_status({"trading_ready": True, "last_reconciliation": "nope"}) == (False, "MALFORMED")


# ---- 注册接线 ----

_ORIGINAL_PAYLOAD: dict[str, Any] = {
    "projection_id": "operator-rebaseline-2026-08-17-v1",
    "account_id": "default",
    "venue_id": "BINANCE",
    "balance_amount": "10545.04904025",
    "balance_currency": "USDT",
    "balance_decimals": 8,
    "positions": {},
    "open_orders": [],
    "captured_at": "2026-08-16T19:38:23.850834+00:00",
    "source": "OPERATOR_REBASELINE",
    "fact_version": "operator-rebaseline-2026-08-17-v1",
    "evidence_hash": "684ed7a3a5063c494253e09b9def8ae6f2b92b4caa5a6441f77c702b35553b75",
    "approval_id": "operator-manual-2026-08-17-v1",
    "complete": True,
    "created_at": "2026-08-16T19:38:23.850834+00:00",
}

_CORRUPTED_PAYLOAD: dict[str, Any] = dict(_ORIGINAL_PAYLOAD, balance_amount="1")


def test_registered() -> None:
    assert SCENARIO_REGISTRY["process_restart"] is ProcessRestartScenario
    assert "process_restart" in RESTART_GROUP


# ---- 假时钟 / 假引擎状态机(全部运维动作注入,禁止真实执行) ----


class _FakeClock:
    """假时钟:now() 返回当前假时间,sleep() 直接推进假时间。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeCursor:
    def __init__(self, conn: "_FakePG", sql: str) -> None:
        self._conn = conn
        self._sql = sql

    def fetchone(self) -> Any:
        return self._conn._fetchone(self._sql)

    def fetchall(self) -> list[Any]:
        return self._conn._fetchall(self._sql)


class _FakePG:
    """内存版 durable 表:opening 基线 payload 与 UNKNOWN outbox message_id 集。

    kill 前后快照可分别注入(baseline_after/corrupted、unknown_after 多出行),
    驱动 durable 校验路径。
    """

    def __init__(
        self,
        fake: "_FakeEngineOps",
        *,
        baseline: dict[str, Any] | None = _ORIGINAL_PAYLOAD,
        baseline_after: dict[str, Any] | None = None,
        unknown_before: set[str] | None = None,
        unknown_after: set[str] | None = None,
    ) -> None:
        self.fake = fake
        self._baseline = baseline
        self._baseline_after = baseline if baseline_after is None else baseline_after
        self._unknown_before = set(unknown_before or set())
        self._unknown_after = self._unknown_before if unknown_after is None else set(unknown_after)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        return _FakeCursor(self, " ".join(sql.split()))

    def _payload(self) -> dict[str, Any] | None:
        return self._baseline if self.fake.killed_at is None else self._baseline_after

    def _unknown_ids(self) -> set[str]:
        return self._unknown_before if self.fake.killed_at is None else self._unknown_after

    def _fetchone(self, sql: str) -> Any:
        if "FROM v3_runtime_records" in sql:
            payload = self._payload()
            return (json.dumps(payload),) if payload is not None else None
        return None

    def _fetchall(self, sql: str) -> list[Any]:
        if "FROM v3_transactional_outbox" in sql:
            return [(mid,) for mid in sorted(self._unknown_ids())]
        return []

    def close(self) -> None:
        return None


class _FakeEngineOps:
    """假引擎状态机:kill 后 process_delay 秒出现新 PID 且 /status 可达;
    新 PID 后 ready_delay 秒 trading_ready=True/recon=post_recon。"""

    def __init__(
        self,
        clock: _FakeClock,
        *,
        old_pid: int = 4242,
        new_pid: int | None = 5353,
        process_delay: float = 20.0,
        ready_delay: float = 40.0,
        baseline_ready: bool = True,
        baseline_recon: str = "MATCHED",
        post_ready: bool = True,
        post_recon: str = "MATCHED",
        baseline_unreachable: bool = False,
        no_process: bool = False,
    ) -> None:
        self.clock = clock
        self.old_pid = old_pid
        self.new_pid = new_pid
        self.process_delay = process_delay
        self.ready_delay = ready_delay
        self.baseline_ready = baseline_ready
        self.baseline_recon = baseline_recon
        self.post_ready = post_ready
        self.post_recon = post_recon
        self.baseline_unreachable = baseline_unreachable
        self.no_process = no_process
        self.killed_at: float | None = None
        self.kickstarted = False
        self.sigkill_calls: list[int] = []

    def engine_pid(self) -> int | None:
        if self.no_process:
            return None
        if self.killed_at is None:
            return self.old_pid
        if self.new_pid is not None and self.clock.now() - self.killed_at >= self.process_delay:
            return self.new_pid
        return None

    def fetch_status(self) -> dict[str, Any]:
        if self.baseline_unreachable and self.killed_at is None:
            raise StatusUnreachableError("engine down")
        if self.killed_at is None:
            return {"trading_ready": self.baseline_ready, "last_reconciliation": {"status": self.baseline_recon}}
        if self.clock.now() - self.killed_at < self.process_delay:
            raise StatusUnreachableError("engine down")
        if self.clock.now() - self.killed_at < self.process_delay + self.ready_delay:
            return {"trading_ready": False, "last_reconciliation": {"status": "UNKNOWN"}}
        return {"trading_ready": self.post_ready, "last_reconciliation": {"status": self.post_recon}}

    def sigkill(self, pid: int) -> None:
        self.sigkill_calls.append(pid)
        self.killed_at = self.clock.now()

    def kickstart(self) -> None:
        self.kickstarted = True


def _ctx(tmp_path: Path, *, dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=None,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


def _scenario(clock: _FakeClock, fake: _FakeEngineOps, pg: _FakePG) -> ProcessRestartScenario:
    return ProcessRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        fetch_status=fake.fetch_status,
        engine_pid=fake.engine_pid,
        sigkill=fake.sigkill,
        kickstart=fake.kickstart,
        connect=lambda dsn: pg,
        poll_interval=5.0,
        process_deadline=120.0,
        ready_deadline=180.0,
    )


# ---- engine_pid_os 候选过滤:bash wrapper 排除,选 python 引擎进程 ----


class _FakeProcResult:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _patch_proc(monkeypatch: Any, *, pgrep_stdout: str, comms: dict[int, str]) -> None:
    """注入假 subprocess.run:pgrep -f 返回匹配集,ps -A -o pid=,comm= 返回可执行名。"""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProcResult:
        if cmd[0] == "/usr/bin/pgrep":
            return _FakeProcResult(pgrep_stdout)
        if cmd[0] == "/bin/ps":
            return _FakeProcResult("".join(f"{pid} {comm}\n" for pid, comm in comms.items()))
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr("beidou_certification.g5_scenarios.restart.process_restart.subprocess.run", _fake_run)


def test_engine_pid_os_filters_wrapper_picks_python(monkeypatch: Any) -> None:
    """实测形态:80298=bash wrapper,80299=python 引擎 —— 必须选 python 进程。"""
    _patch_proc(
        monkeypatch,
        pgrep_stdout="80298\n80299\n",
        comms={80298: "bash", 80299: "/opt/homebrew/bin/python3.12"},
    )
    assert engine_pid_os() == 80299


def test_engine_pid_os_single_python_candidate(monkeypatch: Any) -> None:
    _patch_proc(monkeypatch, pgrep_stdout="9999\n", comms={9999: "python3.12"})
    assert engine_pid_os() == 9999


def test_engine_pid_os_no_match_returns_none(monkeypatch: Any) -> None:
    _patch_proc(monkeypatch, pgrep_stdout="", comms={})
    assert engine_pid_os() is None


def test_engine_pid_os_only_wrapper_returns_none(monkeypatch: Any) -> None:
    """pgrep 只命中 bash wrapper(引擎未在跑)→ None,走 engine_process_not_found。"""
    _patch_proc(monkeypatch, pgrep_stdout="80298\n", comms={80298: "bash"})
    assert engine_pid_os() is None


def test_engine_pid_os_ambiguous_python_candidates_raises(monkeypatch: Any) -> None:
    """多个 python 引擎候选 → EnginePidAmbiguousError,证据含过滤前后 PID 集。"""
    _patch_proc(
        monkeypatch,
        pgrep_stdout="80298\n80299\n80300\n",
        comms={80298: "bash", 80299: "python3.12", 80300: "python3.12"},
    )
    with pytest.raises(EnginePidAmbiguousError) as excinfo:
        engine_pid_os()
    assert excinfo.value.python_candidates == [80299, 80300]
    assert excinfo.value.matched == [80298, 80299, 80300]  # wrapper 保留在匹配集证据


def test_engine_pid_ambiguous_not_verifiable(tmp_path: Path) -> None:
    """engine_pid seam 抛 EnginePidAmbiguousError → 场景 NOT_VERIFIABLE
    (error_type=engine_pid_ambiguous),证据记匹配集与 python 候选,不做 SIGKILL。"""
    clock = _FakeClock()
    pg = _FakePG(_FakeEngineOps(clock))

    def _ambig_pid() -> int | None:
        raise EnginePidAmbiguousError(matched=[4242, 4243, 5353], python_candidates=[4242, 4243])

    scenario = ProcessRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        engine_pid=_ambig_pid,
        sigkill=lambda pid: pytest.fail(f"ambiguous 时不得 SIGKILL: {pid}"),
        kickstart=lambda: pytest.fail("ambiguous 时不得 kickstart"),
        connect=lambda dsn: pg,
    )
    ctx = _ctx(tmp_path)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_pid_ambiguous"
    assert result.evidence["engine_pid_candidates"] == [4242, 4243]
    assert result.evidence["pgrep_matched"] == [4242, 4243, 5353]


# ---- dry_run:NOT_VERIFIABLE 且不触碰任何真实资源 ----


def test_dry_run_not_verifiable_no_real_ops(tmp_path: Path) -> None:
    def _forbidden_pid() -> int | None:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_status() -> dict[str, Any]:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_sigkill(pid: int) -> None:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_kickstart() -> None:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    def _forbidden_connect(dsn: str) -> Any:
        raise AssertionError("dry_run 不得执行任何真实运维动作")

    scenario = ProcessRestartScenario(
        engine_pid=_forbidden_pid,
        fetch_status=_forbidden_status,
        sigkill=_forbidden_sigkill,
        kickstart=_forbidden_kickstart,
        connect=_forbidden_connect,
    )
    ctx = _ctx(tmp_path, dry_run=True)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "RESTART_REQUIRES_REAL_EXECUTION"
    assert result.error_message == "restart requires real execution"
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


# ---- 全流程:SIGKILL → kickstart → 新进程 → ready → durable 校验 → PASS ----


def test_full_flow_process_restart_recovered(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, old_pid=4242, new_pid=5353, process_delay=20.0, ready_delay=40.0)
    pg = _FakePG(fake, unknown_before={"msg-a"})
    scenario = _scenario(clock, fake, pg)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "process_restart_recovered"
    assert evidence["old_pid"] == 4242
    assert evidence["new_pid"] == 5353
    assert evidence["notional_usdt"] == 0.0
    steps = evidence["steps"]

    baseline_pid = next(s for s in steps if s.get("action") == "baseline_pid")
    assert baseline_pid["pid"] == 4242
    baseline_status = next(s for s in steps if s.get("action") == "baseline_status")
    assert baseline_status["ready"] is True and baseline_status["reason"] == "READY"
    snapshot = next(s for s in steps if s.get("action") == "baseline_snapshot")
    assert snapshot["record_id"] == "default:BINANCE"
    assert snapshot["unknown_outbox_ids"] == ["msg-a"]

    assert fake.sigkill_calls == [4242]
    assert fake.kickstarted is True
    sigkill_step = next(s for s in steps if s.get("action") == "sigkill")
    assert sigkill_step["pid"] == 4242
    kickstart_step = next(s for s in steps if s.get("action") == "kickstart")
    assert kickstart_step["target"] == f"gui/{os.getuid()}/com.beidou.autopilot"

    # 轮询行为真实(相对 t0 计时):20s 后新 PID+status 可达(第 5 次查询);
    # 再 40s 后 ready(第 9 次查询)。
    process_up = next(s for s in steps if s.get("action") == "process_up")
    assert process_up["polls"] == 5 and process_up["new_pid"] == 5353
    ready = next(s for s in steps if s.get("action") == "ready_observed")
    assert ready["polls"] == 9
    assert ready["status"]["trading_ready"] is True
    assert clock.t - t0 == 60.0
    assert evidence["restart_elapsed_seconds"] == 20.0
    assert evidence["recovery_elapsed_seconds"] == 40.0

    durable = next(s for s in steps if s.get("action") == "durable_verify")
    assert durable["baseline_intact"] is True
    assert durable["new_unknown_outbox"] == []
    assert durable["unknown_outbox_after"] == ["msg-a"]
    summary = next(s for s in steps if s.get("action") == "post_restart_summary")
    assert summary["old_pid"] == 4242 and summary["new_pid"] == 5353
    assert summary["trading_ready"] is True and summary["recon_status"] == "MATCHED"


# ---- 前置:引擎未就绪 → NOT_VERIFIABLE,不执行 kill ----


def test_engine_not_ready_before_restart_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, baseline_ready=False, baseline_recon="MISMATCHED")
    pg = _FakePG(fake)
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_not_ready_before_restart"
    assert "MISMATCHED" in result.error_message
    assert fake.sigkill_calls == [] and fake.kickstarted is False  # 未执行任何破坏性操作
    assert pg.fake.killed_at is None


def test_status_unreachable_before_restart_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, baseline_unreachable=True)
    pg = _FakePG(fake)
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_not_ready_before_restart"
    assert "不可达" in result.error_message
    assert fake.sigkill_calls == [] and fake.kickstarted is False


def test_engine_process_not_found_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, no_process=True)
    pg = _FakePG(fake)
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "engine_process_not_found"
    assert fake.sigkill_calls == [] and fake.kickstarted is False


def test_baseline_record_missing_not_verifiable(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock)
    pg = _FakePG(fake, baseline=None)
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "baseline_record_missing"
    assert fake.sigkill_calls == [] and fake.kickstarted is False  # 基线缺失时不执行 kill


# ---- 新进程拉起超时 → FAIL ----


def test_process_restart_timeout_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, new_pid=None)  # 永远没有新 PID
    pg = _FakePG(fake)
    scenario = _scenario(clock, fake, pg)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "ProcessRestartTimeoutError"
    assert "PROCESS_RESTART_TIMEOUT" in result.error_message
    steps = result.evidence["steps"]
    assert fake.sigkill_calls == [4242] and fake.kickstarted is True
    timeout = next(s for s in steps if s.get("action") == "process_poll_timeout")
    assert timeout["deadline_seconds"] == 120.0
    # 轮询跑满 120s 窗口(5s 步进,第 25 次查询触发超时)
    assert clock.t - t0 == 120.0


# ---- 恢复超时(新进程起来但 trading_ready 未恢复)→ FAIL ----


def test_engine_recovery_timeout_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock, post_ready=False, post_recon="MATCHED")  # 进程在,但永远不就绪
    pg = _FakePG(fake)
    scenario = _scenario(clock, fake, pg)
    t0 = clock.t
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "EngineRecoveryTimeoutError"
    assert "ENGINE_RECOVERY_TIMEOUT" in result.error_message
    steps = result.evidence["steps"]
    process_up = next(s for s in steps if s.get("action") == "process_up")
    assert process_up["new_pid"] == 5353  # 新进程观测到,卡在 ready 阶段
    timeout = next(s for s in steps if s.get("action") == "ready_poll_timeout")
    assert timeout["deadline_seconds"] == 180.0
    assert timeout["reason"] == "ENGINE_NOT_READY"
    # 20s 拉起 + 180s ready 窗口跑满
    assert clock.t - t0 == 200.0


# ---- durable 漂移 → FAIL ----


def test_durable_baseline_drift_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock)
    pg = _FakePG(fake, baseline_after=_CORRUPTED_PAYLOAD)  # 重启后基线被写坏
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "DurableStateDriftError"
    assert "DURABLE_STATE_DRIFT" in result.error_message
    steps = result.evidence["steps"]
    durable = next(s for s in steps if s.get("action") == "durable_verify")
    assert durable["baseline_intact"] is False
    assert durable["before_hash"] != durable["after_hash"]


def test_durable_unknown_outbox_growth_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock)
    pg = _FakePG(fake, unknown_before={"msg-a"}, unknown_after={"msg-a", "msg-new-unknown"})
    scenario = _scenario(clock, fake, pg)
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "DurableStateDriftError"
    steps = result.evidence["steps"]
    durable = next(s for s in steps if s.get("action") == "durable_verify")
    assert durable["baseline_intact"] is True
    assert durable["new_unknown_outbox"] == ["msg-new-unknown"]


# ---- 异常自捕获 → FAIL,框架不崩溃 ----


def test_sigkill_exception_self_captured_fail(tmp_path: Path) -> None:
    clock = _FakeClock()
    fake = _FakeEngineOps(clock)
    pg = _FakePG(fake)

    def _boom_sigkill(pid: int) -> None:
        raise RuntimeError("fake sigkill failure")

    scenario = ProcessRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        fetch_status=fake.fetch_status,
        engine_pid=fake.engine_pid,
        sigkill=_boom_sigkill,
        kickstart=fake.kickstart,
        connect=lambda dsn: pg,
    )
    result = asyncio.run(scenario.run(_ctx(tmp_path)))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "fake sigkill failure" in result.error_message
    steps = result.evidence["steps"]
    # 前置与 durable 快照已记录,但未到 sigkill 步骤
    assert any(s.get("action") == "baseline_snapshot" for s in steps)
    assert not any(s.get("action") == "sigkill" for s in steps)
