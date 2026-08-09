"""Contracts between the supervisor cycle and the durable G7 producer."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from beidou_certification.unattended import SLICategory, UnattendedCertification
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor


def _supervisor_for_g7(tmp_path: Path) -> tuple[BeidouSupervisor, UnattendedCertification]:
    certification = UnattendedCertification(str(tmp_path))
    window = certification.create_window("plan", duration_days=30)
    certification.start_window(window.window_id)

    supervisor = object.__new__(BeidouSupervisor)
    supervisor._g7_certification = certification
    supervisor._g7_observed_incident_ids = set()
    supervisor._recovery_count = 0
    supervisor.max_restarts = 3
    supervisor._g7_tracker = SimpleNamespace(summary=lambda: {"cycles": 1})
    return supervisor, certification


def _healthy_checks() -> list[CheckResult]:
    return [
        CheckResult("runtime.health.market_data", "market", CheckStatus.PASS, CheckSeverity.P0, "ok"),
        CheckResult("runtime.execution.order_trace", "orders", CheckStatus.PASS, CheckSeverity.P0, "ok"),
        CheckResult("runtime.safety.protection_coverage", "protection", CheckStatus.PASS, CheckSeverity.P0, "ok"),
        CheckResult("runtime.safety.reconciliation", "recon", CheckStatus.PASS, CheckSeverity.P0, "ok"),
        CheckResult("runtime.health.incidents", "incidents", CheckStatus.PASS, CheckSeverity.P2, "ok"),
    ]


def test_supervisor_persists_one_complete_cycle_and_idempotent_daily_report(tmp_path: Path) -> None:
    supervisor, certification = _supervisor_for_g7(tmp_path)
    window_id = certification.list_windows()[0]["window_id"]

    supervisor._record_g7_certification_evidence(_healthy_checks())
    supervisor._record_g7_certification_evidence(_healthy_checks())

    window = certification.get_window(window_id)
    assert window is not None
    assert len(window.sli_samples) == len(SLICategory) * 2
    assert len(window.daily_reports) == 1
    assert window.total_recovery_count == 0
    cost_sample = next(sample for sample in window.sli_samples if sample.category is SLICategory.COST_PNL_REPORTING)
    assert cost_sample.passed is False

    restarted = UnattendedCertification(str(tmp_path))
    restored = restarted.get_window(window_id)
    assert restored is not None
    assert len(restored.sli_samples) == len(SLICategory) * 2
    assert len(restored.daily_reports) == 1


def test_supervisor_p0_opens_incident_and_resets_g7_window(tmp_path: Path) -> None:
    supervisor, certification = _supervisor_for_g7(tmp_path)
    window_id = certification.list_windows()[0]["window_id"]
    checks = _healthy_checks()
    checks.append(
        CheckResult(
            "runtime.health.realtime_heartbeat",
            "heartbeat",
            CheckStatus.FAIL,
            CheckSeverity.P0,
            "stale",
        )
    )

    supervisor._record_g7_certification_evidence(checks)
    window = certification.get_window(window_id)
    assert window is not None
    assert window.reset_count == 1
    assert len(window.incidents) == 1
    assert window.incidents[0].severity.value == "P0"
