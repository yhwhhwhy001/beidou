"""Semantic coverage for launcher state and identity-checked stop paths."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import beidou_launcher.state as state_module
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from beidou_launcher.state import EvidenceWriter, InstanceLock, force_stop_existing, inspect_runtime_status, read_status


def _report() -> StartupReport:
    return StartupReport(
        mode="paper",
        symbols=["BTCUSDT"],
        port=19090,
        commit="state-test",
        checks=[CheckResult("state.check", "state", CheckStatus.PASS, CheckSeverity.INFO, "ok")],
        phase="RUNNING",
        trading_ready=True,
        supervisor_state="RUNNING",
    )


def test_instance_lock_pid_and_race_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_pid_alive = InstanceLock._pid_alive
    assert InstanceLock._pid_alive(0) is False
    monkeypatch.setattr(state_module.os, "kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    assert InstanceLock._pid_alive(12) is False

    monkeypatch.setattr(state_module.os, "kill", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    assert InstanceLock._pid_alive(12) is True

    monkeypatch.setattr(state_module.os, "kill", lambda *_args: (_ for _ in ()).throw(OSError()))
    assert InstanceLock._pid_alive(12) is False

    path = tmp_path / "beidou.pid"
    lock = InstanceLock(path)
    path.write_text("bad", encoding="utf-8")
    monkeypatch.setattr(InstanceLock, "_pid_alive", staticmethod(lambda _pid: False))
    acquired, _ = lock.acquire()
    assert acquired is True
    lock.release()

    monkeypatch.setattr(state_module.os, "open", lambda *_args: (_ for _ in ()).throw(FileExistsError()))
    raced, reason = InstanceLock(path).acquire()
    assert raced is False
    assert "竞争" in reason

    monkeypatch.setattr(InstanceLock, "_pid_alive", original_pid_alive)
    monkeypatch.setattr(state_module.os, "kill", lambda *_args: None)
    monkeypatch.setattr(state_module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    assert InstanceLock._pid_alive(12) is True
    monkeypatch.setattr(state_module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="launchd"))
    assert InstanceLock._pid_alive(12) is False
    busy = tmp_path / "busy.pid"
    busy.write_text("123", encoding="utf-8")
    monkeypatch.setattr(InstanceLock, "_pid_alive", staticmethod(lambda _pid: True))
    assert InstanceLock(busy).acquire()[0] is False


def test_evidence_writer_persists_state_events_and_status_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.write_event("TEST_EVENT", {"state": "RUNNING"})
    writer.write(_report())
    writer.write(_report())  # identical fingerprint is history-throttled

    event = json.loads((tmp_path / "evidence/bootstrap/supervisor-events.jsonl").read_text(encoding="utf-8"))
    assert event["type"] == "TEST_EVENT"
    saved = read_status(tmp_path)
    assert saved is not None
    assert saved["phase"] == "RUNNING"

    monkeypatch.setattr(state_module.InstanceLock, "_pid_alive", staticmethod(lambda _pid: True))
    current = inspect_runtime_status(tmp_path)
    assert current is not None
    assert current["effective_state"] == "RUNNING"

    (tmp_path / ".beidou/supervisor-state.json").write_text("not-json", encoding="utf-8")
    assert read_status(tmp_path) is None


def test_inspect_and_stop_refuse_stale_or_mismatched_state(tmp_path: Path) -> None:
    pid_path = tmp_path / ".beidou/beidou.pid"
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("123", encoding="utf-8")
    assert force_stop_existing(tmp_path) == (False, "拒绝停止：PID 与监督器状态证据不一致")

    status = _report().to_dict()
    status["pid"] = 123
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    (tmp_path / ".beidou/supervisor-state.json").write_text(json.dumps(status), encoding="utf-8")
    assert inspect_runtime_status(tmp_path)["effective_state"] == "STALE"  # type: ignore[index]

    status["updated_at"] = "bad-time"
    (tmp_path / ".beidou/supervisor-state.json").write_text(json.dumps(status), encoding="utf-8")
    assert force_stop_existing(tmp_path)[0] is False


def test_stop_running_instance_requires_identity_and_signals_only_verified_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / ".beidou/beidou.pid"
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("123", encoding="utf-8")
    status = _report().to_dict()
    status.update({"pid": 123, "updated_at": datetime.now(timezone.utc).isoformat()})
    (tmp_path / ".beidou/supervisor-state.json").write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="python -m beidou_launcher.cli start\n"),
    )
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(state_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    stopped, message = state_module.stop_running_instance(tmp_path)
    assert stopped is True
    assert "123" in message
    assert killed == [(123, state_module.signal.SIGTERM)]

    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="/usr/bin/other-process\n"),
    )
    stopped, message = state_module.stop_running_instance(tmp_path)
    assert stopped is False
    assert "不是北斗进程" in message


def test_read_status_missing_and_stop_rejects_invalid_pid(tmp_path: Path) -> None:
    assert read_status(tmp_path) is None
    pid_path = tmp_path / ".beidou/beidou.pid"
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("not-a-pid", encoding="utf-8")
    stopped, reason = state_module.stop_running_instance(tmp_path)
    assert stopped is False
    assert "PID 文件无效" in reason


def test_state_history_rotation_and_rotation_failure_are_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.history_path.parent.mkdir(parents=True, exist_ok=True)
    writer.history_path.write_text("old\n" * 600, encoding="utf-8")
    original_stat = Path.stat

    def large_stat(path: Path):
        if path == writer.history_path:
            return SimpleNamespace(st_size=10 * 1024 * 1024 + 1)
        return original_stat(path)

    monkeypatch.setattr(Path, "stat", large_stat)
    writer.write(_report())
    assert len(writer.history_path.read_text(encoding="utf-8").splitlines()) <= 501

    broken = EvidenceWriter(tmp_path / "broken")
    broken.history_path.parent.mkdir(parents=True, exist_ok=True)
    broken.history_path.write_text("old\n", encoding="utf-8")

    def fail_stat():
        raise OSError("stat unavailable")

    def conditional_stat(path: Path):
        if path == broken.history_path:
            return fail_stat()
        return original_stat(path)

    monkeypatch.setattr(Path, "stat", conditional_stat)
    broken.write(_report())
    assert broken.history_path.exists()


def test_runtime_status_and_stop_reject_every_invalid_identity_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / ".beidou"
    runtime.mkdir(parents=True)
    status_path = runtime / "supervisor-state.json"
    assert inspect_runtime_status(tmp_path) is None
    status = _report().to_dict()
    status.update({"pid": "bad", "updated_at": "not-a-time"})
    status_path.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(state_module.InstanceLock, "_pid_alive", staticmethod(lambda _pid: False))
    inspected = inspect_runtime_status(tmp_path)
    assert inspected is not None
    assert inspected["effective_state"] == "STALE"
    assert inspected["evidence_age_seconds"] is None

    status.update({"pid": 123, "updated_at": "2026-08-22T12:00:00"})
    status_path.write_text(json.dumps(status), encoding="utf-8")
    inspected = inspect_runtime_status(tmp_path)
    assert inspected is not None

    assert state_module.stop_running_instance(tmp_path) == (False, "未发现运行中的北斗实例")
    pid_path = runtime / "beidou.pid"
    pid_path.write_text("123", encoding="utf-8")
    status.update({"pid": 123, "updated_at": "not-a-time"})
    status_path.write_text(json.dumps(status), encoding="utf-8")
    assert "状态时间无效" in state_module.stop_running_instance(tmp_path)[1]

    status.update({"pid": "bad", "updated_at": datetime.now(timezone.utc).isoformat()})
    status_path.write_text(json.dumps(status), encoding="utf-8")
    assert "PID 与监督器状态证据不一致" in state_module.stop_running_instance(tmp_path)[1]

    status.update({"pid": 123, "updated_at": (datetime.now(timezone.utc).timestamp() - 300)})
    status["updated_at"] = datetime.fromtimestamp(status["updated_at"], tz=timezone.utc).isoformat()
    status_path.write_text(json.dumps(status), encoding="utf-8")
    assert "已过期" in state_module.stop_running_instance(tmp_path)[1]

    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    status_path.write_text(json.dumps(status), encoding="utf-8")
    status["updated_at"] = "2026-08-22T12:00:00"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    assert "过期" in state_module.stop_running_instance(tmp_path)[1]
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    status_path.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ps unavailable")),
    )
    assert "无法验证进程身份" in state_module.stop_running_instance(tmp_path)[1]

    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert "不存在或不可查询" in state_module.stop_running_instance(tmp_path)[1]

    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="'unterminated"),
    )
    assert "不是北斗进程" in state_module.stop_running_instance(tmp_path)[1]

    monkeypatch.setattr(
        state_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="python -m beidou_launcher.cli start"),
    )
    monkeypatch.setattr(state_module.os, "kill", lambda *_args: (_ for _ in ()).throw(OSError("kill denied")))
    assert "停止失败" in state_module.stop_running_instance(tmp_path)[1]
