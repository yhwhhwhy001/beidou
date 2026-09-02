from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).parent / "tools"
sys.path.insert(0, str(TOOLS))

import build_governance_inputs as governance_inputs  # noqa: E402
from build_governance_inputs import (  # noqa: E402
    TASK_CUSTODIAN_ID,
    build_window_custody,
    future_contracts,
)

from beidou_research.experiments import assess_oos_preseal_v2  # noqa: E402


def test_future_contracts_bind_exact_unknown_window_without_content_access() -> None:
    source, dataset, lineage = future_contracts()
    assert source["source_contract_digest"]
    assert dataset["prospective_dataset_contract_digest"]
    assert lineage["prospective_pit_contract_digest"]
    assert dataset["oos_data_accessed"] is False
    assert lineage["oos_data_accessed"] is False
    assert dataset["candidate_search"] is False


def test_window_custody_is_real_but_candidate_freeze_remains_separate() -> None:
    source, dataset, lineage = future_contracts()
    window = build_window_custody(
        custody_sealed_at=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
        source=source,
        dataset=dataset,
        lineage=lineage,
    )
    assessment = assess_oos_preseal_v2(window=window, candidate=None)
    assert window.custodian_id == TASK_CUSTODIAN_ID
    assert window.boundary.oos_start == "2026-10-01T00:00:00Z"
    assert window.first_read_not_before == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert assessment.status == "DRAFT_BLOCKED"
    assert assessment.reasons == ("MISSING_CANDIDATE_FREEZE",)
    assert assessment.eligible_for_v2_seal is False


def test_create_reuses_first_metric_owner_observation_time(monkeypatch, tmp_path: Path) -> None:
    observations = iter(("2026-09-02T01:00:00.000000Z", "2026-09-02T02:00:00.000000Z"))

    def fake_verify_metric_owner() -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "status": "PASS_WITH_SCOPE_LIMIT",
            "reasons": [],
            "verified_at": next(observations),
        }

    monkeypatch.setattr(governance_inputs, "verify_metric_owner", fake_verify_metric_owner)

    first = governance_inputs.create(output=tmp_path)
    owner_path = tmp_path / "metric-owner-verification.json"
    first_owner_bytes = owner_path.read_bytes()
    second = governance_inputs.create(output=tmp_path)

    assert owner_path.read_bytes() == first_owner_bytes
    assert first["artifact_hashes"] == second["artifact_hashes"]
