"""BD-AF-P3-T07: adversarial scientific-validation semantics.

The fixtures are deliberately constructed from immutable raw observations.  A
stored implementation verdict is comparison evidence only; it is never an
oracle input.  Synthetic fixtures prove rejection semantics, not profitability.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from beidou_research.validation.contracts import (
    ContractNotVerifiable,
    canonical_digest,
    load_metric_owner_policy,
)
from beidou_research.validation.independent_oracle import (
    rollback_scientific_validation,
    validate_scientific_evidence,
    write_acceptance_artifacts,
)

POLICY_PATH = Path(
    os.environ.get(
        "BEIDOU_T07_METRIC_OWNER_POLICY",
        "/Users/maguannan/beidou-authorization/BD-AF-P3-T07/metric-owner-policy.json",
    )
)
POLICY_SHA256 = "2b7f5bf287432b6051e9252f525023b0c23dfa17c2c26aa4e95ab18002e61f70"
POLICY_DIGEST = "96dbcd31d32ffb53c532a24634c2c85082e4f7d045fd1da24d8521d9495d08b9"
HEX = "0123456789abcdef"


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _candidate_id(family_id: str, index: int) -> str:
    spec_digest = _hex(f"candidate-spec-{index}")
    return canonical_digest({"family_id": family_id, "candidate_spec_digest": spec_digest})


def _reseal(evidence: dict[str, Any]) -> dict[str, Any]:
    evidence["raw_evidence_digest"] = canonical_digest(
        {key: value for key, value in evidence.items() if key != "raw_evidence_digest"}
    )
    return evidence


def _recorded_wfo_folds(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    n = len(samples)
    n_folds = 5
    fold_span = n // n_folds
    test_span = math.floor(fold_span * 0.4)
    purge_span = max(math.ceil(0.05 * fold_span), 1)
    embargo_span = math.ceil(0.02 * fold_span)
    folds: list[dict[str, Any]] = []
    previous_test_ranges: list[range] = []
    for fold_id in range(n_folds):
        test_start = math.floor(fold_span * fold_id + fold_span * 0.6)
        test_end = min(test_start + test_span, n)
        test = list(range(test_start, test_end))
        embargoed = {
            index
            for previous in previous_test_ranges
            for index in range(previous.stop, min(previous.stop + embargo_span, n))
        }
        train = [
            index
            for index in range(0, max(0, test_start - purge_span))
            if index not in embargoed
            and datetime.fromisoformat(samples[index]["label_end_time"])
            <= datetime.fromisoformat(samples[test_start]["observation_time"])
        ]
        folds.append(
            {
                "fold_id": fold_id,
                "train_keys": [samples[index]["key"] for index in train],
                "test_keys": [samples[index]["key"] for index in test],
            }
        )
        previous_test_ranges.append(range(test_start, test_end))
    return folds


def _recorded_cpcv_paths(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = [list(group) for group in _split_indices(len(samples), 8)]
    paths: list[dict[str, Any]] = []
    for path_id, test_groups in enumerate(itertools.combinations(range(8), 2)):
        test = sorted(index for group in test_groups for index in groups[group])
        test_set = set(test)
        excluded = set(test)
        for index in test:
            excluded.update(range(max(0, index - 5), min(len(samples), index + 6)))
        for group in test_groups:
            stop = groups[group][-1] + 1
            excluded.update(range(stop, min(len(samples), stop + 1)))
        train = [index for index in range(len(samples)) if index not in excluded and index not in test_set]
        paths.append(
            {
                "path_id": path_id,
                "test_groups": list(test_groups),
                "train_keys": [samples[index]["key"] for index in train],
                "test_keys": [samples[index]["key"] for index in test],
            }
        )
    return paths


def _split_indices(n: int, groups: int) -> list[range]:
    base, remainder = divmod(n, groups)
    result: list[range] = []
    start = 0
    for group in range(groups):
        size = base + (1 if group < remainder else 0)
        result.append(range(start, start + size))
        start += size
    return result


def _baseline_evidence() -> dict[str, Any]:
    family_basis = {
        "policy_id": "BD-AF-P3-T07-METRIC-OWNER-POLICY",
        "policy_version": "1.0.0",
        "dataset_manifest_digest": _hex("dataset"),
        "pit_manifest_digest": _hex("pit"),
        "universe": "BTCUSDT",
        "primary_interval": "1h",
        "horizon_bars": 1,
        "generator_scope": "frozen-grid-v1",
        "search_scope": "eight-candidate-semantic-fixture",
    }
    family_id = canonical_digest(family_basis)
    candidate_ids = [_candidate_id(family_id, index) for index in range(8)]
    candidates = [
        {
            "candidate_id": candidate_id,
            "candidate_spec_digest": _hex(f"candidate-spec-{index}"),
            "evaluation_status": "PASS" if index == 0 else "FAIL",
            "p_value": 1e-12 if index == 0 else 0.2 + index * 0.05,
        }
        for index, candidate_id in enumerate(candidate_ids)
    ]

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    component_sources = {
        component: _hex(f"source-{component}")
        for component in ("maker_fee", "taker_fee", "half_spread", "slippage", "funding", "market_impact")
    }
    costs = {
        "maker_fee": 2.0,
        "taker_fee": 4.0,
        "half_spread": 0.5,
        "slippage": 1.0,
        "funding": 0.125,
        "market_impact": 0.1,
    }
    samples: list[dict[str, Any]] = []
    for index in range(960):
        observation = start + timedelta(hours=index)
        label_return = 0.0032 + 0.0007 * math.sin(index / 9.0) + 0.0002 * math.cos(index / 23.0)
        predictions = {
            candidate_ids[0]: label_return,
            candidate_ids[1]: -label_return,
            candidate_ids[2]: label_return if index % 3 else -label_return,
            candidate_ids[3]: label_return if index % 4 else -label_return,
            candidate_ids[4]: math.sin(index * 1.7),
            candidate_ids[5]: math.cos(index * 1.3),
            candidate_ids[6]: -1.0 if index % 2 else 1.0,
            candidate_ids[7]: 0.1 if index % 5 else -0.1,
        }
        samples.append(
            {
                "key": f"BTCUSDT-1h-{index:04d}",
                "venue": "BINANCE",
                "symbol": "BTCUSDT",
                "interval": "1h",
                "observation_time": observation.isoformat(),
                "label_end_time": (observation + timedelta(hours=1)).isoformat(),
                "available_as_of": (observation + timedelta(seconds=1)).isoformat(),
                "is_closed": True,
                "revision": 0,
                "label_return": label_return,
                "candidate_predictions": predictions,
                "cost_components_bps": dict(costs),
                "cost_source_digests": dict(component_sources),
                "market_volume_base": 1_000_000.0 + (index % 24) * 1_000.0,
                "price_quote": 100.0,
                "order_notional_quote": 10_000.0,
                "regime": "risk_on" if (index // 40) % 2 == 0 else "risk_off",
            }
        )

    daily_samples = []
    for index in range(120):
        label_return = 0.012 + 0.002 * math.sin(index / 7.0)
        daily_samples.append(
            {
                "key": f"BTCUSDT-1d-{index:04d}",
                "prediction": label_return,
                "label_return": label_return,
                "cost_components_bps": dict(costs),
                "cost_source_digests": dict(component_sources),
            }
        )
    perturbations = [
        {
            "perturbation_id": f"p{offset}",
            "parameter_delta_pct": -0.1 if offset % 2 else 0.1,
            "predictions": [
                sample["candidate_predictions"][candidate_ids[0]] * (0.9 if offset % 2 else 1.1) for sample in samples
            ],
        }
        for offset in range(1, 6)
    ]
    family = {
        **family_basis,
        "family_id": family_id,
        "frozen_before_evaluation": True,
        "declared_denominator": len(candidates),
        "selected_candidate_id": candidate_ids[0],
        "candidates": candidates,
    }
    family["family_registry_digest"] = canonical_digest(candidates)
    evidence = {
        "schema_version": "1.0.0",
        "bindings": {
            "policy_digest": POLICY_DIGEST,
            "dataset_manifest_digest": family_basis["dataset_manifest_digest"],
            "pit_manifest_digest": family_basis["pit_manifest_digest"],
            "lineage_manifest_digest": _hex("lineage"),
            "oos_seal_digest": _hex("oos-seal"),
            "oos_audit_head": _hex("oos-audit"),
            "experiment_identity_digest": _hex("identity"),
            "checkpoint_digest": _hex("checkpoint"),
        },
        "family": family,
        "samples": samples,
        "recorded_wfo_folds": _recorded_wfo_folds(samples),
        "recorded_cpcv_paths": _recorded_cpcv_paths(samples),
        "stability": {
            "timeframe_1h_vs_1d": daily_samples,
            "parameter_perturbation": perturbations,
        },
        "implementation_claim": {
            "status": "PASS",
            "family_denominator": len(candidates),
            "policy_digest": POLICY_DIGEST,
        },
    }
    return _reseal(evidence)


def _mutated_result(policy, mutate, *, reseal: bool = True):
    evidence = _baseline_evidence()
    mutate(evidence)
    if reseal:
        _reseal(evidence)
    return validate_scientific_evidence(policy, evidence)


def _set_selected_pvalue(evidence: dict[str, Any], p_value: float) -> None:
    evidence["family"]["candidates"][0]["p_value"] = p_value
    evidence["family"]["family_registry_digest"] = canonical_digest(evidence["family"]["candidates"])


def _exceed_capacity(evidence: dict[str, Any]) -> None:
    evidence["samples"][0]["order_notional_quote"] = 20_000_000.0
    evidence["samples"][0]["cost_components_bps"]["market_impact"] = 200.0


@pytest.fixture(scope="module")
def policy():
    return load_metric_owner_policy(POLICY_PATH)


def test_owner_policy_digest_and_status_are_frozen(policy) -> None:
    assert policy.digest == POLICY_DIGEST
    assert policy.source_sha256 == POLICY_SHA256
    assert policy.document["status"] == "FROZEN_OWNER_APPROVED"


def test_policy_digest_mutation_is_not_verifiable(policy) -> None:
    mutated = copy.deepcopy(policy.document)
    mutated["multiple_testing"]["pbo"]["threshold"] = 0.3
    with pytest.raises(ContractNotVerifiable, match="POLICY_DIGEST_MISMATCH"):
        load_metric_owner_policy(mutated)


def test_reference_fixture_recomputes_every_gate_and_passes(policy) -> None:
    result = validate_scientific_evidence(policy, _baseline_evidence())
    assert result.status == "PASS", result.reasons
    assert result.implementation_agreement == "AGREE"
    assert all(result.hard_gates.values())
    assert {
        "wfo",
        "cpcv",
        "pbo",
        "dsr",
        "fdr",
        "holm",
        "cost_capacity",
        "stability",
        "uncertainty",
        "sample_sufficiency",
    } <= result.hard_gates.keys()


def test_fake_pass_and_null_alpha_are_rejected(policy) -> None:
    evidence = _baseline_evidence()
    selected = evidence["family"]["selected_candidate_id"]
    for index, sample in enumerate(evidence["samples"]):
        sample["candidate_predictions"][selected] = 1.0 if index % 2 else -1.0
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status != "PASS"
    assert result.implementation_agreement == "DISAGREE"


def test_label_window_leakage_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    observation = datetime.fromisoformat(evidence["samples"][20]["observation_time"])
    evidence["samples"][20]["label_end_time"] = (observation + timedelta(hours=2)).isoformat()
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "LABEL_WINDOW_MISMATCH" in result.reasons


@pytest.mark.parametrize("boundary", ["purge", "embargo"])
def test_bad_purge_or_embargo_fold_membership_is_not_verifiable(policy, boundary: str) -> None:
    evidence = _baseline_evidence()
    fold = evidence["recorded_wfo_folds"][1]
    if boundary == "purge":
        fold["train_keys"].append(evidence["samples"][300]["key"])
    else:
        fold["train_keys"].append(evidence["samples"][191]["key"])
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "WFO_FOLD_MEMBERSHIP_MISMATCH" in result.reasons


def test_family_denominator_shrinkage_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["family"]["candidates"].pop()
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert any("FAMILY" in reason or "DENOMINATOR" in reason for reason in result.reasons)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_raw_values_are_not_verifiable(policy, bad_value: float) -> None:
    evidence = _baseline_evidence()
    evidence["samples"][30]["label_return"] = bad_value
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "NON_FINITE_EVIDENCE" in result.reasons


@pytest.mark.parametrize("component", ["maker_fee", "taker_fee", "half_spread", "slippage", "funding", "market_impact"])
def test_omitted_cost_component_is_not_verifiable(policy, component: str) -> None:
    evidence = _baseline_evidence()
    evidence["samples"][0]["cost_components_bps"].pop(component)
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "COST_COMPONENTS_INCOMPLETE" in result.reasons


def test_missing_capacity_volume_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["samples"][0].pop("market_volume_base")
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "CAPACITY_INPUT_MISSING" in result.reasons


def test_low_sample_fixture_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["samples"] = evidence["samples"][:120]
    evidence["recorded_wfo_folds"] = _recorded_wfo_folds(evidence["samples"])
    evidence["recorded_cpcv_paths"] = _recorded_cpcv_paths(evidence["samples"])
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert any("SAMPLES" in reason or "FOLDS" in reason for reason in result.reasons)


def test_missing_cpcv_path_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["recorded_cpcv_paths"].pop()
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "CPCV_PATH_COUNT_MISMATCH" in result.reasons


def test_missing_stability_dimension_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["stability"].pop("parameter_perturbation")
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "STABILITY_DIMENSION_MISSING" in result.reasons


def test_tampered_raw_payload_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["samples"][5]["label_return"] += 0.01
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "RAW_EVIDENCE_DIGEST_MISMATCH" in result.reasons


def test_missing_lineage_or_oos_binding_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["bindings"].pop("oos_seal_digest")
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "BINDINGS_INCOMPLETE" in result.reasons


def test_uncertainty_gate_rejects_non_positive_stressed_lower_bound(policy) -> None:
    evidence = _baseline_evidence()
    for sample in evidence["samples"]:
        sample["label_return"] = 0.0009
    _reseal(evidence)
    result = validate_scientific_evidence(policy, evidence)
    assert result.status != "PASS"
    assert result.hard_gates.get("uncertainty") is False


def test_every_hard_gate_mutation_changes_the_final_decision(policy) -> None:
    baseline = _baseline_evidence()
    mutations: dict[str, Any] = {
        "sample_sufficiency": lambda value: value["samples"].__delitem__(slice(0, 850)),
        "wfo": lambda value: value["recorded_wfo_folds"].pop(),
        "cpcv": lambda value: value["recorded_cpcv_paths"].pop(),
        "pbo": lambda value: value["recorded_cpcv_paths"].pop(),
        "fdr": lambda value: _set_selected_pvalue(value, 0.9),
        "holm": lambda value: _set_selected_pvalue(value, 0.9),
        "dsr": lambda value: [sample.__setitem__("label_return", 0.0009) for sample in value["samples"]],
        "cost_capacity": _exceed_capacity,
        "stability": lambda value: value["stability"].pop("timeframe_1h_vs_1d"),
        "uncertainty": lambda value: [sample.__setitem__("label_return", 0.0009) for sample in value["samples"]],
    }
    outcomes: dict[str, str] = {}
    for gate, mutate in mutations.items():
        evidence = copy.deepcopy(baseline)
        mutate(evidence)
        _reseal(evidence)
        outcomes[gate] = validate_scientific_evidence(policy, evidence).status
    assert outcomes
    assert all(status != "PASS" for status in outcomes.values()), outcomes


def test_rollback_preserves_bindings_and_never_restores_promotion(policy) -> None:
    passed = validate_scientific_evidence(policy, _baseline_evidence())
    rolled_back = rollback_scientific_validation(passed)
    assert rolled_back["status"] == "NOT_VERIFIABLE"
    assert rolled_back["promotion_enabled"] is False
    assert rolled_back["historical_status"] == "PASS"
    assert rolled_back["preserved_policy_digest"] == POLICY_DIGEST
    assert rolled_back["preserved_raw_evidence_digest"] == passed.raw_evidence_digest


def test_acceptance_artifacts_are_deterministic_and_complete(policy, tmp_path: Path) -> None:
    evidence_dir = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", tmp_path))
    result = validate_scientific_evidence(policy, _baseline_evidence())
    negative_results = [
        _mutated_result(
            policy,
            lambda value: [
                sample["candidate_predictions"].__setitem__(
                    value["family"]["selected_candidate_id"], 1.0 if index % 2 else -1.0
                )
                for index, sample in enumerate(value["samples"])
            ],
        ),
        _mutated_result(
            policy,
            lambda value: value["samples"][20].__setitem__(
                "label_end_time",
                (datetime.fromisoformat(value["samples"][20]["observation_time"]) + timedelta(hours=2)).isoformat(),
            ),
        ),
        _mutated_result(
            policy, lambda value: value["recorded_wfo_folds"][1]["train_keys"].append(value["samples"][300]["key"])
        ),
        _mutated_result(policy, lambda value: value["family"]["candidates"].pop()),
        _mutated_result(
            policy, lambda value: value["samples"][0].__setitem__("label_return", float("nan")), reseal=False
        ),
        _mutated_result(policy, lambda value: value["samples"][0]["cost_components_bps"].pop("market_impact")),
        _mutated_result(policy, lambda value: value["samples"][0].pop("market_volume_base")),
        _mutated_result(policy, lambda value: value["samples"].__delitem__(slice(120, None))),
        _mutated_result(policy, lambda value: value["recorded_cpcv_paths"].pop()),
        _mutated_result(policy, lambda value: value["stability"].pop("parameter_perturbation")),
        _mutated_result(policy, lambda value: value["bindings"].pop("oos_seal_digest")),
        _mutated_result(policy, lambda value: value["samples"][5].__setitem__("label_return", 0.5), reseal=False),
        _mutated_result(
            policy,
            lambda value: [sample.__setitem__("label_return", 0.0009) for sample in value["samples"]],
        ),
    ]
    mutation_outcomes = {
        "sample_sufficiency": negative_results[7].status,
        "wfo": negative_results[2].status,
        "cpcv": negative_results[8].status,
        "pbo": negative_results[8].status,
        "fdr": _mutated_result(policy, lambda value: _set_selected_pvalue(value, 0.9)).status,
        "holm": _mutated_result(policy, lambda value: _set_selected_pvalue(value, 0.9)).status,
        "dsr": negative_results[12].status,
        "cost_capacity": _mutated_result(policy, _exceed_capacity).status,
        "stability": negative_results[9].status,
        "uncertainty": negative_results[12].status,
    }
    write_acceptance_artifacts(
        evidence_dir,
        policy=policy,
        recomputation=result,
        negative_results=negative_results,
        mutation_outcomes=mutation_outcomes,
    )
    expected = {
        "metric-owner-policy.json",
        "independent-recomputation.json",
        "semantic-negative-fixtures.json",
        "gate-mutation-score.json",
    }
    assert expected <= {path.name for path in evidence_dir.iterdir()}
    score = json.loads((evidence_dir / "gate-mutation-score.json").read_text(encoding="utf-8"))
    assert score["score"] == 1.0
    assert score["status"] == "PASS"


def test_fixture_digests_are_lowercase_sha256() -> None:
    evidence = _baseline_evidence()
    assert len(evidence["raw_evidence_digest"]) == 64
    assert set(evidence["raw_evidence_digest"]) <= set(HEX)
