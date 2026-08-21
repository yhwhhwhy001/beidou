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
