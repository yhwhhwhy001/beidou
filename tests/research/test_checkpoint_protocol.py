"""Adversarial proof for the complete T04 checkpoint protocol."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from beidou_research.experiments.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    Checkpoint,
    CheckpointCompatibilityError,
    CheckpointNotVerifiable,
    CheckpointStaleWriterError,
    CheckpointStore,
)
from beidou_research.experiments.contracts import ExperimentRunIdentity
from beidou_research.experiments.store import ExperimentRunLedger


def identity() -> ExperimentRunIdentity:
    return ExperimentRunIdentity(
        run_id="run-checkpoint-001",
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
        seed=19,
    )


def ledger(tmp_path: Path) -> ExperimentRunLedger:
    ledger = ExperimentRunLedger(tmp_path / "runs.sqlite3", identity(), "writer-a", "fence-1")
    ledger.append(
        "CREATED", {"source": "checkpoint-fixture"}, timestamp="2026-01-01T00:00:00Z", idempotency_key="event-1"
    )
    ledger.append(
        "RUNNING", {"source": "checkpoint-fixture"}, timestamp="2026-01-01T00:00:01Z", idempotency_key="event-2"
    )
    return ledger


def checkpoint(
    ledger: ExperimentRunLedger, writer_id: str | None = None, fencing_token: str | None = None
) -> Checkpoint:
    event = ledger.events()[-1]
    return Checkpoint.build(
        identity=ledger.identity,
        event_sequence=event.sequence,
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
        rng_state={"algorithm": "pcg64", "state": [1, 2, 3]},
        search_state={"strategy": "TEMPLATE_GRID", "cursor": 4},
        optimizer_state={"name": "grid", "state": {"next": 5}},
        candidate_queue=["candidate-5", "candidate-6"],
        completed_candidate_ids=["candidate-1", "candidate-2"],
        multiple_testing_family="family-alpha-v1",
        multiple_testing_denominator=100,
        stage_cursor="RUNNING",
        worker_count=1,
        scope={"search": "TEMPLATE_GRID", "datasets": 1, "universes": 1, "timeframes": 1, "jobs": 1},
        idempotency_keys=[item.idempotency_key for item in ledger.events()],
        writer_id=writer_id or ledger.writer_id,
        fencing_token=fencing_token or ledger.fencing_token,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
    )


def test_checkpoint_failing_proof_before_implementation() -> None:
    assert CHECKPOINT_SCHEMA_VERSION == "1.0"


def test_checkpoint_binds_all_required_fields_and_canonical_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    l = ledger(tmp_path)
    cp = checkpoint(l)
    data = cp.as_dict()
    required = {
        "run_id",
        "event_sequence",
        "previous_hash",
        "event_hash",
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
        "rng_state",
        "search_state",
        "optimizer_state",
        "candidate_queue",
        "completed_candidate_ids",
        "multiple_testing_family",
        "multiple_testing_denominator",
        "stage_cursor",
        "worker_count",
        "scope",
        "idempotency_keys",
        "writer_id",
        "fencing_token",
        "schema_version",
        "atomic_storage_digest",
        "checkpoint_digest",
    }
    assert required <= set(data)
    assert cp.digest == data["checkpoint_digest"]
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "checkpoint-field-coverage.json").write_text(
        json.dumps({"required": sorted(required), "covered": sorted(data)}, indent=2) + "\n"
    )
    (evidence / "canonical-hash-report.json").write_text(
        json.dumps({"digest": cp.digest, "canonical": cp.canonical_bytes().decode()}, indent=2) + "\n"
    )


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "event_sequence",
        "previous_hash",
        "event_hash",
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
        "rng_state",
        "search_state",
        "optimizer_state",
        "candidate_queue",
        "completed_candidate_ids",
        "multiple_testing_family",
        "multiple_testing_denominator",
        "stage_cursor",
        "worker_count",
        "scope",
        "idempotency_keys",
        "schema_version",
        "writer_id",
        "fencing_token",
    ],
)
def test_mutating_any_checkpoint_input_changes_digest(field: str, tmp_path: Path) -> None:
    original = checkpoint(ledger(tmp_path))
    data = original.as_dict()
    changed = dict(data)
    if isinstance(changed[field], int):
        changed[field] += 1
    elif isinstance(changed[field], list):
        changed[field] = [*changed[field], "mutation"]
    elif isinstance(changed[field], dict):
        changed[field] = {**changed[field], "mutation": True}
    else:
        changed[field] = f"{changed[field]}-mutation"
    if field in {"worker_count", "scope"}:
        with pytest.raises(ValueError):
            Checkpoint.from_dict(changed)
    else:
        with pytest.raises(CheckpointNotVerifiable):
            Checkpoint.from_dict(changed)


def test_atomic_replace_and_store_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    l = ledger(tmp_path)
    store = CheckpointStore(tmp_path / "checkpoints", identity(), "writer-a", "fence-1")
    cp = checkpoint(l)
    saved = store.save(cp, ledger=l)
    loaded = store.load(
        expected_identity=identity(),
        expected_policy_digest=identity().policy_digest,
        expected_denominator=100,
        ledger=l,
    )
    assert saved == loaded
    assert store.current_path().name == "checkpoint-000001.json"
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "atomicity-fencing-report.json").write_text(
        json.dumps({"generation": 1, "atomic_replace": "PASS"}, indent=2) + "\n"
    )


def test_partial_pointer_or_payload_is_not_verifiable(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    store = CheckpointStore(tmp_path / "checkpoints", identity(), "writer-a", "fence-1")
    store.save(checkpoint(l), ledger=l)
    (tmp_path / "checkpoints" / "checkpoint.current").write_text("partial")
    with pytest.raises(CheckpointNotVerifiable):
        store.load()


def test_pointer_path_escape_is_rejected(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    root = tmp_path / "checkpoints"
    store = CheckpointStore(root, identity(), "writer-a", "fence-1")
    store.save(checkpoint(l), ledger=l)
    (root / "checkpoint.current").write_text(
        json.dumps({"filename": "../outside.json", "generation": 1, "digest": "0" * 64})
    )
    with pytest.raises(CheckpointNotVerifiable):
        store.load()


def test_corrupt_checkpoint_does_not_replace_last_good_generation(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    root = tmp_path / "checkpoints"
    store = CheckpointStore(root, identity(), "writer-a", "fence-1")
    good = store.save(checkpoint(l), ledger=l)
    path = store.current_path()
    raw = json.loads(path.read_text())
    raw["candidate_queue"] = ["tampered"]
    path.write_text(json.dumps(raw))
    with pytest.raises(CheckpointNotVerifiable):
        store.load()
    assert good.event_hash == l.events()[-1].event_hash
    assert path.exists()


def test_unknown_version_and_missing_field_fail_closed(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    data = checkpoint(l).as_dict()
    data["schema_version"] = "999.0"
    data.pop("rng_state")
    with pytest.raises((CheckpointNotVerifiable, ValueError)):
        Checkpoint.from_dict(data)
    for digest_field in ("atomic_storage_digest", "checkpoint_digest"):
        incomplete = checkpoint(l).as_dict()
        incomplete.pop(digest_field)
        with pytest.raises(CheckpointNotVerifiable):
            Checkpoint.from_dict(incomplete)


def test_policy_and_denominator_drift_rejected(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    store = CheckpointStore(tmp_path / "checkpoints", identity(), "writer-a", "fence-1")
    store.save(checkpoint(l), ledger=l)
    with pytest.raises(CheckpointCompatibilityError):
        store.load(expected_policy_digest="different", expected_denominator=100)
    with pytest.raises(CheckpointCompatibilityError):
        store.load(expected_policy_digest=identity().policy_digest, expected_denominator=101)


def test_cross_identity_ledger_is_rejected_even_with_same_run_id(tmp_path: Path) -> None:
    base = identity()
    other = replace(base, policy_digest="9" * 64)
    base_ledger = ExperimentRunLedger(tmp_path / "base.sqlite3", base, "writer-a", "fence-1")
    other_ledger = ExperimentRunLedger(tmp_path / "other.sqlite3", other, "writer-a", "fence-1")
    base_ledger.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="event-1")
    other_ledger.append("CREATED", {}, timestamp="2026-01-01T00:00:00Z", idempotency_key="event-1")
    base_ledger.append("RUNNING", {}, timestamp="2026-01-01T00:00:01Z", idempotency_key="event-2")
    other_ledger.append("RUNNING", {}, timestamp="2026-01-01T00:00:01Z", idempotency_key="event-2")
    store = CheckpointStore(tmp_path / "checkpoints", base, "writer-a", "fence-1")
    with pytest.raises(CheckpointCompatibilityError):
        store.save(checkpoint(base_ledger), ledger=other_ledger)
    store.save(checkpoint(base_ledger), ledger=base_ledger)
    with pytest.raises(CheckpointCompatibilityError):
        store.load(expected_identity=other, ledger=other_ledger)


def test_stale_writer_is_rejected_on_load(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    root = tmp_path / "checkpoints"
    first = CheckpointStore(root, identity(), "writer-a", "fence-1")
    first.save(checkpoint(l), ledger=l)
    CheckpointStore(root, identity(), "writer-b", "fence-2")
    with pytest.raises(CheckpointStaleWriterError):
        first.load()


def test_stale_writer_is_rejected(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    root = tmp_path / "checkpoints"
    first = CheckpointStore(root, identity(), "writer-a", "fence-1")
    second = CheckpointStore(root, identity(), "writer-b", "fence-2")
    with pytest.raises(CheckpointStaleWriterError):
        first.save(checkpoint(l), ledger=l)
    assert second.save(checkpoint(l, "writer-b", "fence-2"), ledger=l).event_sequence == 2


def test_scope_is_strictly_jobs_one_and_declared_shape(tmp_path: Path) -> None:
    l = ledger(tmp_path)
    data = checkpoint(l).as_dict()
    data["scope"] = {"search": "RANDOM", "datasets": 2, "universes": 1, "timeframes": 1, "jobs": 2}
    with pytest.raises(ValueError):
        Checkpoint.from_dict(data)


def test_resume_never_falls_back_to_fresh_run(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    store = CheckpointStore(root, identity(), "writer-a", "fence-1")
    with pytest.raises(CheckpointNotVerifiable):
        store.load()
    assert not hasattr(store, "fresh_run")


def test_checkpoint_recovery_on_copied_store_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    l = ledger(tmp_path)
    source = CheckpointStore(tmp_path / "source", identity(), "writer-a", "fence-1")
    source.save(checkpoint(l), ledger=l)
    copied_root = tmp_path / "copied"
    copied_root.mkdir()
    for path in (tmp_path / "source").iterdir():
        copied_root.joinpath(path.name).write_bytes(path.read_bytes())
    copied = CheckpointStore(copied_root, identity(), "writer-a", "fence-1", register=False)
    assert copied.load() == source.load()
    evidence = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "checkpoint-recovery-transcript.json").write_text(
        json.dumps({"copied_replay": "EQUAL", "fresh_fallback": "FORBIDDEN"}, indent=2) + "\n"
    )
