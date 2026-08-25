"""Adversarial acceptance proof for content-addressed PIT lineage and sealed OOS."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from beidou_research.data.pit_lineage import (
    REQUIRED_LINEAGE_ROLES,
    LineageNotVerifiable,
    build_lineage_artifact,
    build_pit_manifest,
    inspect_lineage,
    load_pit_manifest,
    require_experiment_binding,
)
from beidou_research.experiments import Checkpoint, ExperimentRunIdentity
from beidou_research.experiments.contracts import canonical_json
from beidou_research.experiments.oos_seal import (
    OOSAccessInvalidated,
    OOSAccessReceipt,
    OOSAuditNotVerifiable,
    OOSBoundary,
    OOSSealStore,
)
from beidou_research.experiments.promotion import validate_promotable_experiment

AS_OF = "2024-01-01T00:00:00Z"
KEY = b"0123456789abcdef0123456789abcdef"
AUDIT_KEY = b"abcdef0123456789abcdef0123456789"


def _evidence_dir(tmp_path: Path) -> Path:
    path = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", str(tmp_path / "evidence")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lineage(tmp_path: Path):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    artifacts = {}
    for role in sorted(REQUIRED_LINEAGE_ROLES):
        path = snapshots / f"{role}.json"
        path.write_text(json.dumps({"role": role, "version": 1}, sort_keys=True) + "\n")
        source_id = "BTCUSDT" if role == "universe_membership" else f"{role}-snapshot-2024-01-01"
        event_start = "2020-01-01T00:00:00Z"
        event_end = "2023-12-31T22:00:00Z" if role == "features" else "2023-12-31T23:59:00Z"
        if role == "labels":
            event_start = "2023-12-31T23:00:00Z"
        artifacts[role] = build_lineage_artifact(
            role=role,
            path=path,
            root=tmp_path,
            source_id=source_id,
            revision_id=f"revision-{role}-2024-01-01",
            available_as_of=AS_OF,
            event_time_start=event_start,
            event_time_end=event_end,
            timezone="UTC",
            point_in_time=True,
        )
    return build_pit_manifest(as_of=AS_OF, artifacts=artifacts, root=tmp_path), tmp_path


def _identity_and_checkpoint(manifest):
    artifacts = manifest.artifacts
    identity = ExperimentRunIdentity(
        run_id="run-pit-oos-001",
        code_commit="a" * 40,
        code_tree_digest="b" * 64,
        policy_digest="c" * 64,
        dataset_manifest_digest=artifacts["dataset"].artifact_digest,
        pit_manifest_digest=manifest.digest,
        universe="BTCUSDT",
        timeframe="1h",
        feature_digest=artifacts["features"].artifact_digest,
        label_digest=artifacts["labels"].artifact_digest,
        cost_model=artifacts["costs"].artifact_digest,
        seed=7,
    )
    checkpoint = Checkpoint.build(
        identity=identity,
        event_sequence=1,
        previous_hash="0" * 64,
        event_hash="1" * 64,
        rng_state={"python": [1]},
        search_state={"cursor": 1},
        optimizer_state={"trials": []},
        candidate_queue=["candidate-1"],
        completed_candidate_ids=[],
        multiple_testing_family="family-1",
        multiple_testing_denominator=1,
        stage_cursor="RUNNING",
        worker_count=1,
        scope={"search": "TEMPLATE_GRID", "datasets": 1, "universes": 1, "timeframes": 1, "jobs": 1},
        idempotency_keys=["event-1"],
        writer_id="writer-a",
        fencing_token="fence-1",  # noqa: S106 - deterministic non-secret fixture token
    )
    return identity, checkpoint


def _boundary() -> OOSBoundary:
    return OOSBoundary(
        train_end="2023-06-30T23:59:59Z",
        oos_start="2023-07-01T00:00:00Z",
        oos_end="2023-12-31T23:59:00Z",
        timezone="UTC",
    )


def _runner_result(identity: ExperimentRunIdentity) -> dict[str, str]:
    return {"run_id": identity.run_id, "identity_digest": identity.digest, "status": "COMPLETED"}


def _evaluation_digest(identity: ExperimentRunIdentity) -> str:
    return hashlib.sha256(canonical_json(_runner_result(identity)).encode()).hexdigest()


def _seal_store(tmp_path: Path):
    manifest, _ = _lineage(tmp_path)
    identity, checkpoint = _identity_and_checkpoint(manifest)
    store = OOSSealStore(tmp_path / "sealed-oos")
    seal = store.create(
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        lineage_digest=manifest.digest,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        sealed_at="2024-01-01T00:00:00Z",
        evaluation_not_before="2024-02-01T00:00:00Z",
    )
    return manifest, identity, checkpoint, store, seal


def _promotion_status(store: OOSSealStore, seal, receipt):
    return store.promotion_status(
        seal=seal,
        receipt=receipt,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        candidate_evaluation_digest=hashlib.sha256(
            canonical_json(
                {"run_id": seal.run_id, "identity_digest": seal.experiment_identity_digest, "status": "COMPLETED"}
            ).encode()
        ).hexdigest(),
    )


def _rehash_artifact(payload: dict, role: str) -> None:
    artifact = payload["artifacts"][role]
    core = dict(artifact)
    core.pop("artifact_digest", None)
    artifact["artifact_digest"] = hashlib.sha256(canonical_json(core).encode()).hexdigest()
    core_manifest = dict(payload)
    core_manifest.pop("manifest_digest", None)
    payload["manifest_digest"] = hashlib.sha256(canonical_json(core_manifest).encode()).hexdigest()


def test_promotable_lineage_has_100_percent_field_binding_and_recomputes(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    report = inspect_lineage(manifest.as_dict(), root=root, expected_manifest_digest=manifest.digest)
    assert report.status == "VERIFIABLE"
    assert report.covered_roles == tuple(sorted(REQUIRED_LINEAGE_ROLES))
    assert report.coverage_percent == 100
    loaded = load_pit_manifest(manifest.as_dict(), root=root, expected_manifest_digest=manifest.digest)
    assert loaded.digest == manifest.digest
    with pytest.raises(TypeError):
        loaded.artifacts["dataset"] = loaded.artifacts["labels"]  # type: ignore[index]

    evidence = _evidence_dir(tmp_path)
    (evidence / "pit-lineage-manifest.json").write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n")
    (evidence / "lineage-tamper-report.json").write_text(
        json.dumps({"status": report.status, "independent_recomputed_digest": report.recomputed_digest}, indent=2)
        + "\n"
    )


def test_known_feature_label_leakage_fails_closed(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    payload["artifacts"]["features"]["event_time_end"] = "2023-12-31T23:30:00Z"
    _rehash_artifact(payload, "features")
    report = inspect_lineage(payload, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "FEATURE_LABEL_WINDOW_LEAKAGE" in report.reasons


def test_late_revision_fails_closed_even_with_recomputed_manifest(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    payload["artifacts"]["revisions"]["available_as_of"] = "2024-01-02T00:00:00Z"
    _rehash_artifact(payload, "revisions")
    report = inspect_lineage(payload, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "AVAILABLE_AFTER_AS_OF:revisions" in report.reasons


def test_survivorship_biased_universe_fails_closed(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    payload["artifacts"]["universe_membership"]["point_in_time"] = False
    _rehash_artifact(payload, "universe_membership")
    report = inspect_lineage(payload, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "NOT_POINT_IN_TIME:universe_membership" in report.reasons


def test_non_utc_or_naive_timezone_fails_closed(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    payload["artifacts"]["timestamps"]["timezone"] = "Asia/Shanghai"
    _rehash_artifact(payload, "timestamps")
    report = inspect_lineage(payload, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "NON_UTC_TIMEZONE:timestamps" in report.reasons


def test_mutable_file_fails_closed_after_manifest_creation(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    (root / manifest.artifacts["dataset"].source_path).write_text("mutated\n")
    report = inspect_lineage(manifest.as_dict(), root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "CONTENT_DIGEST_MISMATCH:dataset" in report.reasons


def test_changed_universe_cannot_bind_experiment_or_checkpoint(tmp_path: Path) -> None:
    manifest, _ = _lineage(tmp_path)
    identity, checkpoint = _identity_and_checkpoint(manifest)
    changed = replace(identity, universe="ETHUSDT")
    with pytest.raises(LineageNotVerifiable, match="UNIVERSE_BINDING_MISMATCH"):
        require_experiment_binding(manifest, changed, checkpoint)


def test_forged_manifest_digest_fails_closed(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    payload = manifest.as_dict()
    payload["manifest_digest"] = "0" * 64
    report = inspect_lineage(payload, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "MANIFEST_DIGEST_MISMATCH" in report.reasons


def test_missing_lineage_and_latest_default_provenance_are_not_verifiable(tmp_path: Path) -> None:
    manifest, root = _lineage(tmp_path)
    missing = manifest.as_dict()
    missing["artifacts"].pop("corporate_actions")
    report = inspect_lineage(missing, root=root)
    assert report.status == "NOT_VERIFIABLE"
    assert "MISSING_ROLES:corporate_actions" in report.reasons

    latest = manifest.as_dict()
    latest["artifacts"]["calendar"]["revision_id"] = "latest"
    _rehash_artifact(latest, "calendar")
    report = inspect_lineage(latest, root=root)
    assert "AMBIGUOUS_PROVENANCE:calendar" in report.reasons


def test_lineage_is_bound_to_experiment_run_and_checkpoint(tmp_path: Path) -> None:
    manifest, _ = _lineage(tmp_path)
    identity, checkpoint = _identity_and_checkpoint(manifest)
    binding = require_experiment_binding(manifest, identity, checkpoint)
    assert binding["status"] == "BOUND"
    assert binding["checkpoint_digest"] == checkpoint.digest


def test_oos_seal_is_opaque_and_fixed_before_candidate_evaluation(tmp_path: Path) -> None:
    _manifest, _identity, _checkpoint, store, seal = _seal_store(tmp_path)
    persisted = json.loads(store.seal_path.read_text())
    assert seal.sealed_at < seal.evaluation_not_before
    assert not ({"train_end", "oos_start", "oos_end"} & set(persisted))
    assert all(
        timestamp not in store.seal_path.read_text() for timestamp in _boundary().as_dict().values() if "T" in timestamp
    )


def test_early_oos_access_is_audited_and_invalidates(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    with pytest.raises(OOSAccessInvalidated, match="EARLY_OOS_ACCESS"):
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="2024-01-15T00:00:00Z",
            candidate_evaluation_complete=False,
            candidate_evaluation_digest=_evaluation_digest(identity),
            purpose="candidate-debug",
        )
    assert store.audit_events()[0].decision == "DENIED"


def test_repeated_oos_access_invalidates_promotion(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    first = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    assert _promotion_status(store, seal, first).status == "PROMOTABLE"
    with pytest.raises(OOSAccessInvalidated, match="REPEATED_OOS_ACCESS"):
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="2024-02-03T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest=_evaluation_digest(identity),
            purpose="rerun-after-seeing-oos",
        )
    assert _promotion_status(store, seal, first).status == "INVALIDATED"


def test_mutated_oos_boundary_is_audited_and_invalidates(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    mutated = replace(_boundary(), oos_start="2023-08-01T00:00:00Z")
    with pytest.raises(OOSAccessInvalidated, match="MUTATED_OOS_BOUNDARY"):
        store.access(
            seal=seal,
            boundary=mutated,
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id=identity.run_id,
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="2024-02-02T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest=_evaluation_digest(identity),
            purpose="mutated-split",
        )
    assert "MUTATED_OOS_BOUNDARY" in store.audit_events()[0].reasons


def test_unbound_oos_access_is_audited_and_invalidates(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    with pytest.raises(OOSAccessInvalidated, match="UNBOUND_OOS_ACCESS"):
        store.access(
            seal=seal,
            boundary=_boundary(),
            key=KEY,
            audit_key=AUDIT_KEY,
            run_id="other-run",
            experiment_identity_digest=identity.digest,
            checkpoint_digest=checkpoint.digest,
            accessed_at="2024-02-02T00:00:00Z",
            candidate_evaluation_complete=True,
            candidate_evaluation_digest=_evaluation_digest(identity),
            purpose="unbound-run",
        )
    assert store.audit_events()[0].decision == "DENIED"


def test_valid_access_audit_and_seal_evidence_are_independently_recomputable(tmp_path: Path) -> None:
    manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    status = _promotion_status(store, seal, receipt)
    assert status.status == "PROMOTABLE"
    assert status.synthetic_economic_evidence is False
    assert store.load_seal().digest == seal.digest

    evidence = _evidence_dir(tmp_path)
    (evidence / "oos-seal.json").write_text(json.dumps(seal.as_dict(), indent=2, sort_keys=True) + "\n")
    (evidence / "oos-access-audit.json").write_text(
        json.dumps([event.as_dict() for event in store.audit_events()], indent=2, sort_keys=True) + "\n"
    )
    assert require_experiment_binding(manifest, identity, checkpoint)["status"] == "BOUND"


def test_audit_tamper_fails_closed(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    payload = json.loads(store.audit_path.read_text().splitlines()[0])
    payload["purpose"] = "tampered"
    store.audit_path.write_text(json.dumps(payload) + "\n")
    with pytest.raises(OOSAuditNotVerifiable):
        store.audit_events()


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("early", "EARLY_OOS_ACCESS"),
        ("incomplete", "CANDIDATE_EVALUATION_NOT_COMPLETE"),
        ("unbound", "OOS_AUDIT_BINDING_MISMATCH"),
        ("mutated_boundary", "MUTATED_OOS_BOUNDARY"),
    ],
)
def test_forged_audit_and_self_issued_receipt_cannot_promote(
    tmp_path: Path, mutation: str, expected_reason: str
) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    forged = json.loads(store.audit_path.read_text())
    if mutation == "early":
        forged["accessed_at"] = "2024-01-02T00:00:00Z"
    elif mutation == "incomplete":
        forged["candidate_evaluation_complete"] = False
    elif mutation == "unbound":
        forged["run_id"] = "forged-run"
    else:
        forged["boundary_digest"] = "0" * 64
    forged["decision"] = "ALLOWED_ONCE"
    forged["reasons"] = []
    core = dict(forged)
    core.pop("event_hash")
    forged["event_hash"] = hashlib.sha256(canonical_json(core).encode()).hexdigest()
    store.audit_path.write_text(canonical_json(forged) + "\n")
    forged_receipt = OOSAccessReceipt(seal_digest=seal.digest, sequence=1, audit_head=forged["event_hash"])

    status = _promotion_status(store, seal, forged_receipt)
    assert status.status == "INVALIDATED"
    assert expected_reason in status.reasons
    assert "OOS_AUDIT_AUTHENTICATION_MISMATCH" in status.reasons


def test_completed_research_runner_without_lineage_or_seal_is_non_promotable(tmp_path: Path) -> None:
    from beidou_research.mining.runner import ResumableMiningRunner

    result = ResumableMiningRunner.start(
        tmp_path / "research-only",
        identity=ExperimentRunIdentity(
            run_id="research-only",
            code_commit="a" * 40,
            code_tree_digest="b" * 64,
            policy_digest="c" * 64,
            dataset_manifest_digest="d" * 64,
            pit_manifest_digest="e" * 64,
            universe="BTCUSDT",
            timeframe="1h",
            feature_digest="f" * 64,
            label_digest="0" * 64,
            cost_model="unbound-cost",
            seed=7,
        ),
        candidate_family=[{"candidate_id": "candidate-1", "evidence": {"score": 0.1}}],
        writer_id="writer-a",
        fencing_token="fence-1",  # noqa: S106 - deterministic non-secret fixture token
    )
    assert result["status"] == "COMPLETED"
    decision = validate_promotable_experiment(runner_result=result)
    assert decision.status == "NOT_VERIFIABLE"
    assert "MISSING_PIT_LINEAGE" in decision.reasons
    assert "MISSING_OOS_SEAL" in decision.reasons


def test_real_promotion_gate_requires_all_runner_checkpoint_lineage_and_seal_bindings(tmp_path: Path) -> None:
    manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    runner_result = _runner_result(identity)
    decision = validate_promotable_experiment(
        runner_result=runner_result,
        lineage_manifest=manifest,
        lineage_root=tmp_path,
        identity=identity,
        checkpoint=checkpoint,
        oos_store=store,
        oos_seal=seal,
        oos_receipt=receipt,
        oos_boundary=_boundary(),
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
    )
    assert decision.status == "PROMOTABLE"
    assert decision.promotable is True
    assert decision.synthetic_economic_evidence is False

    mismatch = validate_promotable_experiment(
        runner_result={**runner_result, "identity_digest": "9" * 64},
        lineage_manifest=manifest,
        lineage_root=tmp_path,
        identity=identity,
        checkpoint=replace(checkpoint, checkpoint_digest="8" * 64),
        oos_store=store,
        oos_seal=seal,
        oos_receipt=receipt,
        oos_boundary=_boundary(),
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
    )
    assert mismatch.status == "NOT_VERIFIABLE"
    assert mismatch.promotable is False
    assert "RUNNER_IDENTITY_BINDING_MISMATCH" in mismatch.reasons
    assert "OOS_EXPERIMENT_BINDING_MISMATCH" in mismatch.reasons


def test_rollback_preserves_seal_and_audit_but_is_non_promotable(tmp_path: Path) -> None:
    _manifest, identity, checkpoint, store, seal = _seal_store(tmp_path)
    receipt = store.access(
        seal=seal,
        boundary=_boundary(),
        key=KEY,
        audit_key=AUDIT_KEY,
        run_id=identity.run_id,
        experiment_identity_digest=identity.digest,
        checkpoint_digest=checkpoint.digest,
        accessed_at="2024-02-02T00:00:00Z",
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=_evaluation_digest(identity),
        purpose="sealed-evaluation",
    )
    before_seal = store.seal_path.read_bytes()
    before_audit = store.audit_path.read_bytes()
    rollback = store.rollback_to_research_only(seal=seal, receipt=receipt)
    assert rollback["status"] == "NON_PROMOTABLE_RESEARCH_ONLY"
    assert rollback["preserved_audit_head"] == receipt.audit_head
    assert store.seal_path.read_bytes() == before_seal
    assert store.audit_path.read_bytes() == before_audit
