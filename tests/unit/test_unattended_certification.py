"""Fail-closed tests for the real G7 unattended producer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from beidou_certification.unattended import (
    MINIMUM_SLI_SAMPLES,
    IncidentRecord,
    IncidentSeverity,
    SLICategory,
    SLISample,
    UnattendedCertification,
    WindowStatus,
)


def _mature_window(tmp_path):
    engine = UnattendedCertification(str(tmp_path))
    window = engine.create_window("test-plan", duration_days=30, commit="commit", g5_hash="g5")
    window.started_at = datetime.now(timezone.utc) - timedelta(days=31)
    window.status = WindowStatus.RUNNING
    return engine, window


def test_empty_mature_window_is_not_verifiable(tmp_path):
    engine, window = _mature_window(tmp_path)

    result = engine.evaluate(window.window_id)

    assert result["status"] == "NOT_VERIFIABLE"
    assert "SLI samples" in result["reason"]


def test_sparse_mature_window_cannot_emit_pass(tmp_path):
    engine, window = _mature_window(tmp_path)
    for index in range(MINIMUM_SLI_SAMPLES - 1):
        window.sli_samples.append(
            SLISample(
                category=list(SLICategory)[index % len(SLICategory)],
                value=1.0,
                threshold=0.9,
                passed=True,
            )
        )

    result = engine.evaluate(window.window_id)

    assert result["status"] == "NOT_VERIFIABLE"


def test_active_incident_blocks_mature_sli_window(tmp_path):
    engine, window = _mature_window(tmp_path)
    for index in range(MINIMUM_SLI_SAMPLES):
        window.sli_samples.append(
            SLISample(
                category=list(SLICategory)[index % len(SLICategory)],
                value=1.0,
                threshold=0.9,
                passed=True,
            )
        )
    # Seven categories are covered above; daily reports are the independent
    # evidence-gap gate and are intentionally supplied for this test.
    window.daily_reports.extend([object()] * window.duration_days)
    window.incidents.append(IncidentRecord("incident-1", IncidentSeverity.P1, "open", "still open"))

    result = engine.evaluate(window.window_id)

    assert result["status"] == "FAIL"
    assert "active incidents" in result["reason"]


def test_complete_window_evidence_survives_process_restart(tmp_path):
    engine = UnattendedCertification(str(tmp_path))
    window = engine.create_window("test-plan", duration_days=30, commit="commit", g5_hash="g5")
    engine.start_window(window.window_id)
    sample = SLISample(
        category=SLICategory.DATA_QUALITY,
        value=1.0,
        threshold=0.9,
        passed=True,
        metadata={"source": "unit"},
    )
    engine.record_sli(window.window_id, sample)
    incident = IncidentRecord("incident-1", IncidentSeverity.P2, "test", "closed")
    engine.open_incident(window.window_id, incident)
    engine.close_incident(window.window_id, incident.incident_id)
    report = engine.generate_daily_report(window.window_id)
    assert report is not None

    # A fresh process must reconstruct the complete evidence graph, not just
    # the counters that the old implementation persisted.
    restarted = UnattendedCertification(str(tmp_path))
    restored = restarted.get_window(window.window_id)
    assert restored is not None
    assert restored.evidence_state_complete is True
    assert restored.plan_version == "test-plan"
    assert restored.commit == "commit"
    assert restored.g5_certificate_hash == "g5"
    assert len(restored.sli_samples) == 1
    assert restored.sli_samples[0].metadata == {"source": "unit"}
    assert len(restored.incidents) == 1
    assert restored.incidents[0].resolved is True
    assert len(restored.daily_reports) == 1
    assert restarted.state_load_errors == ()


def test_legacy_counter_only_state_is_visible_but_cannot_certify(tmp_path):
    state_path = tmp_path / "g7-legacy-state.json"
    state_path.write_text(
        "{"
        '"window_id":"g7-legacy",'
        '"status":"RUNNING",'
        '"started_at":"2020-01-01T00:00:00+00:00",'
        '"elapsed_days":30,'
        '"reset_count":0,'
        '"incident_count":0,'
        '"sli_sample_count":200,'
        '"report_count":30'
        "}"
    )

    engine = UnattendedCertification(str(tmp_path))
    window = engine.get_window("g7-legacy")
    assert window is not None
    assert window.evidence_state_complete is False
    result = engine.evaluate("g7-legacy")
    assert result["status"] == "NOT_VERIFIABLE"
    assert "legacy counter-only" in result["reason"]


def test_state_file_contains_full_evidence_and_schema_version(tmp_path):
    engine = UnattendedCertification(str(tmp_path))
    window = engine.create_window("test-plan")
    engine.start_window(window.window_id)
    engine.record_sli(
        window.window_id,
        SLISample(SLICategory.RECONCILIATION, 1.0, 0.9, True),
    )

    import json

    state = json.loads((tmp_path / f"{window.window_id}-state.json").read_text())
    assert state["state_schema_version"] == 2
    assert state["sli_sample_count"] == 1
    assert len(state["sli_samples"]) == 1
    assert "incidents" in state
    assert "daily_reports" in state


def test_simulated_window_cannot_become_real_after_restart(tmp_path):
    engine = UnattendedCertification(str(tmp_path))
    window = engine.create_window("test-plan")
    engine.start_window(window.window_id)

    result = engine.fast_forward(window.window_id, duration_days=30)
    assert result["status"] == "NOT_VERIFIABLE"
    assert result["is_simulated"] is True

    restarted = UnattendedCertification(str(tmp_path))
    restored_result = restarted.evaluate(window.window_id)
    assert restored_result["status"] == "NOT_VERIFIABLE"
    assert restored_result["is_simulated"] is True


def test_g7_starter_does_not_seed_synthetic_pass_samples():
    """G7 counts only real Supervisor cycles, never a fabricated start snapshot."""

    source = (Path(__file__).parents[2] / "scripts/certification/start_g7.py").read_text(encoding="utf-8")
    assert "record_batch_sli" not in source
    assert "Initial reconciliation" not in source
    assert "No synthetic initial SLI samples written" in source
    assert '"--g5-plan"' in source
    assert "expected_g5_scenarios" in source


def test_g7_fast_forward_binds_explicit_window_id_via_public_api():
    """Simulation must not create an orphan random window or mutate internals."""

    source = (Path(__file__).parents[2] / "scripts/certification/evaluate_g7.py").read_text(encoding="utf-8")
    assert 'engine.create_window("v1.0", duration_days=30, window_id=args.window_id)' in source
    assert "window.window_id = args.window_id" not in source
    assert "engine._windows[args.window_id]" not in source
    assert "engine._active_window = window" not in source


def test_explicit_g7_window_id_is_bound_and_path_safe(tmp_path):
    engine = UnattendedCertification(str(tmp_path))
    window = engine.create_window("plan", commit="commit", window_id="g7-operator-window")
    assert window.window_id == "g7-operator-window"
    assert (tmp_path / "g7-operator-window-state.json").exists()

    with pytest.raises(ValueError, match="Invalid G7 window_id"):
        engine.create_window("plan", window_id="../escape")
    with pytest.raises(ValueError, match="already exists"):
        engine.create_window("plan", window_id="g7-operator-window")
