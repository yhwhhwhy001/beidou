"""M00-E02: preflight and supervisor construction are producer-free."""

from __future__ import annotations

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
    assert not (tmp_path / "artifacts" / "evidence" / "g7").exists()
