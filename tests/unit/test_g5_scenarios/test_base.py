from __future__ import annotations

import json
from pathlib import Path

import pytest

from beidou_certification.g5_scenarios.base import (
    EvidenceWriteError,
    NotionalExceededError,
    NotionalLedger,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
    write_scenario_evidence,
)


def test_scenario_result_hash_is_deterministic():
    r = ScenarioResult(
        scenario_id="create_query_cancel",
        status=ScenarioStatus.PASS,
        evidence={"steps": [{"action": "order", "order_id": "1"}]},
        duration=1.0,
    )
    h1, h2 = r.artifact_hash(), r.artifact_hash()
    assert len(h1) == 64 and h1 == h2
    r2 = ScenarioResult(scenario_id="create_query_cancel", status=ScenarioStatus.FAIL, evidence={}, duration=1.0)
    assert r2.artifact_hash() != h1


def test_notional_ledger_enforces_limit():
    ledger = NotionalLedger(limit_usdt=20.0)
    ledger.record("s1", 15.0)
    assert ledger.total == 15.0
    with pytest.raises(NotionalExceededError) as ei:
        ledger.record("s2", 6.0)
    assert ei.value.scenario_id == "s2" and ei.value.limit == 20.0


def test_write_evidence_roundtrip(tmp_path: Path):
    ctx = ScenarioContext(
        client=None, ledger=NotionalLedger(20.0), evidence_dir=tmp_path, symbol="BTCUSDT", dry_run=True
    )
    r = ScenarioResult(scenario_id="x", status=ScenarioStatus.PASS, evidence={"k": "v"}, duration=0.0)
    p = write_scenario_evidence(ctx, r)
    data = json.loads(Path(p).read_text())
    assert data["scenario_id"] == "x" and data["evidence"] == {"k": "v"}
    assert "artifact_hash" in data and "written_at" in data


def test_write_evidence_failure_raises(tmp_path: Path):
    (tmp_path / "no").write_text("block")
    ctx = ScenarioContext(
        client=None,
        ledger=NotionalLedger(20.0),
        evidence_dir=tmp_path / "no" / "such" / "dir" / "file",
        symbol="B",
        dry_run=True,
    )
    r = ScenarioResult(scenario_id="x", status=ScenarioStatus.PASS, evidence={}, duration=0.0)
    with pytest.raises(EvidenceWriteError):
        write_scenario_evidence(ctx, r)
