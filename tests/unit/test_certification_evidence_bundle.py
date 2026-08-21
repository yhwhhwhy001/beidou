from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_certification.evidence_bundle import (
    EvidenceBundle,
    EvidenceItem,
    GateCertificate,
    GateResult,
    GateRunner,
    ScenarioStatus,
)
from beidou_research.mining.persistence import PostgreSQLFactorStore


def _complete_bundle() -> EvidenceBundle:
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        manifest={
            "repository": "yhwhhwhy001/beidou",
            "commit": "abc123",
            "dependency_lock_hash": "lock-hash",
            "container_digest": "sha256:container",
            "policy_version": "policy-v1",
            "dataset_manifest_hash": "dataset-hash",
            "account_environment": "TESTNET",
        },
    )
    bundle.add_evidence(
        EvidenceItem(
            name="runtime-facts",
            source_uri="evidence://runtime-facts",
            checksum="facts-hash",
            producer="test",
            commit="abc123",
        )
    )
    bundle.record_scenario("g0", ScenarioStatus.PASS, evidence_refs=["runtime-facts"])
    return bundle


def test_bundle_hash_binds_provenance_and_references() -> None:
    bundle = _complete_bundle()
    original = bundle.compute_bundle_hash()
    bundle.manifest["policy_version"] = "policy-v2"
    assert bundle.compute_bundle_hash() != original


def test_pass_bundle_requires_bound_evidence() -> None:
    bundle = _complete_bundle()
    assert bundle.validate() == (True, "OK")
    bundle.record_scenario("g1", ScenarioStatus.PASS)
    valid, reason = bundle.validate()
    assert not valid
    assert reason == "scenario_evidence_unbound:g1"


def test_postgres_factor_store_does_not_invent_localhost_authority(monkeypatch) -> None:
    monkeypatch.delenv("BEIDOU_DATABASE_URL", raising=False)
    store = PostgreSQLFactorStore(auto_connect=False)
    assert store.conn_string == ""
    assert not store._try_connect()
    assert store.last_error == "DATABASE_URL_UNKNOWN"


def test_bundle_rejects_incomplete_metadata_and_unbound_pass_evidence() -> None:
    bundle = EvidenceBundle(bundle_id="bundle-1", manifest={})
    with pytest.raises(ValueError, match="metadata"):
        bundle.add_evidence(EvidenceItem("", "uri", "hash", "producer", "commit"))
    with pytest.raises(ValueError, match="scenario_id"):
        bundle.record_scenario("", ScenarioStatus.PASS)
    assert bundle.validate() == (False, "manifest_missing")

    bundle.manifest = {
        "repository": "repo",
        "commit": "commit",
        "dependency_lock_hash": "lock",
        "container_digest": "container",
        "policy_version": "policy",
        "dataset_manifest_hash": "dataset",
        "account_environment": "TESTNET",
    }
    bundle.add_evidence(EvidenceItem("evidence", "uri", "hash", "producer", "commit"))
    bundle.record_scenario("scenario", ScenarioStatus.PASS, evidence_refs=["missing"])
    assert bundle.validate() == (False, "scenario_evidence_unbound:scenario")
    bundle.scenario_results["scenario"] = ScenarioStatus.FAIL
    assert bundle.validate() == (True, "OK")
    assert bundle.is_complete()

    identity_missing = _complete_bundle()
    identity_missing.bundle_id = ""
    assert identity_missing.validate() == (False, "bundle_identity_missing")
    context_missing = _complete_bundle()
    context_missing.manifest["policy_version"] = ""
    assert context_missing.validate() == (False, "manifest_context_incomplete")
    item_missing = _complete_bundle()
    item_missing.evidence_items[0].producer = ""
    assert item_missing.validate() == (False, "evidence_item_metadata_incomplete")


def test_gate_runner_distinguishes_missing_failure_and_not_verifiable_scenarios() -> None:
    runner = GateRunner()
    assert runner.run_gate("unknown", {}) is GateResult.NOT_VERIFIABLE
    assert runner.run_gate("G0", {}) is GateResult.NOT_VERIFIABLE
    assert runner.run_gate("G0", {"lint": ScenarioStatus.FAIL, "typecheck": ScenarioStatus.PASS}) is GateResult.FAIL
    assert runner.has_p0_blocker()
    assert (
        runner.run_gate("G0", {"lint": ScenarioStatus.NOT_VERIFIABLE, "typecheck": ScenarioStatus.PASS})
        is GateResult.NOT_VERIFIABLE
    )
    assert runner.run_gate("G0", {"lint": ScenarioStatus.PASS, "typecheck": ScenarioStatus.PASS}) is GateResult.PASS


def test_gate_certificate_requires_all_context_and_time_validity() -> None:
    base = {
        "gate_id": "G5",
        "result": GateResult.PASS,
        "bundle_hash": "bundle-hash",
        "repository": "repo",
        "commit": "commit",
        "dependency_lock_hash": "lock",
        "container_digest": "container",
        "policy_version": "policy",
        "dataset_manifest_hash": "dataset",
        "account_environment": "TESTNET",
        "valid_from": datetime.now(timezone.utc).isoformat(),
        "signature": "signature",
    }
    certificate = GateCertificate(**base)
    assert certificate.is_valid()
    certificate.revoked = True
    assert not certificate.is_valid()
    certificate.revoked = False
    certificate.signature = ""
    assert not certificate.is_valid()
    certificate.signature = "signature"
    certificate.valid_until = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert not certificate.is_valid()
    certificate.valid_until = "not-a-date"
    assert not certificate.is_valid()
    certificate.valid_until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert certificate.is_valid()
    certificate.result = GateResult.FAIL
    assert not certificate.is_valid()


def test_gate_runner_certificate_is_not_verifiable_without_complete_provenance() -> None:
    bundle = _complete_bundle()
    bundle.record_scenario("lint", ScenarioStatus.PASS, evidence_refs=["runtime-facts"])
    bundle.record_scenario("typecheck", ScenarioStatus.PASS, evidence_refs=["runtime-facts"])
    certificate = GateRunner().generate_certificate("G0", bundle, "abc123")
    assert certificate.result is GateResult.PASS
    assert certificate.bundle_hash == bundle.compute_bundle_hash()

    incomplete = EvidenceBundle(bundle_id="incomplete", manifest=bundle.manifest.copy())
    incomplete.record_scenario("lint", ScenarioStatus.PASS)
    incomplete.record_scenario("typecheck", ScenarioStatus.PASS)
    blocked = GateRunner().generate_certificate("G0", incomplete, "abc123")
    assert blocked.result is GateResult.NOT_VERIFIABLE
