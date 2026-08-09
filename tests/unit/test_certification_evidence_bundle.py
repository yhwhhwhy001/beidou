from __future__ import annotations

from beidou_certification.evidence_bundle import EvidenceBundle, EvidenceItem, ScenarioStatus
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
