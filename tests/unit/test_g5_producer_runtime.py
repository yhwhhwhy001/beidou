"""Negative contracts for the isolated G5 producer runtime."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_certification.g5_scenarios.restart.process_restart import ProcessRestartScenario, parse_engine_status
from beidou_control.plane import ControlPlane
from beidou_launcher.g5_producer import producer_launchd_target, producer_status_verdict
from beidou_launcher.supervisor import BeidouSupervisor

ROOT = Path(__file__).resolve().parents[2]


def test_producer_status_requires_explicit_held_no_new_risk_contract() -> None:
    payload = {
        "g5_producer_mode": True,
        "g5_producer_writes_held": True,
        "control_action": "NO_NEW_RISK",
        "g5_producer_ready": True,
        "last_reconciliation": {"status": "MATCHED"},
        "trading_ready": False,
    }
    assert producer_status_verdict(payload) == (True, "G5_PRODUCER_READY")
    assert parse_engine_status(payload) == (True, "G5_PRODUCER_READY")

    for field, value in (
        ("g5_producer_mode", False),
        ("g5_producer_writes_held", False),
        ("control_action", "RESUME"),
        ("g5_producer_ready", False),
    ):
        invalid = dict(payload)
        invalid[field] = value
        assert producer_status_verdict(invalid)[0] is False
        assert parse_engine_status(invalid)[0] is False


def test_producer_target_is_separate_from_autopilot() -> None:
    assert producer_launchd_target(uid=os.getuid()).endswith("/com.beidou.g5-producer")
    assert producer_launchd_target(uid=os.getuid()) != f"gui/{os.getuid()}/com.beidou.autopilot"


def test_g5_producer_launchd_template_is_explicitly_held() -> None:
    path = ROOT / "deploy" / "com.beidou.g5-producer.plist"
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "com.beidou.g5-producer"
    assert payload["ProgramArguments"][2] == "g5-producer"
    assert payload["EnvironmentVariables"]["BEIDOU_ENV"] == "testnet"
    assert payload["EnvironmentVariables"]["BEIDOU_G5_PRODUCER"] == "1"
    assert payload["EnvironmentVariables"]["BEIDOU_TERMINAL_WRITE_HOLD"] == "hard"
    assert payload["KeepAlive"] is False


def test_g5_process_restart_forces_launchd_wrapper_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("BEIDOU_G5_PRODUCER", "1")
    monkeypatch.setattr(subprocess, "run", _run)

    ProcessRestartScenario._kickstart_os()

    assert calls == [["/bin/launchctl", "kickstart", "-k", producer_launchd_target()]]


def test_normal_process_restart_keeps_autopilot_kickstart_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.delenv("BEIDOU_G5_PRODUCER", raising=False)
    monkeypatch.setattr(subprocess, "run", _run)

    ProcessRestartScenario._kickstart_os()

    assert calls == [["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/com.beidou.autopilot"]]


@pytest.mark.asyncio
async def test_g5_producer_health_does_not_become_paused_under_no_new_risk(tmp_path: Path) -> None:
    supervisor = BeidouSupervisor(
        project_root=tmp_path,
        mode="testnet",
        symbols=["BTCUSDT"],
        port=19090,
        producer_only=True,
    )
    control = ControlPlane()
    supervisor.engine = SimpleNamespace(
        _control=control,
        _lifecycle=SimpleNamespace(state="ACTIVE"),
        _alerts=SimpleNamespace(_active_incidents={}),
        _running=True,
    )
    supervisor.report.supervisor_state = "DEGRADED"

    await supervisor._apply_debounce_action("RUNNING", [], has_persistent=False)

    assert supervisor.report.supervisor_state == "RUNNING"
    assert supervisor._control_state() == "NO_NEW_RISK"
