"""M00-E02: preflight and supervisor construction are producer-free."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import beidou_launcher.supervisor as supervisor_module
from beidou_launcher.models import CheckStatus
from beidou_launcher.preflight import run_preflight
from beidou_launcher.supervisor import BeidouSupervisor


def test_preflight_does_not_create_or_probe_runtime_storage(tmp_path) -> None:
    checks, _guard = run_preflight(tmp_path, "paper", 0)
    storage = next(item for item in checks if item.check_id == "preflight.runtime_storage")

    assert storage.status is CheckStatus.UNKNOWN
    assert not (tmp_path / ".beidou").exists()
    assert not (tmp_path / "evidence").exists()


def test_supervisor_construction_does_not_activate_g7_producer(tmp_path) -> None:
    supervisor = BeidouSupervisor(
        project_root=tmp_path,
        mode="safety_only",
        symbols=["EXPLICIT_SYMBOL"],
        port=19090,
    )

    assert supervisor._g7_certification is None
    assert supervisor._g7_certification_summary() == {
        "active_windows": [],
        "state_load_errors": [],
        "producer_status": "HARD_HOLD",
    }
    assert not (tmp_path / ".beidou").exists()
    assert not (tmp_path / "evidence").exists()
    assert not (tmp_path / "artifacts" / "evidence" / "g7").exists()


def test_blocked_preflight_runs_before_lock_or_evidence_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path.cwd())
    supervisor = BeidouSupervisor(
        project_root=tmp_path,
        mode="safety_only",
        symbols=["EXPLICIT_SYMBOL"],
        port=19090,
    )
    blocker = next(item for item in run_preflight(tmp_path, "paper", 0)[0] if item.is_blocking)
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([blocker], None))
    monkeypatch.setattr(
        supervisor.lock,
        "acquire",
        lambda: (_ for _ in ()).throw(AssertionError("lock must not be created before preflight")),
    )
    monkeypatch.setattr(
        supervisor.writer,
        "write",
        lambda _report: (_ for _ in ()).throw(AssertionError("blocked preflight must not write evidence")),
    )

    assert asyncio.run(supervisor.run()) == 2
    assert not (tmp_path / ".beidou").exists()
    assert not (tmp_path / "evidence").exists()
