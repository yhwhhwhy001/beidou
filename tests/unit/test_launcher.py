"""Unit tests for the one-click launcher."""

from __future__ import annotations

from beidou_launcher.models import CheckReport, CheckResult, CheckStatus
from beidou_launcher.supervisor import RuntimeSnapshot, evaluate_runtime_snapshots, parse_prometheus_metrics


def test_check_report_blocks_critical_failure() -> None:
    report = CheckReport(phase="test", mode="paper")
    report.results.append(
        CheckResult(code="X", subject="x", status=CheckStatus.FAIL, message="failed", critical=True)
    )
    report.finish()
    assert report.passed is False


def test_parse_prometheus_metrics_ignores_labels() -> None:
    metrics = parse_prometheus_metrics(
        """
        # HELP beidou_tick_count ticks
        beidou_tick_count 12
        beidou_error_count 0
        beidou_alerts{key="warning"} 1
        """
    )
    assert metrics == {"beidou_tick_count": 12.0, "beidou_error_count": 0.0}


def test_runtime_evidence_requires_progress_and_active_factors() -> None:
    base_status = {
        "lifecycle_state": "ACTIVE",
        "mode": "paper",
        "control_action": "RESUME",
        "active_incidents": [],
    }
    first = RuntimeSnapshot(
        captured_at=1.0,
        ready={"ready": True},
        status=base_status,
        metrics={"beidou_tick_count": 10, "beidou_error_count": 0, "beidou_active_factors": 8},
    )
    second = RuntimeSnapshot(
        captured_at=7.0,
        ready={"ready": True},
        status=base_status,
        metrics={"beidou_tick_count": 11, "beidou_error_count": 0, "beidou_active_factors": 8},
    )
    report = evaluate_runtime_snapshots(first, second, "paper")
    assert report.passed is True


def test_runtime_evidence_rejects_stalled_clock() -> None:
    status = {
        "lifecycle_state": "ACTIVE",
        "mode": "paper",
        "control_action": "RESUME",
        "active_incidents": [],
    }
    first = RuntimeSnapshot(
        captured_at=1.0,
        ready={"ready": True},
        status=status,
        metrics={"beidou_tick_count": 10, "beidou_error_count": 0, "beidou_active_factors": 8},
    )
    second = RuntimeSnapshot(
        captured_at=7.0,
        ready={"ready": True},
        status=status,
        metrics={"beidou_tick_count": 10, "beidou_error_count": 0, "beidou_active_factors": 8},
    )
    report = evaluate_runtime_snapshots(first, second, "paper")
    assert report.passed is False
