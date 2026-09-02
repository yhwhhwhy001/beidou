"""ARO-06B: governed OOS v2 preseal, identity, crash, and eligibility contracts."""

from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from beidou_research.experiments import (
    ExperimentRunIdentity,
    GovernedOOSAccessReceiptV2,
    GovernedOOSSealStoreV2,
    GovernedOOSSealV2,
    GovernedPromotionDecisionV2,
    OOSAccessInvalidated,
    OOSAccessReceipt,
    OOSBoundary,
    OOSCandidateFreezeV2,
    OOSGovernanceV2Error,
    OOSPresealAssessmentV2,
    OOSSealStore,
    OOSWindowCustodySpecV2,
    assess_oos_preseal_v2,
    require_preseal_identity_binding_v2,
)

KEY = b"0123456789abcdef0123456789abcdef"
AUDIT_KEY = b"abcdef0123456789abcdef0123456789"
SEALED_AT = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
FROZEN_AT = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
GOVERNED_AT = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
READ_AT = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
CANDIDATE_EVALUATION_DIGEST = "7" * 64


def _window(**changes: object) -> OOSWindowCustodySpecV2:
    values = {
        "boundary": OOSBoundary(
            train_end="2026-08-31T23:59:59Z",
            oos_start="2026-09-01T00:00:00Z",
            oos_end="2026-09-30T23:00:00Z",
            timezone="UTC",
        ),
        "venue": "BINANCE_USDM",
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "interval": "1h",
        "source_endpoint": (
            "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-09.zip"
        ),
        "source_contract_digest": "1" * 64,
        "dataset_manifest_digest": "2" * 64,
        "pit_manifest_digest": "3" * 64,
        "custodian_id": "metric-custodian-a",
        "custody_sealed_at": SEALED_AT,
        "first_read_not_before": READ_AT,
    }
    return OOSWindowCustodySpecV2(**(values | changes))


def _candidate(**changes: object) -> OOSCandidateFreezeV2:
    values = {
        "code_commit": "a" * 40,
        "code_tree_digest": "b" * 64,
        "strategy_policy_digest": "c" * 64,
        "metric_policy_digest": "d" * 64,
        "cost_policy_digest": "e" * 64,
        "metric_owner_id": "metric-owner-a",
        "candidate_frozen_at": FROZEN_AT,
    }
    return OOSCandidateFreezeV2(**(values | changes))


def _ready():
    result = assess_oos_preseal_v2(window=_window(), candidate=_candidate())
    assert result.status == "READY_FOR_V2_SEAL"
    return result


def _identity(preseal=None, **changes: object) -> ExperimentRunIdentity:
    preseal = preseal or _ready()
    values = {
        "run_id": "alpha-oos-v2-run",
        "code_commit": "a" * 40,
        "code_tree_digest": "b" * 64,
        "policy_digest": preseal.identity_policy_digest,
        "dataset_manifest_digest": "2" * 64,
        "pit_manifest_digest": "3" * 64,
        "universe": "BINANCE_USDM|BTCUSDT,ETHUSDT",
        "timeframe": "1h",
        "feature_digest": "4" * 64,
        "label_digest": "5" * 64,
        "cost_model": "e" * 64,
        "seed": 7,
    }
    return ExperimentRunIdentity(**(values | changes))


def _created_store(
    root: Path,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[GovernedOOSSealStoreV2, OOSPresealAssessmentV2, ExperimentRunIdentity, GovernedOOSSealV2]:
    ready = _ready()
    identity = _identity(ready)
    store = GovernedOOSSealStoreV2(root, fault_injector=fault_injector)
    seal = store.create(
        preseal=ready,
        identity=identity,
        checkpoint_digest="6" * 64,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        governed_at=GOVERNED_AT,
    )
    return store, ready, identity, seal


def _access_once(
    store: GovernedOOSSealStoreV2,
    ready: OOSPresealAssessmentV2,
    identity: ExperimentRunIdentity,
    seal: GovernedOOSSealV2,
) -> GovernedOOSAccessReceiptV2:
    return store.access(
        seal=seal,
        preseal=ready,
        identity=identity,
        checkpoint_digest="6" * 64,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        accessed_at=READ_AT,
        candidate_evaluation_complete=True,
        candidate_evaluation_digest=CANDIDATE_EVALUATION_DIGEST,
        purpose="sealed-one-shot-evaluation",
    )


def _governed_race_worker(root: str, start: Any, results: Any) -> None:
    ready = _ready()
    identity = _identity(ready)
    store = GovernedOOSSealStoreV2(root)
    seal = store.load_governed_seal()
    original_append = store.legacy_store._append

    def slow_append(event: Any) -> None:
        time.sleep(0.2)
        original_append(event)

    store.legacy_store._append = slow_append  # type: ignore[method-assign]
    start.wait(timeout=10)
    try:
        receipt = _access_once(store, ready, identity, seal)
        results.put(("SUCCESS", receipt.legacy_sequence, receipt.legacy_audit_head))
    except OOSAccessInvalidated as exc:
        results.put(("DENIED", str(exc), ""))
    except Exception as exc:  # pragma: no cover - surfaced in parent assertion
        results.put(("ERROR", type(exc).__name__, str(exc)))


def test_missing_preseal_inputs_are_versioned_draft_blockers() -> None:
    draft = assess_oos_preseal_v2(window=None, candidate=None)
    assert draft.status == "DRAFT_BLOCKED"
    assert draft.eligible_for_v2_seal is False
    assert draft.reasons == ("MISSING_WINDOW_CUSTODY_SPEC", "MISSING_CANDIDATE_FREEZE")
    assert len(draft.assessment_digest) == 64
    with pytest.raises(OOSGovernanceV2Error, match="PRESEAL_NOT_READY"):
        draft.require_ready()


def test_preseal_digest_is_deterministic_domain_bound_and_mutation_sensitive() -> None:
    ready = _ready()
    assert ready.preseal_digest == _ready().preseal_digest
    changed_window = assess_oos_preseal_v2(window=_window(custodian_id="metric-custodian-b"), candidate=_candidate())
    changed_candidate = assess_oos_preseal_v2(window=_window(), candidate=_candidate(metric_policy_digest="f" * 64))
    assert changed_window.preseal_digest != ready.preseal_digest
    assert changed_candidate.preseal_digest != ready.preseal_digest
    assert changed_candidate.identity_policy_digest != ready.identity_policy_digest


def test_every_window_and_candidate_field_is_transitively_digest_bound() -> None:
    window = _window()
    candidate = _candidate()
    changed_windows = (
        _window(
            boundary=OOSBoundary(
                train_end="2026-08-31T22:59:59Z",
                oos_start="2026-09-01T00:00:00Z",
                oos_end="2026-09-30T23:00:00Z",
                timezone="UTC",
            )
        ),
        _window(symbols=("BTCUSDT", "SOLUSDT")),
        _window(interval="4h"),
        _window(
            source_endpoint=(
                "https://data.binance.vision/data/futures/um/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-2026-09.zip"
            )
        ),
        _window(source_contract_digest="8" * 64),
        _window(dataset_manifest_digest="8" * 64),
        _window(pit_manifest_digest="8" * 64),
        _window(custodian_id="metric-custodian-b"),
        _window(custody_sealed_at=datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)),
        _window(first_read_not_before=datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)),
    )
    changed_candidates = (
        _candidate(code_commit="f" * 40),
        _candidate(code_tree_digest="f" * 64),
        _candidate(strategy_policy_digest="f" * 64),
        _candidate(metric_policy_digest="f" * 64),
        _candidate(cost_policy_digest="f" * 64),
        _candidate(metric_owner_id="metric-owner-b"),
        _candidate(candidate_frozen_at=datetime(2026, 9, 1, 1, 0, 1, tzinfo=timezone.utc)),
    )
    assert all(changed.digest != window.digest for changed in changed_windows)
    assert all(changed.digest != candidate.digest for changed in changed_candidates)


def test_preseal_chronology_is_fail_closed() -> None:
    before_custody = assess_oos_preseal_v2(
        window=_window(),
        candidate=_candidate(candidate_frozen_at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)),
    )
    at_first_read = assess_oos_preseal_v2(
        window=_window(),
        candidate=_candidate(candidate_frozen_at=READ_AT),
    )
    assert before_custody.reasons == ("CANDIDATE_FROZEN_BEFORE_CUSTODY",)
    assert at_first_read.reasons == ("CANDIDATE_NOT_FROZEN_BEFORE_FIRST_READ",)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://demo-fapi.binance.com/fapi/v1/klines",
        "https://testnet.binancefuture.com/fapi/v1/klines",
        "https://unknown.example/fapi/v1/klines",
    ],
)
def test_execution_only_demo_testnet_and_unknown_sources_cannot_be_ready(endpoint: str) -> None:
    with pytest.raises(OOSGovernanceV2Error, match="OOS_SOURCE_NOT_ECONOMIC_RESEARCH"):
        _window(source_endpoint=endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"code_commit": "f" * 40},
        {"code_tree_digest": "f" * 64},
        {"policy_digest": "f" * 64},
        {"dataset_manifest_digest": "f" * 64},
        {"pit_manifest_digest": "f" * 64},
        {"universe": "BINANCE_USDM|BTCUSDT"},
        {"timeframe": "4h"},
        {"cost_model": "f" * 64},
    ],
)
def test_each_preseal_to_identity_mapping_fails_independently(changes: dict[str, object]) -> None:
    ready = _ready()
    with pytest.raises(OOSGovernanceV2Error, match="PRESEAL_IDENTITY_BINDING_MISMATCH"):
        require_preseal_identity_binding_v2(ready, _identity(ready, **changes))


def test_v1_objects_are_machine_visible_as_legacy_and_not_v2_eligible(tmp_path: Path) -> None:
    legacy_store = OOSSealStore(tmp_path / "legacy")
    legacy_seal = legacy_store.create(
        boundary=_window().boundary,
        key=KEY,
        audit_key=AUDIT_KEY,
        lineage_digest="3" * 64,
        run_id="legacy-run",
        experiment_identity_digest="4" * 64,
        checkpoint_digest="5" * 64,
        sealed_at="2026-09-01T00:00:00Z",
        evaluation_not_before="2026-10-01T00:00:00Z",
    )
    assert legacy_seal.governance_version == 1
    assert legacy_seal.promotion_scope == "LEGACY_RESEARCH_ONLY"
    assert legacy_seal.eligible_for_v2_promotion is False
    assert OOSAccessReceipt("a" * 64, 1, "b" * 64).eligible_for_v2_promotion is False
    assert not isinstance(legacy_seal, GovernedOOSSealV2)


def test_governed_store_refuses_draft_and_writes_nothing(tmp_path: Path) -> None:
    draft = assess_oos_preseal_v2(window=None, candidate=None)
    store = GovernedOOSSealStoreV2(tmp_path / "governed")
    with pytest.raises(OOSGovernanceV2Error, match="PRESEAL_NOT_READY"):
        store.create(
            preseal=draft,
            identity=_identity(),
            checkpoint_digest="6" * 64,
            boundary_key=KEY,
            audit_key=AUDIT_KEY,
            governed_at=GOVERNED_AT,
        )
    assert not (tmp_path / "governed" / "oos-seal.json").exists()
    assert not (tmp_path / "governed" / "oos-governed-seal-v2.json").exists()


def test_crash_after_legacy_seal_leaves_non_upgradable_orphan(tmp_path: Path) -> None:
    def fail_after_legacy(boundary: str) -> None:
        if boundary == "after_legacy_seal_durable":
            raise RuntimeError("simulated-crash")

    root = tmp_path / "orphan"
    ready = _ready()
    store = GovernedOOSSealStoreV2(root, fault_injector=fail_after_legacy)
    with pytest.raises(RuntimeError, match="simulated-crash"):
        store.create(
            preseal=ready,
            identity=_identity(ready),
            checkpoint_digest="6" * 64,
            boundary_key=KEY,
            audit_key=AUDIT_KEY,
            governed_at=GOVERNED_AT,
        )
    assert (root / "oos-seal.json").is_file()
    assert not (root / "oos-governed-seal-v2.json").exists()
    with pytest.raises(OOSGovernanceV2Error, match="ORPHAN_LEGACY_SEAL_NOT_V2_UPGRADABLE"):
        GovernedOOSSealStoreV2(root).create(
            preseal=ready,
            identity=_identity(ready),
            checkpoint_digest="6" * 64,
            boundary_key=KEY,
            audit_key=AUDIT_KEY,
            governed_at=GOVERNED_AT,
        )


def test_governed_record_without_exact_legacy_record_is_not_verifiable(tmp_path: Path) -> None:
    store, _ready_value, _identity_value, _seal = _created_store(tmp_path / "missing-legacy")
    store.legacy_store.seal_path.unlink()
    with pytest.raises(OOSGovernanceV2Error, match="GOVERNED_LEGACY_SEAL_NOT_VERIFIABLE"):
        store.load_governed_seal()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("preseal_digest", "8" * 64),
        ("window_custody_digest", "8" * 64),
        ("candidate_freeze_digest", "8" * 64),
        ("experiment_identity_digest", "8" * 64),
        ("legacy_seal_digest", "8" * 64),
        ("governed_at", "2026-09-01T02:00:01.000000Z"),
    ],
)
def test_governed_seal_recomputes_every_caller_supplied_binding(tmp_path: Path, field: str, value: object) -> None:
    _store, _ready_value, _identity_value, seal = _created_store(tmp_path / field)
    with pytest.raises(OOSGovernanceV2Error, match="GOVERNED_SEAL_DIGEST_MISMATCH"):
        replace(seal, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("governed_seal_digest", "8" * 64),
        ("preseal_digest", "8" * 64),
        ("legacy_seal_digest", "8" * 64),
        ("legacy_sequence", 2),
        ("legacy_audit_head", "8" * 64),
    ],
)
def test_governed_receipt_recomputes_every_caller_supplied_binding(tmp_path: Path, field: str, value: object) -> None:
    store, ready, identity, seal = _created_store(tmp_path / field)
    receipt = _access_once(store, ready, identity, seal)
    with pytest.raises(OOSGovernanceV2Error, match="GOVERNED_RECEIPT_DIGEST_MISMATCH"):
        replace(receipt, **{field: value})


def test_v2_concurrent_access_issues_exactly_one_governed_receipt(tmp_path: Path) -> None:
    root = tmp_path / "governed-race"
    _created_store(root)
    context = mp.get_context("spawn")
    start = context.Barrier(2)
    results = context.Queue()
    processes = [context.Process(target=_governed_race_worker, args=(str(root), start, results)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in processes]
    assert sum(outcome[0] == "SUCCESS" for outcome in outcomes) == 1, outcomes
    assert sum(outcome[0] == "DENIED" and "REPEATED_OOS_ACCESS" in outcome[1] for outcome in outcomes) == 1


def test_v2_crash_after_durable_access_is_fail_closed_on_retry(tmp_path: Path) -> None:
    def fail_after_access(boundary: str) -> None:
        if boundary == "after_legacy_access_durable":
            raise RuntimeError("simulated-post-commit-crash")

    root = tmp_path / "post-commit"
    store, ready, identity, seal = _created_store(root, fault_injector=fail_after_access)
    with pytest.raises(RuntimeError, match="simulated-post-commit-crash"):
        _access_once(store, ready, identity, seal)
    events = store.legacy_store.audit_events()
    assert len(events) == 1
    assert events[0].decision == "ALLOWED_ONCE"
    retry = GovernedOOSSealStoreV2(root)
    with pytest.raises(OOSAccessInvalidated, match="REPEATED_OOS_ACCESS"):
        _access_once(retry, ready, identity, seal)
    assert len(retry.legacy_store.audit_events()) == 2


def test_only_governed_types_can_produce_v2_eligible_promotion(tmp_path: Path) -> None:
    ready = _ready()
    identity = _identity(ready)
    store = GovernedOOSSealStoreV2(tmp_path / "complete")
    seal = store.create(
        preseal=ready,
        identity=identity,
        checkpoint_digest="6" * 64,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        governed_at=GOVERNED_AT,
    )
    receipt = store.access(
        seal=seal,
        preseal=ready,
        identity=identity,
        checkpoint_digest="6" * 64,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        accessed_at=READ_AT,
        candidate_evaluation_complete=True,
        candidate_evaluation_digest="7" * 64,
        purpose="sealed-one-shot-evaluation",
    )
    decision = store.promotion_status(
        seal=seal,
        receipt=receipt,
        preseal=ready,
        identity=identity,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        candidate_evaluation_digest="7" * 64,
    )
    assert isinstance(seal, GovernedOOSSealV2)
    assert isinstance(receipt, GovernedOOSAccessReceiptV2)
    assert isinstance(decision, GovernedPromotionDecisionV2)
    assert decision.status == "PROMOTABLE_V2"
    assert decision.eligible_for_v2_promotion is True
    for field, value in (
        ("status", "NOT_VERIFIABLE_V2"),
        ("reasons", ("MUTATED",)),
        ("eligible_for_v2_promotion", False),
        ("governed_seal_digest", "8" * 64),
        ("governed_receipt_digest", "8" * 64),
        ("preseal_digest", "8" * 64),
        ("experiment_identity_digest", "8" * 64),
        ("candidate_evaluation_digest", "8" * 64),
    ):
        with pytest.raises(OOSGovernanceV2Error, match="GOVERNED_PROMOTION_"):
            replace(decision, **{field: value})
    legacy_receipt = OOSAccessReceipt(seal.legacy_seal_digest, 1, receipt.legacy_audit_head)
    with pytest.raises((TypeError, OOSGovernanceV2Error), match="V2_TYPE_REQUIRED"):
        store.promotion_status(
            seal=seal,
            receipt=legacy_receipt,  # type: ignore[arg-type]
            preseal=ready,
            identity=identity,
            boundary_key=KEY,
            audit_key=AUDIT_KEY,
            candidate_evaluation_digest="7" * 64,
        )


def test_public_value_factories_cannot_bypass_store_authority() -> None:
    assert not hasattr(GovernedOOSSealV2, "build")
    assert not hasattr(GovernedOOSAccessReceiptV2, "build")
    assert not hasattr(GovernedPromotionDecisionV2, "build")


def test_direct_eligible_decision_requires_store_authority(tmp_path: Path) -> None:
    store, ready, identity, seal = _created_store(tmp_path / "store-authority")
    receipt = _access_once(store, ready, identity, seal)
    decision = store.promotion_status(
        seal=seal,
        receipt=receipt,
        preseal=ready,
        identity=identity,
        boundary_key=KEY,
        audit_key=AUDIT_KEY,
        candidate_evaluation_digest=CANDIDATE_EVALUATION_DIGEST,
    )
    with pytest.raises(OOSGovernanceV2Error, match="GOVERNED_PROMOTION_STORE_AUTHORITY_REQUIRED"):
        GovernedPromotionDecisionV2(
            status=decision.status,
            reasons=decision.reasons,
            eligible_for_v2_promotion=decision.eligible_for_v2_promotion,
            governed_seal_digest=decision.governed_seal_digest,
            governed_receipt_digest=decision.governed_receipt_digest,
            preseal_digest=decision.preseal_digest,
            experiment_identity_digest=decision.experiment_identity_digest,
            candidate_evaluation_digest=decision.candidate_evaluation_digest,
            governed_promotion_digest=decision.governed_promotion_digest,
        )
