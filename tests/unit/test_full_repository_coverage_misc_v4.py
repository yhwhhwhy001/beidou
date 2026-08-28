"""Behavior-backed coverage for the remaining V4 research/runtime seams."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_pit_lineage_rejects_malformed_artifacts_and_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.data.pit_lineage as pit
    from tests.research.test_pit_lineage_and_oos_seal import _lineage, _rehash_artifact

    with pytest.raises(ValueError, match="canonical UTC"):
        pit._utc_timestamp("2024-01-01")

    class _OffsetDateTime:
        @staticmethod
        def fromisoformat(_value: str) -> datetime:
            return datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1)))

    monkeypatch.setattr(pit, "datetime", _OffsetDateTime)
    with pytest.raises(ValueError, match="must be UTC"):
        pit._utc_timestamp("2024-01-01T00:00:00Z")
    monkeypatch.undo()

    manifest, root = _lineage(tmp_path)
    artifact = manifest.artifacts["dataset"].as_dict()
    for mutation, expected in (
        (lambda value: value.pop("role"), "MISSING_ARTIFACT_FIELDS:role"),
        (lambda value: value.__setitem__("extra", "x"), "UNKNOWN_ARTIFACT_FIELDS:extra"),
        (lambda value: value.__setitem__("source_id", 1), "INVALID_ARTIFACT_FIELD_TYPE:source_id"),
        (lambda value: value.__setitem__("point_in_time", "yes"), "INVALID_ARTIFACT_FIELD_TYPE:point_in_time"),
    ):
        changed = dict(artifact)
        mutation(changed)
        with pytest.raises((TypeError, ValueError), match=expected):
            pit.LineageArtifact.from_dict(changed)

    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(pit.LineageNotVerifiable, match="SOURCE_OUTSIDE_LINEAGE_ROOT"):
        pit.build_lineage_artifact(
            role="dataset",
            path=outside,
            root=root,
            source_id="dataset-2024",
            revision_id="revision-1",
            available_as_of="2024-01-01T00:00:00Z",
            event_time_start="2023-01-01T00:00:00Z",
            event_time_end="2023-12-01T00:00:00Z",
            timezone="UTC",
            point_in_time=True,
        )
    missing = tmp_path / "missing.json"
    with pytest.raises(pit.LineageNotVerifiable, match="MISSING_LINEAGE_SOURCE"):
        pit.build_lineage_artifact(
            role="dataset",
            path=missing,
            root=root,
            source_id="dataset-2024",
            revision_id="revision-1",
            available_as_of="2024-01-01T00:00:00Z",
            event_time_start="2023-01-01T00:00:00Z",
            event_time_end="2023-12-01T00:00:00Z",
            timezone="UTC",
            point_in_time=True,
        )

    payload = manifest.as_dict()
    payload.pop("as_of")
    payload["extra"] = True
    payload["schema_version"] = "9"
    payload["artifacts"] = []
    report = pit.inspect_lineage(payload, root=root, expected_manifest_digest="0" * 64)
    assert report.status == "NOT_VERIFIABLE"
    assert {
        "MISSING_MANIFEST_FIELDS:as_of",
        "UNKNOWN_MANIFEST_FIELDS:extra",
        "UNKNOWN_LINEAGE_SCHEMA_VERSION",
        "INVALID_MANIFEST_AS_OF",
        "INVALID_ARTIFACT_MAP",
        "EXPECTED_MANIFEST_DIGEST_MISMATCH",
    } <= set(report.reasons)

    payload = manifest.as_dict()
    payload["artifacts"]["unknown"] = payload["artifacts"]["dataset"]
    payload["artifacts"]["dataset"] = "not-an-artifact"
    report = pit.inspect_lineage(payload, root=root)
    assert "UNKNOWN_ROLES:unknown" in report.reasons
    assert "INVALID_ARTIFACT:dataset" in report.reasons

    payload = manifest.as_dict()
    dataset = payload["artifacts"]["dataset"]
    dataset.update(
        role="features",
        artifact_digest="bad",
        content_sha256="bad",
        source_id="latest",
        source_path="../escape",
    )
    report = pit.inspect_lineage(payload, root=root)
    assert {
        "ROLE_KEY_MISMATCH:dataset",
        "ARTIFACT_DIGEST_MISMATCH:dataset",
        "INVALID_CONTENT_DIGEST:dataset",
        "AMBIGUOUS_PROVENANCE:dataset",
        "UNSAFE_SOURCE_PATH:dataset",
    } <= set(report.reasons)

    payload = manifest.as_dict()
    payload["artifacts"]["dataset"]["source_path"] = "missing-source.json"
    _rehash_artifact(payload, "dataset")
    assert "MISSING_SOURCE_FILE:dataset" in pit.inspect_lineage(payload, root=root).reasons

    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "source.json").write_text("source", encoding="utf-8")
    symlink = tmp_path / "escape-link"
    symlink.symlink_to(outside_dir, target_is_directory=True)
    payload = manifest.as_dict()
    payload["artifacts"]["dataset"]["source_path"] = "escape-link/source.json"
    _rehash_artifact(payload, "dataset")
    assert "UNSAFE_SOURCE_PATH:dataset" in pit.inspect_lineage(payload, root=root).reasons


def test_pit_lineage_semantic_windows_and_bindings_fail_closed(tmp_path: Path) -> None:
    import beidou_research.data.pit_lineage as pit
    from tests.research.test_pit_lineage_and_oos_seal import _identity_and_checkpoint, _lineage, _rehash_artifact

    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    dataset = payload["artifacts"]["dataset"]
    dataset.update(
        event_time_start="2025-01-01T00:00:00Z",
        event_time_end="2024-12-01T00:00:00Z",
        available_as_of="2025-01-01T00:00:00Z",
    )
    _rehash_artifact(payload, "dataset")
    report = pit.inspect_lineage(payload, root=root)
    assert {
        "INVALID_EVENT_WINDOW:dataset",
        "AVAILABLE_AFTER_AS_OF:dataset",
        "EVENT_AFTER_AS_OF:dataset",
    } <= set(report.reasons)

    payload = manifest.as_dict()
    payload["artifacts"]["features"]["event_time_end"] = "invalid"
    _rehash_artifact(payload, "features")
    report = pit.inspect_lineage(payload, root=root)
    assert "INVALID_TIMESTAMP:features" in report.reasons

    payload = manifest.as_dict()
    payload["manifest_digest"] = "f" * 64
    with pytest.raises(pit.LineageNotVerifiable, match="MANIFEST_DIGEST_MISMATCH"):
        pit.load_pit_manifest(payload, root=root)

    identity, checkpoint = _identity_and_checkpoint(manifest)
    wrong_checkpoint = replace(
        checkpoint,
        pit_manifest_digest="0" * 64,
        run_id="wrong-run",
    )
    with pytest.raises(pit.LineageNotVerifiable) as error:
        pit.require_experiment_binding(manifest, identity, wrong_checkpoint)
    assert "CHECKPOINT_PIT_MANIFEST_DIGEST_BINDING_MISMATCH" in str(error.value)
    assert "CHECKPOINT_RUN_BINDING_MISMATCH" in str(error.value)


def test_checkpoint_value_and_store_integrity_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.experiments.checkpoint as checkpoint_module
    from beidou_research.experiments.checkpoint import (
        Checkpoint,
        CheckpointCompatibilityError,
        CheckpointNotVerifiable,
        CheckpointStaleWriterError,
        CheckpointStore,
    )
    from tests.research.test_checkpoint_protocol import checkpoint, identity, ledger

    active_ledger = ledger(tmp_path)
    current = checkpoint(active_ledger)
    for field, value, expected in (
        ("multiple_testing_denominator", 0, "INVALID_MULTIPLE_TESTING_DENOMINATOR"),
        ("event_sequence", 0, "INVALID_CHECKPOINT_SEQUENCE_OR_WORKERS"),
        ("writer_id", " ", "EMPTY_CHECKPOINT_FIELD:writer_id"),
    ):
        changed = current.as_dict()
        changed[field] = value
        with pytest.raises(ValueError, match=expected):
            Checkpoint.from_dict(changed)
    changed = current.as_dict()
    changed["checkpoint_digest"] = "0" * 64
    with pytest.raises(CheckpointNotVerifiable, match="CHECKPOINT_DIGEST_MISMATCH"):
        Checkpoint.from_dict(changed)

    with pytest.raises(ValueError, match="writer_id and fencing_token"):
        CheckpointStore(tmp_path / "bad-store", identity(), "", "fence")
    with pytest.raises(CheckpointNotVerifiable, match="MISSING_WRITER_FENCE"):
        CheckpointStore(tmp_path / "unregistered", identity(), "writer", "fence", register=False)

    cleanup_root = tmp_path / "cleanup"
    store = CheckpointStore(cleanup_root, identity(), "writer-a", "fence-1")
    monkeypatch.setattr(checkpoint_module.os, "replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        store._atomic_bytes(cleanup_root / "target", b"payload")
    assert not list(cleanup_root.glob(".target.*"))
    monkeypatch.undo()

    store._fence_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="INVALID_WRITER_FENCE"):
        store._check_fence()

    pointer_root = tmp_path / "pointer"
    pointer_store = CheckpointStore(pointer_root, identity(), "writer-a", "fence-1")
    (pointer_root / "checkpoint-000001.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="MISSING_CHECKPOINT_POINTER"):
        pointer_store._pointer()
    for pointer in (
        {"filename": "checkpoint-000001.json"},
        {"filename": "checkpoint-000001.json", "digest": "0" * 64, "generation": 0},
    ):
        (pointer_root / "checkpoint.current").write_text(json.dumps(pointer), encoding="utf-8")
        with pytest.raises(CheckpointNotVerifiable, match="INVALID_CHECKPOINT_POINTER"):
            pointer_store._pointer()

    valid_store = CheckpointStore(tmp_path / "valid", identity(), "writer-a", "fence-1")
    with pytest.raises(CheckpointCompatibilityError, match="CHECKPOINT_IDENTITY_MISMATCH"):
        valid_store.save(replace(current, code_commit="9" * 40))
    with pytest.raises(CheckpointStaleWriterError, match="CHECKPOINT_WRITER_OR_RUN_MISMATCH"):
        valid_store.save(replace(current, writer_id="writer-b"))

    wrong_ledger = ledger(tmp_path / "other")
    wrong_event = wrong_ledger.events()[-1]
    wrong_sequence = replace(current, event_hash=wrong_event.previous_hash)
    with pytest.raises(CheckpointNotVerifiable, match="LEDGER_CHECKPOINT_SEQUENCE_MISMATCH"):
        valid_store.save(wrong_sequence, ledger=active_ledger)
    wrong_state = replace(current, stage_cursor="CREATED")
    with pytest.raises(CheckpointNotVerifiable, match="LEDGER_CHECKPOINT_STATE_MISMATCH"):
        valid_store.save(wrong_state, ledger=active_ledger)


def test_checkpoint_generation_and_load_rejections(tmp_path: Path) -> None:
    from beidou_research.experiments.checkpoint import (
        CheckpointCompatibilityError,
        CheckpointNotVerifiable,
        CheckpointStore,
    )
    from tests.research.test_checkpoint_protocol import checkpoint, identity, ledger

    active_ledger = ledger(tmp_path)
    current = checkpoint(active_ledger)
    root = tmp_path / "store"
    store = CheckpointStore(root, identity(), "writer-a", "fence-1")

    (root / "checkpoint.current").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="INVALID_CHECKPOINT_POINTER"):
        store.save(current)
    (root / "checkpoint.current").unlink()
    (root / "checkpoint-000001.json").write_text("conflict", encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="MISSING_CHECKPOINT_POINTER"):
        store.save(current)
    (root / "checkpoint-000001.json").unlink()
    store.save(current, ledger=active_ledger)

    pointer = json.loads((root / "checkpoint.current").read_text())
    payload_path = root / pointer["filename"]
    payload = payload_path.read_bytes()
    payload_path.unlink()
    with pytest.raises(CheckpointNotVerifiable, match="MISSING_CHECKPOINT_PAYLOAD"):
        store.load()

    payload_path.write_bytes(b" " + payload)
    pointer["digest"] = hashlib.sha256(b" " + payload).hexdigest()
    (root / "checkpoint.current").write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="NON_CANONICAL_CHECKPOINT_BYTES"):
        store.load()

    payload_path.write_text("[]", encoding="utf-8")
    pointer["digest"] = hashlib.sha256(b"[]").hexdigest()
    (root / "checkpoint.current").write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="INVALID_CHECKPOINT_PAYLOAD"):
        store.load()

    clean = CheckpointStore(tmp_path / "clean", identity(), "writer-a", "fence-1")
    clean.save(current)
    other_identity = replace(identity(), run_id="other-run")
    from beidou_research.experiments.store import ExperimentRunLedger

    other_ledger = ExperimentRunLedger(tmp_path / "other.sqlite", other_identity, "writer-a", "fence-1")
    with pytest.raises(CheckpointCompatibilityError, match="LEDGER_IDENTITY_MISMATCH"):
        clean.load(ledger=other_ledger)

    empty_ledger = ExperimentRunLedger(tmp_path / "empty.sqlite", identity(), "writer-a", "fence-1")
    with pytest.raises(CheckpointNotVerifiable, match="LEDGER_CHECKPOINT_SEQUENCE_MISMATCH"):
        clean.load(ledger=empty_ledger)

    from beidou_research.experiments.checkpoint import Checkpoint

    event = active_ledger.events()[-1]
    different_state_checkpoint = Checkpoint.build(
        identity=identity(),
        event_sequence=event.sequence,
        previous_hash=event.previous_hash,
        event_hash=event.event_hash,
        rng_state={"algorithm": "pcg64"},
        search_state={"cursor": 1},
        optimizer_state={"next": 2},
        candidate_queue=[],
        completed_candidate_ids=[],
        multiple_testing_family="family",
        multiple_testing_denominator=1,
        stage_cursor=event.stage,
        worker_count=1,
        scope={"search": "TEMPLATE_GRID", "datasets": 1, "universes": 1, "timeframes": 1, "jobs": 1},
        idempotency_keys=["wrong-1", "wrong-2"],
        writer_id="writer-a",
        fencing_token="fence-1",  # noqa: S106 - deterministic non-secret fixture token
    )
    state_store = CheckpointStore(tmp_path / "state-store", identity(), "writer-a", "fence-1")
    state_store.save(different_state_checkpoint)
    with pytest.raises(CheckpointNotVerifiable, match="LEDGER_CHECKPOINT_STATE_MISMATCH"):
        state_store.load(ledger=active_ledger)


def test_experiment_ledger_rejects_corruption_and_invalid_writes(tmp_path: Path) -> None:
    from beidou_research.experiments.store import (
        ExperimentRunLedger,
        LedgerIntegrityError,
        NotVerifiable,
        StaleWriterError,
    )
    from tests.research.test_experiment_run_ledger import identity

    with pytest.raises(ValueError, match="writer_id and fencing_token"):
        ExperimentRunLedger(tmp_path / "bad.sqlite", identity(), "", "fence")
    ledger = ExperimentRunLedger(tmp_path / "events.sqlite", identity(), "writer-a", "fence-1")
    with pytest.raises(LedgerIntegrityError, match="UNKNOWN_EVENT_VERSION"):
        ledger.append("CREATED", {}, timestamp="t", idempotency_key="id", schema_version="9")
    with pytest.raises(ValueError, match="timestamp and idempotency_key"):
        ledger.append("CREATED", {}, timestamp="", idempotency_key="id")
    with pytest.raises(StaleWriterError, match="WRITER_IDENTITY_MISMATCH"):
        ledger.append("CREATED", {}, timestamp="t", idempotency_key="id", writer_id="writer-b")

    event = ledger.append("CREATED", {"ok": True}, timestamp="t", idempotency_key="id")
    ledger._conn.execute(
        "UPDATE events SET payload_json = ? WHERE run_id = ? AND sequence = 1",
        ("not-json", identity().run_id),
    )
    with pytest.raises(NotVerifiable, match="INVALID_PAYLOAD_JSON"):
        ledger.replay()
    ledger._conn.execute(
        "UPDATE events SET payload_json = ?, payload_digest = ? WHERE run_id = ? AND sequence = 1",
        (event.payload_json, event.payload_digest, identity().run_id),
    )
    ledger._conn.execute("UPDATE events SET stage='COMPLETED' WHERE run_id=?", (identity().run_id,))
    with pytest.raises(NotVerifiable, match="INVALID_TRANSITION"):
        ledger.replay()

    destination = tmp_path / "export.sqlite"
    destination.write_text("old", encoding="utf-8")
    ledger.export_to(destination)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_oos_types_reject_malformed_boundaries_seals_and_audit_events(tmp_path: Path) -> None:
    from beidou_research.experiments.oos_seal import (
        OOSAccessEvent,
        OOSAuditNotVerifiable,
        OOSBoundary,
        OOSSeal,
        OOSSealNotVerifiable,
        OOSSealStore,
    )
    from tests.research.test_pit_lineage_and_oos_seal import _boundary, _seal_store

    with pytest.raises(ValueError, match="canonical UTC"):
        OOSBoundary("2023-01-01", "2023-02-01T00:00:00Z", "2023-03-01T00:00:00Z", "UTC")
    with pytest.raises(ValueError, match="OOS_BOUNDARY_MUST_USE_UTC"):
        OOSBoundary("2023-01-01T00:00:00Z", "2023-02-01T00:00:00Z", "2023-03-01T00:00:00Z", "CST")
    with pytest.raises(ValueError, match="INVALID_OOS_BOUNDARY_ORDER"):
        OOSBoundary("2023-02-01T00:00:00Z", "2023-02-01T00:00:00Z", "2023-03-01T00:00:00Z", "UTC")

    _manifest, _identity, _checkpoint, _store, seal = _seal_store(tmp_path)
    for mutate, expected in (
        (lambda value: value.pop("seal_id"), "MISSING_SEAL_FIELDS"),
        (lambda value: value.__setitem__("schema_version", "9"), "UNKNOWN_OOS_SEAL_VERSION"),
        (lambda value: value.__setitem__("seal_id", "bad"), "INVALID_SEAL_DIGEST:seal_id"),
        (lambda value: value.__setitem__("run_id", ""), "EMPTY_SEAL_RUN_ID"),
        (
            lambda value: value.__setitem__("sealed_at", value["evaluation_not_before"]),
            "SEAL_NOT_CREATED_BEFORE_EVALUATION",
        ),
        (lambda value: value.__setitem__("sealed_at", "bad"), "INVALID_SEAL_TIMESTAMP"),
        (lambda value: value.__setitem__("seal_digest", "0" * 64), "OOS_SEAL_DIGEST_MISMATCH"),
    ):
        value = seal.as_dict()
        mutate(value)
        with pytest.raises(OOSSealNotVerifiable, match=expected):
            OOSSeal.from_dict(value)

    event = OOSAccessEvent.build(
        audit_key=b"a" * 32,
        sequence=1,
        previous_hash="0" * 64,
        seal_digest=seal.digest,
        accessed_at="2024-02-01T00:00:00Z",
        run_id=seal.run_id,
        experiment_identity_digest=seal.experiment_identity_digest,
        checkpoint_digest=seal.checkpoint_digest,
        boundary_digest=seal.boundary_digest,
        purpose="evaluate",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="1" * 64,
        decision="ALLOWED_ONCE",
        reasons=(),
    )
    missing = event.as_dict()
    missing.pop("purpose")
    with pytest.raises(OOSAuditNotVerifiable, match="MISSING_AUDIT_FIELDS"):
        OOSAccessEvent.from_dict(missing)
    unknown = event.as_dict()
    unknown["schema_version"] = "9"
    with pytest.raises(OOSAuditNotVerifiable, match="UNKNOWN_AUDIT_VERSION"):
        OOSAccessEvent.from_dict(unknown)

    occupied = OOSSealStore(tmp_path / "occupied")
    occupied.audit_path.write_text("", encoding="utf-8")
    with pytest.raises(OOSSealNotVerifiable, match="IMMUTABLE_OOS_SEAL_ALREADY_EXISTS"):
        occupied.create(
            boundary=_boundary(),
            key=b"k" * 32,
            audit_key=b"a" * 32,
            lineage_digest="1" * 64,
            run_id="run",
            experiment_identity_digest="2" * 64,
            checkpoint_digest="3" * 64,
            sealed_at="2024-01-01T00:00:00Z",
            evaluation_not_before="2024-02-01T00:00:00Z",
        )


def test_oos_store_fail_closed_io_and_access_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.experiments.oos_seal as oos
    from tests.research.test_pit_lineage_and_oos_seal import (
        AUDIT_KEY,
        KEY,
        _boundary,
        _evaluation_digest,
        _seal_store,
    )

    manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    for key, audit_key, expected in (
        (b"short", AUDIT_KEY, "OOS_SEAL_KEY_TOO_SHORT"),
        (KEY, b"short", "OOS_AUDIT_KEY_TOO_SHORT"),
    ):
        with pytest.raises(ValueError, match=expected):
            oos.OOSSealStore(tmp_path / expected).create(
                boundary=_boundary(),
                key=key,
                audit_key=audit_key,
                lineage_digest=manifest.digest,
                run_id=identity.run_id,
                experiment_identity_digest=identity.digest,
                checkpoint_digest=checkpoint.digest,
                sealed_at="2024-01-01T00:00:00Z",
                evaluation_not_before="2024-02-01T00:00:00Z",
            )
    with pytest.raises(ValueError, match="SEAL_MUST_PRECEDE_CANDIDATE_EVALUATION"):
        oos.OOSSealStore(tmp_path / "late").create(
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            lineage_digest=manifest.digest,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            sealed_at="2024-03-01T00:00:00Z",
            evaluation_not_before="2024-02-01T00:00:00Z",
        )

    missing = oos.OOSSealStore(tmp_path / "missing")
    with pytest.raises(oos.OOSSealNotVerifiable, match="MISSING_OOS_SEAL"):
        missing.load_seal()
    store.seal_path.write_text("null", encoding="utf-8")
    with pytest.raises(oos.OOSSealNotVerifiable, match="INVALID_OOS_SEAL"):
        store.load_seal()

    # Restore a fresh store for access and audit semantics.
    other = tmp_path / "fresh"
    other.mkdir()
    manifest, identity, checkpoint, store, seal = _seal_store(other)
    store.seal_path.write_text(" " + store.seal_path.read_text(), encoding="utf-8")
    with pytest.raises(oos.OOSSealNotVerifiable, match="NON_CANONICAL_OOS_SEAL_BYTES"):
        store.load_seal()
    store.seal_path.write_text(json.dumps(seal.as_dict(), sort_keys=True, separators=(",", ":")) + "\n")

    store.audit_path.write_text("\n", encoding="utf-8")
    with pytest.raises(oos.OOSAuditNotVerifiable, match="BLANK_OOS_AUDIT_EVENT"):
        store.audit_events()
    store.audit_path.write_text("null\n", encoding="utf-8")
    with pytest.raises(oos.OOSAuditNotVerifiable, match="INVALID_OOS_AUDIT_EVENT"):
        store.audit_events()

    store.audit_path.unlink()
    wrong_seal = replace(seal, run_id="wrong")
    with pytest.raises(oos.OOSSealNotVerifiable, match="SUPPLIED_SEAL_DOES_NOT_MATCH"):
        store.access(
            seal=wrong_seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="2024-02-01T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest=_evaluation_digest(identity),
            purpose="evaluate",
        )

    with pytest.raises(oos.OOSAccessInvalidated) as invalid:
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=b"z" * 32,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="invalid",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest="bad",
            purpose=" ",
        )
    assert {
        "INVALID_OOS_ACCESS_TIMESTAMP",
        "UNBOUND_OOS_AUDIT_ANCHOR",
        "MISSING_OOS_ACCESS_PURPOSE",
        "INVALID_CANDIDATE_EVALUATION_DIGEST",
    } <= set(str(invalid.value).split(";"))


def test_oos_promotion_and_rollback_detect_binding_failures(tmp_path: Path) -> None:
    from beidou_research.experiments.oos_seal import OOSAccessReceipt, OOSAuditNotVerifiable, OOSBoundary
    from tests.research.test_pit_lineage_and_oos_seal import (
        AUDIT_KEY,
        KEY,
        _boundary,
        _evaluation_digest,
        _seal_store,
    )

    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-01T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="evaluate",
    )
    mutated = OOSBoundary(
        train_end="2023-05-31T23:59:59Z",
        oos_start="2023-06-01T00:00:00Z",
        oos_end="2023-12-31T23:59:00Z",
        timezone="UTC",
    )
    status = store.promotion_status(
        seal=seal,
        receipt=replace(receipt, seal_digest="0" * 64),
        boundary=mutated,
        key=KEY,
        audit_key=b"z" * 32,
        candidate_evaluation_digest=_evaluation_digest(identity),
    )
    assert status.status == "INVALIDATED"
    assert {"OOS_SEAL_RECEIPT_MISMATCH", "MUTATED_OOS_BOUNDARY", "OOS_AUDIT_ANCHOR_MISMATCH"} <= set(status.reasons)

    raw = json.loads(store.audit_path.read_text())
    raw["accessed_at"] = "invalid"
    core = dict(raw)
    core.pop("event_hash")
    raw["event_hash"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    store.audit_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    invalid_time = store.promotion_status(
        seal=seal,
        receipt=OOSAccessReceipt(seal.digest, 1, raw["event_hash"]),
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        candidate_evaluation_digest=_evaluation_digest(identity),
    )
    assert "INVALID_OOS_ACCESS_TIMESTAMP" in invalid_time.reasons

    with pytest.raises(OOSAuditNotVerifiable, match="ROLLBACK_EVIDENCE_BINDING_MISMATCH"):
        store.rollback_to_research_only(seal=seal, receipt=replace(receipt, audit_head="0" * 64))


def test_promotion_gate_reports_runner_lineage_and_oos_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.data.pit_lineage as pit
    from beidou_research.experiments.promotion import validate_promotable_experiment

    missing = validate_promotable_experiment(runner_result=None)
    assert {"MISSING_RUNNER_RESULT", "MISSING_PIT_LINEAGE", "MISSING_OOS_SEAL"} <= set(missing.reasons)
    incomplete = validate_promotable_experiment(runner_result={"status": "FAILED"})
    assert "RUNNER_NOT_COMPLETED" in incomplete.reasons

    lineage = SimpleNamespace(as_dict=lambda: {}, digest="lineage")
    identity = SimpleNamespace(run_id="run", digest="identity")
    checkpoint = SimpleNamespace(digest="checkpoint")
    report = SimpleNamespace(status="NOT_VERIFIABLE", reasons=("BAD_LINEAGE",))
    monkeypatch.setattr(pit, "inspect_lineage", lambda *_args, **_kwargs: report)
    monkeypatch.setattr(pit, "require_experiment_binding", lambda *_args: None)
    oos_store = SimpleNamespace(
        load_seal=lambda: object(),
        promotion_status=lambda **_kwargs: SimpleNamespace(status="INVALIDATED", reasons=("BAD_OOS",)),
    )
    seal = SimpleNamespace(
        lineage_digest="wrong",
        run_id="wrong",
        experiment_identity_digest="wrong",
        checkpoint_digest="wrong",
    )
    decision = validate_promotable_experiment(
        runner_result={"status": "COMPLETED", "run_id": "wrong", "identity_digest": "identity"},
        lineage_manifest=lineage,
        lineage_root=Path("."),
        identity=identity,
        checkpoint=checkpoint,
        oos_store=oos_store,
        oos_seal=seal,
        oos_receipt=object(),
        oos_boundary=object(),
        boundary_key=b"k",
        audit_key=b"a",
    )
    assert {
        "PIT:BAD_LINEAGE",
        "RUNNER_RUN_BINDING_MISMATCH",
        "IMMUTABLE_OOS_SEAL_MISMATCH",
        "OOS_EXPERIMENT_BINDING_MISMATCH",
        "OOS:BAD_OOS",
    } <= set(decision.reasons)

    monkeypatch.setattr(pit, "require_experiment_binding", Mock(side_effect=RuntimeError("binding")))
    oos_store.load_seal = Mock(side_effect=RuntimeError("oos"))
    failed = validate_promotable_experiment(
        runner_result={"status": "COMPLETED", "run_id": "run", "identity_digest": "identity"},
        lineage_manifest=lineage,
        lineage_root=Path("."),
        identity=identity,
        checkpoint=checkpoint,
        oos_store=oos_store,
        oos_seal=seal,
        oos_receipt=object(),
        oos_boundary=object(),
        boundary_key=b"k",
        audit_key=b"a",
    )
    assert any(reason.startswith("PIT_EXPERIMENT_BINDING_INVALID:RuntimeError") for reason in failed.reasons)
    assert any(reason.startswith("OOS_PROMOTION_BINDING_INVALID:RuntimeError") for reason in failed.reasons)


def test_frozen_candidate_and_marginal_contribution_reject_ambiguous_inputs() -> None:
    from beidou_research.mining.orchestrator import FrozenCandidateAttempt
    from beidou_research.mining.selection.marginal_contribution import recompute_marginal_contribution

    for value, expected in (
        ({"candidate_id": "", "evidence": {}}, "CANDIDATE_ID_REQUIRED"),
        ({"candidate_id": "c", "evidence": []}, "CANDIDATE_EVIDENCE_REQUIRED"),
        ({"candidate_id": "c", "evidence": {}, "rejection_reasons": [""]}, "INVALID_REJECTION_REASONS"),
    ):
        with pytest.raises(ValueError, match=expected):
            FrozenCandidateAttempt.from_dict(value)

    with pytest.raises(ValueError, match="RETURN_SAMPLES_INSUFFICIENT"):
        recompute_marginal_contribution([0.1], [0.2])
    with pytest.raises(ValueError, match="RETURN_VALUE_INVALID"):
        recompute_marginal_contribution([True, 0.1], [0.2, 0.3])
    with pytest.raises(ValueError, match="RETURN_VALUE_NON_FINITE"):
        recompute_marginal_contribution([0.0, float("nan")], [0.1, 0.2])


def test_remaining_lineage_checkpoint_and_ledger_conflicts(tmp_path: Path) -> None:
    import beidou_research.data.pit_lineage as pit
    from beidou_research.experiments.checkpoint import CheckpointNotVerifiable, CheckpointStore
    from beidou_research.experiments.store import NotVerifiable
    from tests.research.test_checkpoint_protocol import checkpoint, identity, ledger
    from tests.research.test_pit_lineage_and_oos_seal import _lineage

    lineage_root = tmp_path / "lineage"
    lineage_root.mkdir()
    manifest, root = _lineage(lineage_root)
    payload = manifest.as_dict()
    payload["artifacts"]["dataset"].pop("role")
    report = pit.inspect_lineage(payload, root=root)
    assert "INVALID_ARTIFACT:dataset" in report.reasons

    active_ledger = ledger(tmp_path / "checkpoint")
    current = checkpoint(active_ledger)
    store_root = tmp_path / "checkpoint-store"
    store = CheckpointStore(store_root, identity(), "writer-a", "fence-1")
    store.save(current)
    (store_root / "checkpoint-000002.json").write_text("immutable-conflict", encoding="utf-8")
    with pytest.raises(CheckpointNotVerifiable, match="IMMUTABLE_GENERATION_CONFLICT"):
        store.save(current)

    first = active_ledger.events()[0]
    duplicate = replace(first, sequence=2, previous_hash=first.event_hash)
    with pytest.raises(NotVerifiable, match="DUPLICATE_IDEMPOTENCY_KEY"):
        active_ledger._validate([first, duplicate])


def test_oos_remaining_storage_and_chain_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.experiments.oos_seal as oos
    from tests.research.test_pit_lineage_and_oos_seal import AUDIT_KEY, KEY, _boundary, _seal_store

    class _OffsetDateTime:
        @staticmethod
        def fromisoformat(_value: str) -> datetime:
            return datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1)))

    monkeypatch.setattr(oos, "datetime", _OffsetDateTime)
    with pytest.raises(ValueError, match="timestamp must be UTC"):
        oos._utc("2024-01-01T00:00:00Z")
    monkeypatch.undo()

    race_root = tmp_path / "race"
    race_store = oos.OOSSealStore(race_root)
    race_store.seal_path.write_text("occupied", encoding="utf-8")
    original_exists = Path.exists

    def hide_seal(path: Path) -> bool:
        if path == race_store.seal_path:
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", hide_seal)
    with pytest.raises(oos.OOSSealNotVerifiable, match="IMMUTABLE_OOS_SEAL_ALREADY_EXISTS"):
        race_store.create(
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            lineage_digest="1" * 64,
            run_id="run",
            experiment_identity_digest="2" * 64,
            checkpoint_digest="3" * 64,
            sealed_at="2024-01-01T00:00:00Z",
            evaluation_not_before="2024-02-01T00:00:00Z",
        )
    monkeypatch.undo()

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    _manifest, identity, checkpoint, store, seal = _seal_store(fixture_root)
    store.seal_path.write_text("[]", encoding="utf-8")
    with pytest.raises(oos.OOSSealNotVerifiable, match="MISSING_SEAL_FIELDS"):
        store.load_seal()
    store.seal_path.write_text(json.dumps(seal.as_dict(), sort_keys=True, separators=(",", ":")) + "\n")

    store.audit_path.write_text("placeholder", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path == store.audit_path:
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(oos.OOSAuditNotVerifiable, match="UNREADABLE_OOS_AUDIT"):
        store.audit_events()
    monkeypatch.undo()

    event = oos.OOSAccessEvent.build(
        audit_key=AUDIT_KEY,
        sequence=1,
        previous_hash="0" * 64,
        seal_digest=seal.digest,
        accessed_at="2024-02-01T00:00:00Z",
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        boundary_digest=seal.boundary_digest,
        purpose="evaluate",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="4" * 64,
        decision="ALLOWED_ONCE",
        reasons=(),
    )
    store.audit_path.write_text(" " + json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(oos.OOSAuditNotVerifiable, match="NON_CANONICAL_OOS_AUDIT_EVENT"):
        store.audit_events()
    second = oos.OOSAccessEvent.build(
        audit_key=AUDIT_KEY,
        sequence=2,
        previous_hash="wrong",
        seal_digest=seal.digest,
        accessed_at="2024-02-01T00:00:00Z",
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        boundary_digest=seal.boundary_digest,
        purpose="evaluate",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="4" * 64,
        decision="ALLOWED_ONCE",
        reasons=(),
    )
    store.audit_path.write_text(
        json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":"))
        + "\n"
        + json.dumps(second.as_dict(), sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(oos.OOSAuditNotVerifiable, match="OOS_AUDIT_CHAIN_MISMATCH"):
        store.audit_events()


def test_resumable_runner_rejects_start_and_manifest_ambiguity(tmp_path: Path) -> None:
    from beidou_research.mining.runner import ResumableMiningRunner, ResumeRunError
    from tests.research.test_factor_miner_resume import create_interrupted, family, identity, rewrite_manifest

    with pytest.raises(TypeError, match="EXPERIMENT_RUN_IDENTITY_REQUIRED"):
        ResumableMiningRunner.start(
            tmp_path / "bad-identity",
            identity=object(),
            candidate_family=family(),
            writer_id="writer",
            fencing_token="fence",  # noqa: S106 - deterministic non-secret fixture token
        )
    with pytest.raises(ValueError, match="EMPTY_CANDIDATE_FAMILY"):
        ResumableMiningRunner.start(
            tmp_path / "empty",
            identity=identity("empty"),
            candidate_family=[],
            writer_id="writer",
            fencing_token="fence",  # noqa: S106 - deterministic non-secret fixture token
        )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "fact").write_text("preserve", encoding="utf-8")
    with pytest.raises(ResumeRunError, match="RUN_DIRECTORY_NOT_EMPTY"):
        ResumableMiningRunner.start(
            occupied,
            identity=identity("occupied"),
            candidate_family=family(),
            writer_id="writer",
            fencing_token="fence",  # noqa: S106 - deterministic non-secret fixture token
        )

    interrupted = create_interrupted(tmp_path / "inspect", "inspect")
    with pytest.raises(ResumeRunError, match="RUN_NOT_DURABLY_COMPLETED"):
        ResumableMiningRunner.inspect_completed(interrupted)

    mutations = {
        "not_object": None,
        "fields": lambda manifest: manifest.__setitem__("extra", True),
        "version": lambda manifest: manifest.__setitem__("schema_version", "9"),
        "identity": lambda manifest: manifest.__setitem__("identity_digest", "0" * 64),
    }
    for label, mutation in mutations.items():
        run_dir = create_interrupted(tmp_path / label, label)
        manifest_path = run_dir / "run-manifest.json"
        if mutation is None:
            manifest_path.write_text("[]\n", encoding="utf-8")
        else:
            rewrite_manifest(manifest_path, mutation)
        with pytest.raises(ResumeRunError, match="CORRUPT_OR_INCOMPATIBLE_RUN_MANIFEST"):
            ResumableMiningRunner._open_for_resume(run_dir)


def test_resumable_runner_detects_checkpoint_distance_and_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from beidou_research.experiments.checkpoint import CheckpointStore
    from beidou_research.mining.runner import ResumableMiningRunner, ResumeRunError
    from tests.research.test_factor_miner_resume import create_interrupted

    run_dir = create_interrupted(tmp_path / "distance", "distance")
    monkeypatch.setattr(
        CheckpointStore,
        "load",
        lambda self, **_kwargs: SimpleNamespace(event_sequence=999, digest="irrelevant"),
    )
    with pytest.raises(ResumeRunError, match="AMBIGUOUS_LEDGER_CHECKPOINT_DISTANCE"):
        ResumableMiningRunner._open_for_resume(run_dir)
    monkeypatch.undo()

    run_dir = create_interrupted(tmp_path / "divergence", "divergence")
    original_load = CheckpointStore.load

    def divergent_load(self: CheckpointStore, **kwargs: object) -> SimpleNamespace:
        actual = original_load(self, **kwargs)
        return SimpleNamespace(event_sequence=actual.event_sequence, digest="0" * 64)

    monkeypatch.setattr(CheckpointStore, "load", divergent_load)
    with pytest.raises(ResumeRunError, match="CHECKPOINT_STATE_DIVERGENCE"):
        ResumableMiningRunner._open_for_resume(run_dir)


def test_resumable_runner_event_invariants_and_atomic_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_research.mining.runner as runner_module
    from beidou_research.mining.runner import ResumableMiningRunner, ResumeRunError
    from tests.research.test_factor_miner_resume import create_interrupted

    target = tmp_path / "atomic.json"
    monkeypatch.setattr(runner_module.os, "replace", Mock(side_effect=OSError("replace")))
    with pytest.raises(OSError, match="replace"):
        runner_module._atomic_canonical_json(target, {"fact": True})
    assert not list(tmp_path.glob(".atomic.json.*"))
    monkeypatch.undo()

    run_dir = create_interrupted(tmp_path / "events", "events")
    runner, _behind = ResumableMiningRunner._open_for_resume(run_dir)
    with pytest.raises(ValueError, match="UNKNOWN_DURABLE_BOUNDARY"):
        runner._inject("unknown", "transition")
    with pytest.raises(ResumeRunError, match="MISSING_CREATED_EVENT"):
        runner._validate_events([])
    with pytest.raises(ResumeRunError, match="CANNOT_CHECKPOINT_EMPTY_LEDGER"):
        runner._checkpoint_for_events([])

    created_payload = {
        "family_hash": runner.family_hash,
        "candidate_ids": [candidate.candidate_id for candidate in runner.candidates],
        "multiple_testing_denominator": len(runner.candidates),
        "worker_count": 1,
        "scope": {"search": "TEMPLATE_GRID", "datasets": 1, "universes": 1, "timeframes": 1, "jobs": 1},
    }
    created = SimpleNamespace(stage="CREATED", payload_json=json.dumps(created_payload), idempotency_key="wrong")
    with pytest.raises(ResumeRunError, match="CREATED_EVENT_MISMATCH"):
        runner._validate_events([created])
    created.idempotency_key = "run-created"

    too_many = [created] + [SimpleNamespace(stage="RUNNING") for _ in range(len(runner.candidates) + 1)]
    with pytest.raises(ResumeRunError, match="TOO_MANY_CANDIDATE_ATTEMPTS"):
        runner._validate_events(too_many)

    bad_attempt = SimpleNamespace(stage="RUNNING", payload_json="{}", idempotency_key="bad")
    with pytest.raises(ResumeRunError, match="CANDIDATE_ATTEMPT_MISMATCH"):
        runner._validate_events([created, bad_attempt])

    attempts = []
    for index, candidate in enumerate(runner.candidates):
        attempts.append(
            SimpleNamespace(
                stage="RUNNING",
                payload_json=json.dumps(
                    {
                        "kind": "CANDIDATE_ATTEMPT",
                        **candidate.attempt_record(family_hash=runner.family_hash, attempt_index=index),
                    }
                ),
                idempotency_key=f"candidate-attempt:{index}:{candidate.candidate_id}",
            )
        )
    completed = SimpleNamespace(stage="COMPLETED", payload_json="{}", idempotency_key="run-completed")
    with pytest.raises(ResumeRunError, match="INVALID_COMPLETION_POSITION"):
        runner._validate_events([created, completed])
    with pytest.raises(ResumeRunError, match="FINAL_RESULT_MISMATCH"):
        runner._validate_events([created, *attempts, completed])
