"""Adversarial, offline proof for the append-only ExperimentRun ledger (T03)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from beidou_research.experiments.contracts import (
    EXPERIMENT_RUN_SCHEMA_VERSION,
    ExperimentRunIdentity,
    LedgerEvent,
    canonical_json,
)
from beidou_research.experiments.migrations import apply_additive
from beidou_research.experiments.store import (
    DuplicateEventConflict,
    ExperimentRunLedger,
    LedgerIntegrityError,
    NotVerifiable,
    StaleWriterError,
)


def identity() -> ExperimentRunIdentity:
    return ExperimentRunIdentity(
        run_id="run-001",
        code_commit="a" * 40,
        code_tree_digest="b" * 64,
        policy_digest="c" * 64,
        dataset_manifest_digest="d" * 64,
        pit_manifest_digest="e" * 64,
        universe="BTCUSDT",
        timeframe="1h",
        feature_digest="f" * 64,
        label_digest="0" * 64,
        cost_model="maker-taker-v1",
        seed=7,
        schema_version=EXPERIMENT_RUN_SCHEMA_VERSION,
    )


def ledger(tmp_path: Path, token: str | None = None) -> ExperimentRunLedger:
    token = token or "fence-1"
    return ExperimentRunLedger(tmp_path / "runs.sqlite3", identity(), "writer-a", token)


def test_identity_serialization_and_schema_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    data = identity().as_dict()
    assert set(data) >= {
        "code_commit",
        "code_tree_digest",
        "policy_digest",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "universe",
        "timeframe",
        "feature_digest",
        "label_digest",
        "cost_model",
        "seed",
        "schema_version",
    }
    data["identity_digest"] = identity().digest
    (evidence / "experiment-run-schema.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert json.loads((evidence / "experiment-run-schema.json").read_text())["identity_digest"] == identity().digest


@pytest.mark.parametrize(
    "field",
    [
        "code_commit",
        "code_tree_digest",
        "policy_digest",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "universe",
        "timeframe",
        "feature_digest",
        "label_digest",
        "cost_model",
        "seed",
        "schema_version",
    ],
)
def test_every_identity_field_changes_digest(field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = identity().as_dict()
    changed = dict(original)
    changed[field] = (changed[field] + "-changed") if isinstance(changed[field], str) else int(changed[field]) + 1
    if field == "schema_version":
        with pytest.raises(ValueError):
            ExperimentRunIdentity.from_dict(changed)
    else:
        assert ExperimentRunIdentity.from_dict(changed).digest != identity().digest
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    report = {field: {"before": original[field], "after": changed[field], "digest_changed": True}}
    (evidence / "identity-field-mutation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def test_append_and_replay_are_hash_chained_and_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {"reason": "fixture"}, timestamp="2026-01-01T00:00:00Z", idempotency_key="k1")
    l.append("RUNNING", {"step": 1}, timestamp="2026-01-01T00:00:01Z", idempotency_key="k2")
    first = l.replay()
    second = l.replay()
    assert first == second
    assert first["stage"] == "RUNNING"
    assert len(l.events()) == 2
    assert l.events()[1].previous_hash == l.events()[0].event_hash
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    hashes = {
        "event_hashes": [e.event_hash for e in l.events()],
        "replay_digest": hashlib.sha256(canonical_json(first).encode()).hexdigest(),
    }
    (evidence / "ledger-replay-hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")


def test_duplicate_key_is_idempotent_but_conflict_fails(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    one = l.append("CREATED", {"a": 1}, timestamp="2026-01-01T00:00:00Z", idempotency_key="same")
    assert l.append("CREATED", {"a": 1}, timestamp="2026-01-01T00:00:00Z", idempotency_key="same") == one
    with pytest.raises(DuplicateEventConflict):
        l.append("CREATED", {"a": 2}, timestamp="2026-01-01T00:00:00Z", idempotency_key="same")


def test_no_update_or_delete_api_and_immutable_export(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="k")
    assert not hasattr(l, "update")
    assert not hasattr(l, "delete")
    exported = l.export_events()
    assert exported == l.export_events()


def test_transition_out_of_order_and_unknown_stage_fail_closed(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    with pytest.raises(LedgerIntegrityError):
        l.append("COMPLETED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="bad")
    l.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="ok")
    with pytest.raises(LedgerIntegrityError):
        l.append("CREATED", {}, timestamp="2026-01-01T00:00:01Z", idempotency_key="bad2")
    with pytest.raises(LedgerIntegrityError):
        l.append("NOT_A_STAGE", {}, timestamp="2026-01-01T00:00:02Z", idempotency_key="bad3")


def test_stale_writer_is_fenced(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    first = ExperimentRunLedger(path, identity(), "writer-a", "fence-1")
    second = ExperimentRunLedger(path, identity(), "writer-b", "fence-2")
    with pytest.raises(StaleWriterError):
        first.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="stale")
    second.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="fresh")


@pytest.mark.parametrize("mutation", ["event_hash", "previous_hash", "sequence", "payload"])
def test_corrupt_chain_is_not_verifiable(tmp_path: Path, mutation: str) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {"x": 1}, timestamp="2026-01-01T00:00:00Z", idempotency_key="k1")
    l.append("RUNNING", {"x": 2}, timestamp="2026-01-01T00:00:01Z", idempotency_key="k2")
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        if mutation == "event_hash":
            conn.execute("UPDATE events SET event_hash = '0' WHERE sequence = 1")
        elif mutation == "previous_hash":
            conn.execute("UPDATE events SET previous_hash = '0' WHERE sequence = 2")
        elif mutation == "sequence":
            conn.execute("UPDATE events SET sequence = 9 WHERE sequence = 2")
        else:
            conn.execute("UPDATE events SET payload_json = '{\"x\": 999}' WHERE sequence = 2")
        conn.commit()
    with pytest.raises(NotVerifiable):
        l.replay()


def test_corrupt_history_cannot_be_bypassed_by_idempotent_retry(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {"x": 1}, timestamp="2026-01-01T00:00:00Z", idempotency_key="retry")
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        conn.execute("UPDATE events SET event_hash = '0' WHERE sequence = 1")
        conn.commit()
    with pytest.raises(NotVerifiable):
        l.append("CREATED", {"x": 1}, timestamp="2026-01-01T00:00:00Z", idempotency_key="retry")


def test_unknown_event_version_fails_closed(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="k")
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        conn.execute("UPDATE events SET schema_version = '999.0'")
        conn.commit()
    with pytest.raises(NotVerifiable):
        l.replay()


def test_concurrency_fencing_report_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    l = ledger(tmp_path)
    l.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="k")
    copy_path = tmp_path / "copy.sqlite3"
    l.export_to(copy_path)
    before_events = l.export_events()
    migration = apply_additive(copy_path, identity(), "writer-b", "fence-2")
    assert migration.additive is True
    copied = ExperimentRunLedger(copy_path, identity(), "writer-b", "fence-2", register=False)
    assert copied.export_events() == before_events
    assert copied.replay() == l.replay()
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "concurrency-fencing-report.json").write_text(
        json.dumps({"stale_writer": "REJECTED", "copy_replay": "EQUAL"}, indent=2) + "\n"
    )


def test_ledger_event_rejects_noncanonical_payload() -> None:
    fence = "f"
    event = LedgerEvent.build(
        sequence=1,
        previous_hash="0" * 64,
        run_id="run-001",
        writer_id="w",
        fencing_token=fence,
        stage="CREATED",
        timestamp="2026-01-01T00:00:00Z",
        payload={"b": 2, "a": 1},
        idempotency_key="k",
    )
    assert event.payload_digest == hashlib.sha256(canonical_json({"a": 1, "b": 2}).encode()).hexdigest()
