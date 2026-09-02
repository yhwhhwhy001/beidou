from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).parent / "tools"
sys.path.insert(0, str(TOOLS))

from build_august_pit_lineage import create, verify  # noqa: E402

from beidou_research.data.pit_lineage import (  # noqa: E402
    REQUIRED_LINEAGE_ROLES,
    inspect_lineage,
)


def test_august_pairwise_lineage_is_complete_causal_and_idempotent(tmp_path: Path) -> None:
    first = create(output=tmp_path)
    manifest_path = tmp_path / "pit-lineage-manifest.json"
    first_manifest = manifest_path.read_bytes()
    second = create(output=tmp_path)

    manifest = json.loads(first_manifest)
    inspection = inspect_lineage(
        manifest,
        root=tmp_path,
        expected_manifest_digest=manifest["manifest_digest"],
    )
    features = json.loads((tmp_path / "roles/features.json").read_text(encoding="utf-8"))
    labels = json.loads((tmp_path / "roles/labels.json").read_text(encoding="utf-8"))

    assert manifest_path.read_bytes() == first_manifest
    assert first["manifest_digest"] == second["manifest_digest"]
    assert inspection.status == "VERIFIABLE"
    assert inspection.covered_roles == tuple(sorted(REQUIRED_LINEAGE_ROLES))
    assert inspection.coverage_percent == 100
    assert len(features["records"]) == len(labels["records"]) == 2_676
    assert {row["record_id"] for row in features["records"]} == {
        row["record_id"] for row in labels["records"]
    }
    assert all(row["available_as_of"] <= row["decision_time"] for row in features["records"])
    assert all(row["available_as_of"] > row["decision_time"] for row in labels["records"])
    assert first["candidate_search"] is False
    assert first["promotion_use"] == "NOT_VERIFIABLE"
    assert first["source_vintage_at_decision"] == "NOT_VERIFIABLE_RETROSPECTIVE_RECONSTRUCTION"
    assert verify(output=tmp_path)["status"] == "PASS_WITH_CONDITIONS"
