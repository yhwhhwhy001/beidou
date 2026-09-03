"""Semantic branch coverage for research validation and portfolio custody contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import beidou_research.portfolio.contracts as portfolio_contracts
import beidou_research.portfolio.decision as portfolio_decision
import beidou_research.validation.contracts as validation_contracts
import beidou_research.validation.independent_oracle as validation_oracle
from beidou_research.portfolio.contracts import PortfolioDecision, PortfolioNotVerifiable, PortfolioOwnerPolicy
from beidou_research.portfolio.store import (
    AppendOnlyDecisionStore,
    DecisionStoreError,
    DecisionStoreNotVerifiable,
)
from beidou_research.validation.contracts import ContractNotVerifiable, MetricOwnerPolicy
from beidou_shared.contracts.alpha_execution import AlphaTarget, ExecutionFact, ExecutionStatus, OrderIntent
from beidou_shared.contracts.experiment import DatasetRef, ExperimentRunRef
from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId
from tests.policy_envelope import require_policy_envelope
from tests.research.test_candidate_champion_decision import (
    POLICY_PATH as PORTFOLIO_POLICY_PATH,
)
from tests.research.test_candidate_champion_decision import (
    _baseline_evidence as portfolio_evidence,
)
from tests.research.test_candidate_champion_decision import (
    _reseal as reseal_portfolio,
)
from tests.research.test_scientific_validation_semantics import (
    POLICY_PATH as VALIDATION_POLICY_PATH,
)
from tests.research.test_scientific_validation_semantics import (
    _baseline_evidence as validation_evidence,
)
from tests.research.test_scientific_validation_semantics import (
    _reseal as reseal_validation,
)


@pytest.fixture(scope="module")
def metric_policy() -> MetricOwnerPolicy:
    require_policy_envelope(VALIDATION_POLICY_PATH, "BEIDOU_T07_METRIC_OWNER_POLICY")
    return validation_contracts.load_metric_owner_policy(VALIDATION_POLICY_PATH)


@pytest.fixture(scope="module")
def owner_policy() -> PortfolioOwnerPolicy:
    require_policy_envelope(PORTFOLIO_POLICY_PATH, "BEIDOU_T08_PORTFOLIO_OWNER_POLICY")
    return portfolio_contracts.load_portfolio_owner_policy(PORTFOLIO_POLICY_PATH)


def _metric_policy_with(policy: MetricOwnerPolicy, mutate: Any) -> MetricOwnerPolicy:
    document = copy.deepcopy(policy.document)
    mutate(document)
    return replace(policy, document=document)


def _owner_policy_document() -> dict[str, Any]:
    require_policy_envelope(PORTFOLIO_POLICY_PATH, "BEIDOU_T08_PORTFOLIO_OWNER_POLICY")
    return json.loads(PORTFOLIO_POLICY_PATH.read_text(encoding="utf-8"))


def _metric_policy_document() -> dict[str, Any]:
    require_policy_envelope(VALIDATION_POLICY_PATH, "BEIDOU_T07_METRIC_OWNER_POLICY")
    return json.loads(VALIDATION_POLICY_PATH.read_text(encoding="utf-8"))


def _refresh_validation_family(evidence: dict[str, Any]) -> None:
    evidence["family"]["family_registry_digest"] = validation_contracts.canonical_digest(
        evidence["family"]["candidates"]
    )


def _identity_digest(identity: dict[str, Any]) -> str:
    return portfolio_contracts.canonical_digest(
        {key: value for key, value in identity.items() if key != "identity_digest"}
    )


def _decision(decision_id: str, supersedes: str | None = None, **changes: Any) -> PortfolioDecision:
    values: dict[str, Any] = {
        "status": "PASS",
        "disposition": "SHADOW_ELIGIBLE",
        "reasons": (),
        "hard_gates": {"identity": True},
        "recomputed": {"mean_delta_return": 0.01},
        "decision_id": decision_id,
        "portfolio_id": "PORTFOLIO-UNIT",
        "candidate_id": "CANDIDATE",
        "champion_id": "CHAMPION",
        "candidate_identity_digest": "a" * 64,
        "champion_identity_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "input_bundle_digest": "d" * 64,
        "decision_timestamp_utc": "2026-08-29T00:00:00+00:00",
        "supersedes_decision_id": supersedes,
    }
    values.update(changes)
    return PortfolioDecision(**values)


def _valid_store_records(tmp_path: Path) -> tuple[list[dict[str, Any]], str, str]:
    path = tmp_path / "valid-decisions.jsonl"
    store = AppendOnlyDecisionStore(path)
    first_id, second_id = "1" * 64, "2" * 64
    store.append(_decision(first_id))
    store.append(_decision(second_id, first_id))
    return store._records(), first_id, second_id


def _reseal_store_record(record: dict[str, Any]) -> None:
    record["decision_digest"] = portfolio_contracts.canonical_digest(record["decision"])
    record["record_hash"] = portfolio_contracts.canonical_digest(
        {key: value for key, value in record.items() if key != "record_hash"}
    )


def test_neutral_alpha_contracts_reject_ambiguous_or_unbounded_values() -> None:
    aware = datetime(2026, 8, 29, tzinfo=timezone.utc)
    naive = datetime(2026, 8, 29)  # noqa: DTZ001 - deliberately invalid contract input
    base_target = {
        "strategy_id": StrategyId("alpha-v4"),
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("BINANCE_TESTNET"),
        "target_weight": 0.1,
        "forecast_hash": "forecast-sha256",
        "model_version": SchemaVersion("4.0.0"),
        "timestamp": aware,
    }

    for change, reason in (
        ({"target_weight": float("nan")}, "target_weight must be finite"),
        ({"forecast_hash": "  "}, "forecast_hash must be non-empty"),
        ({"timestamp": naive}, "timestamp must be timezone-aware"),
    ):
        with pytest.raises(ValueError, match=reason):
            AlphaTarget(**(base_target | change))

    target = AlphaTarget(**base_target)
    with pytest.raises(ValueError, match="intent_id must be non-empty"):
        OrderIntent(intent_id=" ", target=target, requested_at=aware)
    with pytest.raises(ValueError, match="requested_at must be timezone-aware"):
        OrderIntent(intent_id="intent-1", target=target, requested_at=naive)

    for values, reason in (
        ({"intent_id": " ", "filled_weight": 0.1, "observed_at": aware}, "intent_id must be non-empty"),
        (
            {"intent_id": "intent-1", "filled_weight": float("inf"), "observed_at": aware},
            "filled_weight must be finite when present",
        ),
        (
            {"intent_id": "intent-1", "filled_weight": None, "observed_at": naive},
            "observed_at must be timezone-aware",
        ),
    ):
        with pytest.raises(ValueError, match=reason):
            ExecutionFact(status=ExecutionStatus.UNKNOWN, **values)


def test_offline_experiment_contracts_reject_unbound_identity_and_clock() -> None:
    valid_dataset = DatasetRef(
        dataset_id="btc-1h",
        version=SchemaVersion("1.0.0"),
        content_hash="dataset-sha256",
    )
    for changes, reason in (
        ({"dataset_id": " "}, "dataset_id must be non-empty"),
        ({"version": SchemaVersion(" ")}, "version must be non-empty"),
        ({"content_hash": " "}, "content_hash must be non-empty"),
        ({"source": "REMOTE"}, "offline Alpha requires a LOCAL dataset reference"),
    ):
        values = {
            "dataset_id": "btc-1h",
            "version": SchemaVersion("1.0.0"),
            "content_hash": "dataset-sha256",
            "source": "LOCAL",
        }
        with pytest.raises(ValueError, match=reason):
            DatasetRef(**(values | changes))

    aware = datetime(2026, 8, 29, tzinfo=timezone.utc)
    for changes, reason in (
        ({"run_id": " "}, "run_id and code_hash must be non-empty"),
        ({"code_hash": " "}, "run_id and code_hash must be non-empty"),
        (
            {"started_at": datetime(2026, 8, 29)},  # noqa: DTZ001 - deliberately invalid contract input
            "started_at must be timezone-aware",
        ),
    ):
        values = {
            "run_id": "run-v4",
            "code_hash": "code-sha256",
            "policy_version": SchemaVersion("4.0.0"),
            "dataset": valid_dataset,
            "started_at": aware,
        }
        with pytest.raises(ValueError, match=reason):
            ExperimentRunRef(**(values | changes))


def test_validation_canonical_json_rejects_non_json_semantics() -> None:
    with pytest.raises(ContractNotVerifiable, match="NON_STRING_JSON_KEY"):
        validation_contracts.canonical_json({1: "not-json-contract"})
    with pytest.raises(ContractNotVerifiable, match="UNSUPPORTED_CANONICAL_TYPE:object"):
        validation_contracts.canonical_json(object())


def test_validation_strict_json_rejects_duplicates_corruption_and_non_objects(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"policy_id":"first","policy_id":"second"}', encoding="utf-8")
    with pytest.raises(ContractNotVerifiable, match="DUPLICATE_JSON_KEY:policy_id"):
        validation_contracts._load_json_strict(duplicate)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_UNREADABLE:JSONDecodeError"):
        validation_contracts._load_json_strict(invalid)

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ContractNotVerifiable, match="POLICY_NOT_OBJECT"):
        validation_contracts._load_json_strict(non_object)


def test_metric_policy_loader_enforces_source_digest_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-policy.json"
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_UNREADABLE:FileNotFoundError"):
        validation_contracts.load_metric_owner_policy(missing)

    wrong_bytes = tmp_path / "wrong-policy.json"
    wrong_bytes.write_text("{}", encoding="utf-8")
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_SHA256_MISMATCH"):
        validation_contracts.load_metric_owner_policy(wrong_bytes)
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_INVALID"):
        validation_contracts.load_metric_owner_policy(object())  # type: ignore[arg-type]
    with pytest.raises(ContractNotVerifiable, match="POLICY_DIGEST_MISSING"):
        validation_contracts.load_metric_owner_policy({})

    document = _metric_policy_document()
    mapped = validation_contracts.load_metric_owner_policy(document)
    assert mapped.source_sha256 == "MAPPING_INPUT"
    assert mapped.source_bytes == b""

    mutated = copy.deepcopy(document)
    mutated["minimum_samples"]["fast_screen"]["min_sample_count"] += 1
    with pytest.raises(ContractNotVerifiable, match="POLICY_DIGEST_MISMATCH"):
        validation_contracts.load_metric_owner_policy(mutated)

    wrong_identity = copy.deepcopy(document)
    wrong_identity["status"] = "DRAFT"
    digest_scope = copy.deepcopy(wrong_identity)
    del digest_scope["digest"]["policy_digest"]
    changed_digest = validation_contracts.canonical_digest(digest_scope)
    wrong_identity["digest"]["policy_digest"] = changed_digest
    monkeypatch.setattr(validation_contracts, "APPROVED_POLICY_DIGEST", changed_digest)
    with pytest.raises(ContractNotVerifiable, match="POLICY_IDENTITY_MISMATCH"):
        validation_contracts.load_metric_owner_policy(wrong_identity)


def test_raw_evidence_rejects_non_mapping_and_schema_drift(metric_policy: MetricOwnerPolicy) -> None:
    with pytest.raises(ContractNotVerifiable, match="EVIDENCE_NOT_OBJECT"):
        validation_contracts.validate_raw_evidence(metric_policy, [])  # type: ignore[arg-type]

    missing_field = validation_evidence()
    missing_field.pop("stability")
    reseal_validation(missing_field)
    with pytest.raises(ContractNotVerifiable, match="EVIDENCE_FIELDS_INVALID"):
        validation_contracts.validate_raw_evidence(metric_policy, missing_field)

    unknown_schema = validation_evidence()
    unknown_schema["schema_version"] = "9.9.9"
    reseal_validation(unknown_schema)
    with pytest.raises(ContractNotVerifiable, match="EVIDENCE_SCHEMA_VERSION_UNKNOWN"):
        validation_contracts.validate_raw_evidence(metric_policy, unknown_schema)


@pytest.mark.parametrize(
    ("mutation", "reason", "refresh_family"),
    [
        (lambda value: value["bindings"].__setitem__("policy_digest", "not-a-digest"), "BINDING_DIGEST_INVALID", False),
        (lambda value: value["family"].__setitem__("unexpected", True), "FAMILY_FIELDS_INVALID", False),
        (lambda value: value["family"].__setitem__("family_id", "0" * 64), "FAMILY_ID_MISMATCH", False),
        (lambda value: value["family"].__setitem__("candidates", []), "FAMILY_EMPTY", True),
        (
            lambda value: value["family"].__setitem__("frozen_before_evaluation", False),
            "FAMILY_DENOMINATOR_DRIFT",
            False,
        ),
        (
            lambda value: value["family"]["candidates"][0].__setitem__("unexpected", True),
            "CANDIDATE_FIELDS_INVALID",
            True,
        ),
        (
            lambda value: value["family"]["candidates"][0].__setitem__("candidate_id", "f" * 64),
            "CANDIDATE_ID_MISMATCH",
            True,
        ),
        (
            lambda value: value["family"]["candidates"][0].__setitem__("evaluation_status", "UNKNOWN"),
            "CANDIDATE_STATUS_INVALID",
            True,
        ),
        (
            lambda value: value["family"]["candidates"][0].__setitem__("p_value", 1.1),
            "P_VALUE_OUT_OF_RANGE",
            True,
        ),
        (
            lambda value: value["family"]["candidates"].__setitem__(1, copy.deepcopy(value["family"]["candidates"][0])),
            "FAMILY_CANDIDATE_DUPLICATE",
            True,
        ),
        (
            lambda value: value["family"].__setitem__("selected_candidate_id", "e" * 64),
            "SELECTED_CANDIDATE_UNKNOWN",
            False,
        ),
        (lambda value: value.__setitem__("samples", []), "SAMPLES_MISSING", False),
        (lambda value: value.__setitem__("recorded_wfo_folds", None), "WFO_FOLDS_MISSING", False),
        (lambda value: value.__setitem__("recorded_cpcv_paths", None), "CPCV_PATHS_MISSING", False),
        (lambda value: value.__setitem__("stability", []), "STABILITY_EVIDENCE_INVALID", False),
        (
            lambda value: value["implementation_claim"].__setitem__("unexpected", True),
            "IMPLEMENTATION_CLAIM_INVALID",
            False,
        ),
        (
            lambda value: value["implementation_claim"].__setitem__("status", "UNKNOWN"),
            "IMPLEMENTATION_STATUS_INVALID",
            False,
        ),
    ],
)
def test_raw_evidence_contract_rejects_semantic_mutations(
    metric_policy: MetricOwnerPolicy, mutation: Any, reason: str, refresh_family: bool
) -> None:
    evidence = validation_evidence()
    mutation(evidence)
    if refresh_family:
        _refresh_validation_family(evidence)
    reseal_validation(evidence)
    with pytest.raises(ContractNotVerifiable, match=reason):
        validation_contracts.validate_raw_evidence(metric_policy, evidence)


def test_raw_evidence_family_is_bound_to_frozen_sampling_policy(metric_policy: MetricOwnerPolicy) -> None:
    incompatible = _metric_policy_with(
        metric_policy,
        lambda document: document["sampling_clock"].__setitem__("primary_interval", "4h"),
    )
    with pytest.raises(ContractNotVerifiable, match="FAMILY_BINDING_MISMATCH"):
        validation_contracts.validate_raw_evidence(incompatible, validation_evidence())


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, "CLOCK_FIELD_INVALID"),
        ("not-a-clock", "CLOCK_FIELD_INVALID"),
        ("2026-08-29T00:00:00", "CLOCK_NOT_UTC"),
    ],
)
def test_oracle_clock_requires_parseable_utc(value: Any, reason: str) -> None:
    with pytest.raises(ContractNotVerifiable, match=reason):
        validation_oracle._utc(value)


@pytest.mark.parametrize("interval", ["", "bad", "0h", "1m"])
def test_oracle_interval_requires_positive_supported_duration(interval: str) -> None:
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_INTERVAL_MISMATCH"):
        validation_oracle._interval_hours(interval)


def test_oracle_cost_and_capacity_inputs_fail_closed(metric_policy: MetricOwnerPolicy) -> None:
    sample = validation_evidence()["samples"][0]

    missing_source = copy.deepcopy(sample)
    missing_source["cost_source_digests"].pop("funding")
    with pytest.raises(ContractNotVerifiable, match="COST_SOURCE_BINDINGS_INCOMPLETE"):
        validation_oracle._validate_cost_inputs(metric_policy, missing_source, interval="1h")

    boolean_cost = copy.deepcopy(sample)
    boolean_cost["cost_components_bps"]["funding"] = True
    with pytest.raises(ContractNotVerifiable, match="COST_COMPONENT_UNKNOWN"):
        validation_oracle._validate_cost_inputs(metric_policy, boolean_cost, interval="1h")

    overflowing_cost = copy.deepcopy(sample)
    overflowing_cost["cost_components_bps"]["funding"] = 10**10_000
    with pytest.raises(ContractNotVerifiable, match="COST_COMPONENT_UNKNOWN"):
        validation_oracle._validate_cost_inputs(metric_policy, overflowing_cost, interval="1h")

    non_numeric_capacity = copy.deepcopy(sample)
    non_numeric_capacity["market_volume_base"] = False
    with pytest.raises(ContractNotVerifiable, match="CAPACITY_INPUT_MISSING"):
        validation_oracle._validate_cost_inputs(metric_policy, non_numeric_capacity, interval="1h")

    overflowing_capacity = copy.deepcopy(sample)
    overflowing_capacity["market_volume_base"] = 10**10_000
    with pytest.raises(ContractNotVerifiable, match="CAPACITY_INPUT_MISSING"):
        validation_oracle._validate_cost_inputs(metric_policy, overflowing_capacity, interval="1h")

    non_positive_capacity = copy.deepcopy(sample)
    non_positive_capacity["price_quote"] = 0.0
    with pytest.raises(ContractNotVerifiable, match="CAPACITY_INPUT_MISSING"):
        validation_oracle._validate_cost_inputs(metric_policy, non_positive_capacity, interval="1h")


def test_oracle_sample_identity_alignment_and_eligibility(metric_policy: MetricOwnerPolicy) -> None:
    baseline = validation_evidence()

    not_object = copy.deepcopy(baseline)
    not_object["samples"] = [None]
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_FIELDS_INVALID"):
        validation_oracle._validate_samples(metric_policy, not_object)

    extra_field = copy.deepcopy(baseline)
    extra_field["samples"][0]["unexpected"] = True
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_FIELDS_INVALID"):
        validation_oracle._validate_samples(metric_policy, extra_field)

    duplicate_key = copy.deepcopy(baseline)
    duplicate_key["samples"][1]["key"] = duplicate_key["samples"][0]["key"]
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_KEY_DUPLICATE"):
        validation_oracle._validate_samples(metric_policy, duplicate_key)

    ineligible = copy.deepcopy(baseline)
    ineligible["samples"][0]["is_closed"] = False
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_NOT_ELIGIBLE"):
        validation_oracle._validate_samples(metric_policy, ineligible)

    wrong_interval = copy.deepcopy(baseline)
    wrong_interval["samples"][0]["interval"] = "4h"
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_INTERVAL_MISMATCH"):
        validation_oracle._validate_samples(metric_policy, wrong_interval)

    reversed_clock = copy.deepcopy(baseline)
    reversed_clock["samples"][0], reversed_clock["samples"][1] = (
        reversed_clock["samples"][1],
        reversed_clock["samples"][0],
    )
    with pytest.raises(ContractNotVerifiable, match="SAMPLE_CLOCK_NOT_STRICTLY_INCREASING"):
        validation_oracle._validate_samples(metric_policy, reversed_clock)

    incomplete_predictions = copy.deepcopy(baseline)
    incomplete_predictions["samples"][0]["candidate_predictions"].popitem()
    with pytest.raises(ContractNotVerifiable, match="FAMILY_PREDICTIONS_INCOMPLETE"):
        validation_oracle._validate_samples(metric_policy, incomplete_predictions)


def test_oracle_statistical_degenerate_cases_are_explicit(metric_policy: MetricOwnerPolicy) -> None:
    assert validation_oracle._pearson([1.0, 1.0], [2.0, 3.0]) == 0.0

    evidence = validation_evidence()
    wfo_policy = _metric_policy_with(
        metric_policy,
        lambda document: document["wfo"].__setitem__("min_folds_for_verdict", 99),
    )
    _, wfo_reasons, wfo_gate = validation_oracle._wfo_recomputation(
        wfo_policy, evidence, evidence["family"]["selected_candidate_id"]
    )
    assert "WFO_FOLDS_INSUFFICIENT" in wfo_reasons
    assert wfo_gate is False

    cpcv_policy = _metric_policy_with(
        metric_policy,
        lambda document: document["cpcv"].__setitem__("min_paths_for_verdict", 99),
    )
    _, cpcv_reasons, cpcv_gate, pbo_gate = validation_oracle._cpcv_recomputation(
        cpcv_policy, evidence, evidence["family"]["selected_candidate_id"]
    )
    assert "CPCV_PATHS_INSUFFICIENT" in cpcv_reasons
    assert cpcv_gate is pbo_gate is False

    one_candidate = {
        "family": {
            "candidates": [{"candidate_id": "only", "p_value": 0.01}],
            "selected_candidate_id": "only",
        }
    }
    result, gates, reasons = validation_oracle._multiple_testing(metric_policy, one_candidate, [0.1])
    assert "DSR_INPUTS_INSUFFICIENT" in reasons
    assert result["dsr_probability"] == 0.0
    assert gates["dsr"] is False

    two_candidates = {
        "family": {
            "candidates": [
                {"candidate_id": "selected", "p_value": 0.01},
                {"candidate_id": "other", "p_value": 0.02},
            ],
            "selected_candidate_id": "selected",
        }
    }
    invalid_se, invalid_gates, invalid_reasons = validation_oracle._multiple_testing(
        metric_policy, two_candidates, [-10.0, -3.0]
    )
    assert "DSR_STANDARD_ERROR_INVALID" in invalid_reasons
    assert invalid_se["sharpe_standard_error"] == 0.0
    assert invalid_gates["dsr"] is False


def test_stability_sample_contract_rejects_identity_and_shape_drift(metric_policy: MetricOwnerPolicy) -> None:
    evidence = validation_evidence()
    valid = evidence["stability"]["timeframe_1h_vs_1d"][0]
    family = evidence["family"]
    venue = evidence["samples"][0]["venue"]

    cases: list[tuple[Any, str]] = [(None, "STABILITY_SAMPLE_FIELDS_INVALID")]
    missing_capacity = copy.deepcopy(valid)
    missing_capacity.pop("market_volume_base")
    cases.append((missing_capacity, "CAPACITY_INPUT_MISSING"))
    extra_field = copy.deepcopy(valid)
    extra_field["unexpected"] = True
    cases.append((extra_field, "STABILITY_SAMPLE_FIELDS_INVALID"))
    wrong_interval = copy.deepcopy(valid)
    wrong_interval["interval"] = "1h"
    cases.append((wrong_interval, "STABILITY_INTERVAL_MISMATCH"))
    wrong_venue = copy.deepcopy(valid)
    wrong_venue["venue"] = "OTHER"
    cases.append((wrong_venue, "VENUE_IDENTITY_MISMATCH"))

    for sample, reason in cases:
        with pytest.raises(ContractNotVerifiable, match=reason):
            validation_oracle._net_return_from_stability_sample(
                metric_policy,
                sample,
                family=family,
                expected_venue=venue,
            )


def test_stability_policy_and_perturbations_are_fail_closed(metric_policy: MetricOwnerPolicy) -> None:
    baseline = validation_evidence()
    candidate_id = baseline["family"]["selected_candidate_id"]

    wrong_interval = _metric_policy_with(
        metric_policy,
        lambda document: document["sampling_clock"].__setitem__("stability_interval", "2d"),
    )
    with pytest.raises(ContractNotVerifiable, match="STABILITY_INTERVAL_POLICY_MISMATCH"):
        validation_oracle._stability_recomputation(wrong_interval, baseline, candidate_id)

    insufficient = copy.deepcopy(baseline)
    insufficient["stability"]["timeframe_1h_vs_1d"] = []
    insufficient["stability"]["parameter_perturbation"] = []
    _, reasons, gate = validation_oracle._stability_recomputation(metric_policy, insufficient, candidate_id)
    assert {"STABILITY_TIMEFRAME_SAMPLES_INSUFFICIENT", "STABILITY_PERTURBATIONS_INSUFFICIENT"} <= set(reasons)
    assert gate is False

    malformed = copy.deepcopy(baseline)
    malformed["stability"]["parameter_perturbation"][0]["unexpected"] = True
    with pytest.raises(ContractNotVerifiable, match="STABILITY_PERTURBATION_FIELDS_INVALID"):
        validation_oracle._stability_recomputation(metric_policy, malformed, candidate_id)

    wrong_delta = copy.deepcopy(baseline)
    wrong_delta["stability"]["parameter_perturbation"][0]["parameter_delta_pct"] = 0.2
    with pytest.raises(ContractNotVerifiable, match="STABILITY_PERTURBATION_POLICY_MISMATCH"):
        validation_oracle._stability_recomputation(metric_policy, wrong_delta, candidate_id)

    unknown_dimensions = _metric_policy_with(
        metric_policy,
        lambda document: document["stability"].__setitem__("dimensions", ["regime"]),
    )
    _, dimension_reasons, dimension_gate = validation_oracle._stability_recomputation(
        unknown_dimensions, baseline, candidate_id
    )
    assert "STABILITY_POLICY_DIMENSIONS_UNKNOWN" in dimension_reasons
    assert dimension_gate is False


def test_acceptance_artifact_writer_requires_policy_hash_and_bytes(
    metric_policy: MetricOwnerPolicy, tmp_path: Path
) -> None:
    result = validation_oracle._safe_result(status="FAIL", reasons=["unit"], policy=metric_policy, evidence=None)
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_SHA256_MISMATCH"):
        validation_oracle.write_acceptance_artifacts(
            tmp_path,
            policy=replace(metric_policy, source_sha256="0" * 64),
            recomputation=result,
            negative_results=[],
            mutation_outcomes={},
        )
    with pytest.raises(ContractNotVerifiable, match="POLICY_SOURCE_BYTES_MISMATCH"):
        validation_oracle.write_acceptance_artifacts(
            tmp_path,
            policy=replace(metric_policy, source_bytes=b"tampered"),
            recomputation=result,
            negative_results=[],
            mutation_outcomes={},
        )


def test_portfolio_strict_helpers_reject_ambiguous_sources_and_numbers(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(PortfolioNotVerifiable, match="DUPLICATE_JSON_KEY"):
        portfolio_contracts._load_json_strict(duplicate)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(PortfolioNotVerifiable, match="JSON_SOURCE_INVALID"):
        portfolio_contracts._load_json_strict(invalid)

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(PortfolioNotVerifiable, match="JSON_SOURCE_NOT_OBJECT"):
        portfolio_contracts._load_json_strict(non_object)

    with pytest.raises(PortfolioNotVerifiable, match="NUMBER_INVALID"):
        portfolio_contracts._number(True, "NUMBER_INVALID")
    with pytest.raises(PortfolioNotVerifiable, match="NUMBER_INVALID"):
        portfolio_contracts._number(0.0, "NUMBER_INVALID", positive=True)
    with pytest.raises(PortfolioNotVerifiable, match="CLOCK_FIELD_INVALID"):
        portfolio_contracts._utc(None)
    with pytest.raises(PortfolioNotVerifiable, match="CLOCK_FIELD_INVALID"):
        portfolio_contracts._utc("not-a-clock")


def test_portfolio_policy_loader_enforces_custody_digest_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_SOURCE_UNREADABLE"):
        portfolio_contracts.load_portfolio_owner_policy(tmp_path / "missing.json")

    wrong_bytes = tmp_path / "wrong.json"
    wrong_bytes.write_text("{}", encoding="utf-8")
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_SOURCE_SHA256_MISMATCH"):
        portfolio_contracts.load_portfolio_owner_policy(wrong_bytes)
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_SOURCE_INVALID"):
        portfolio_contracts.load_portfolio_owner_policy(object())  # type: ignore[arg-type]
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_DIGEST_MISSING"):
        portfolio_contracts.load_portfolio_owner_policy({})

    document = _owner_policy_document()
    mapped = portfolio_contracts.load_portfolio_owner_policy(document)
    assert mapped.source_sha256 == "MAPPING_INPUT"
    assert mapped.source_bytes == b""

    mutated = copy.deepcopy(document)
    mutated["uncertainty"]["replicates"] += 1
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_DIGEST_MISMATCH"):
        portfolio_contracts.load_portfolio_owner_policy(mutated)

    wrong_identity = copy.deepcopy(document)
    wrong_identity["status"] = "DRAFT"
    digest_scope = copy.deepcopy(wrong_identity)
    del digest_scope["digest"]["policy_digest"]
    changed_digest = portfolio_contracts.canonical_digest(digest_scope)
    wrong_identity["digest"]["policy_digest"] = changed_digest
    monkeypatch.setattr(portfolio_contracts, "APPROVED_POLICY_DIGEST", changed_digest)
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_IDENTITY_MISMATCH"):
        portfolio_contracts.load_portfolio_owner_policy(wrong_identity)


def test_portfolio_identity_contract_rejects_missing_or_unbound_fields() -> None:
    candidate = portfolio_evidence()["candidate_identity"]
    with pytest.raises(PortfolioNotVerifiable, match="CANDIDATE_IDENTITY_FIELDS_INVALID"):
        portfolio_contracts._validate_identity({}, "candidate")

    empty_id = copy.deepcopy(candidate)
    empty_id["candidate_id"] = ""
    with pytest.raises(PortfolioNotVerifiable, match="CANDIDATE_IDENTITY_FIELDS_INVALID"):
        portfolio_contracts._validate_identity(empty_id, "candidate")

    empty_version = copy.deepcopy(candidate)
    empty_version["candidate_version"] = ""
    with pytest.raises(PortfolioNotVerifiable, match="CANDIDATE_IDENTITY_FIELDS_INVALID"):
        portfolio_contracts._validate_identity(empty_version, "candidate")

    invalid_digest = copy.deepcopy(candidate)
    invalid_digest["candidate_spec_digest"] = "not-a-digest"
    with pytest.raises(PortfolioNotVerifiable, match="CANDIDATE_IDENTITY_DIGEST_INVALID"):
        portfolio_contracts._validate_identity(invalid_digest, "candidate")


def test_portfolio_evidence_rejects_non_mapping_and_top_level_drift(owner_policy: PortfolioOwnerPolicy) -> None:
    with pytest.raises(PortfolioNotVerifiable, match="EVIDENCE_NOT_OBJECT"):
        portfolio_contracts.validate_decision_evidence(owner_policy, [])  # type: ignore[arg-type]

    invalid_fields = portfolio_evidence()
    invalid_fields["unexpected"] = True
    reseal_portfolio(invalid_fields)
    with pytest.raises(PortfolioNotVerifiable, match="EVIDENCE_FIELDS_INVALID"):
        portfolio_contracts.validate_decision_evidence(owner_policy, invalid_fields)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.__setitem__("portfolio_id", ""), "PORTFOLIO_ID_INVALID"),
        (lambda value: value.__setitem__("supersedes_decision_id", "bad"), "SUPERSESSION_ID_INVALID"),
        (lambda value: value["bindings"].pop("tail_evidence_digest"), "BINDINGS_INCOMPLETE"),
        (lambda value: value["bindings"].__setitem__("tail_evidence_digest", "A" * 64), "BINDING_DIGEST_INVALID"),
        (lambda value: value["constraints"].pop("max_leverage"), "CONSTRAINT_INPUT_MISSING"),
        (lambda value: value["constraints"].__setitem__("snapshot_digest", "0" * 64), "CONSTRAINT_BINDING_MISMATCH"),
        (lambda value: value["uncertainty_spec"].__setitem__("unexpected", True), "UNCERTAINTY_INPUT_MISSING"),
        (
            lambda value: value["uncertainty_spec"].__setitem__("source_digest", "0" * 64),
            "UNCERTAINTY_BINDING_MISMATCH",
        ),
        (lambda value: value.__setitem__("bars", value["bars"][:1]), "ALIGNED_SAMPLES_INSUFFICIENT"),
        (lambda value: value["bars"][0].__setitem__("unexpected", True), "BAR_FIELDS_INVALID"),
        (lambda value: value["bars"][0]["key"].__setitem__("interval", "4h"), "CLOCK_INTERVAL_MISMATCH"),
        (
            lambda value: value["bars"][0].__setitem__(
                "combined_weights", copy.deepcopy(value["bars"][0]["champion_weights"])
            ),
            "PORTFOLIO_WEIGHTS_INVALID",
        ),
        (
            lambda value: value["bars"][0]["incremental_cost_components_bps"].__setitem__("market_impact", 9.0),
            "COST_IMPACT_BINDING_MISMATCH",
        ),
        (lambda value: value["bars"][0].__setitem__("regime", "unknown"), "REGIME_INPUT_INVALID"),
        (
            lambda value: value["bindings"].__setitem__("cost_evidence_digest", "0" * 64),
            "DIMENSION_EVIDENCE_BINDING_MISMATCH",
        ),
        (lambda value: value.__setitem__("input_bundle_digest", "0" * 64), "INPUT_BUNDLE_DIGEST_MISMATCH"),
    ],
)
def test_portfolio_evidence_contract_rejects_semantic_mutations(
    owner_policy: PortfolioOwnerPolicy, mutation: Any, reason: str
) -> None:
    evidence = portfolio_evidence()
    mutation(evidence)
    if reason != "INPUT_BUNDLE_DIGEST_MISMATCH":
        reseal_portfolio(evidence)
    with pytest.raises(PortfolioNotVerifiable, match=reason):
        portfolio_contracts.validate_decision_evidence(owner_policy, evidence)


def test_portfolio_evidence_binds_identity_alignment_order_and_dimension_hashes(
    owner_policy: PortfolioOwnerPolicy,
) -> None:
    identity_mismatch = portfolio_evidence()
    for role in ("candidate_identity", "champion_identity"):
        identity_mismatch[role]["dataset_manifest_digest"] = "9" * 64
        identity_mismatch[role]["identity_digest"] = _identity_digest(identity_mismatch[role])
    reseal_portfolio(identity_mismatch)
    with pytest.raises(PortfolioNotVerifiable, match="IDENTITY_BINDING_MISMATCH"):
        portfolio_contracts.validate_decision_evidence(owner_policy, identity_mismatch)

    duplicate = portfolio_evidence()
    duplicate["bars"][1]["key"] = copy.deepcopy(duplicate["bars"][0]["key"])
    duplicate["expected_keys"][1] = copy.deepcopy(duplicate["bars"][1]["key"])
    reseal_portfolio(duplicate)
    with pytest.raises(PortfolioNotVerifiable, match="DUPLICATE_ALIGNMENT_KEY"):
        portfolio_contracts.validate_decision_evidence(owner_policy, duplicate)

    out_of_order = portfolio_evidence()
    out_of_order["bars"][0], out_of_order["bars"][1] = out_of_order["bars"][1], out_of_order["bars"][0]
    out_of_order["expected_keys"][0], out_of_order["expected_keys"][1] = (
        out_of_order["expected_keys"][1],
        out_of_order["expected_keys"][0],
    )
    reseal_portfolio(out_of_order)
    with pytest.raises(PortfolioNotVerifiable, match="ALIGNMENT_KEYS_NOT_STRICTLY_INCREASING"):
        portfolio_contracts.validate_decision_evidence(owner_policy, out_of_order)


def test_portfolio_decision_math_and_constraint_mapping_are_explicit() -> None:
    assert portfolio_decision._std([1.0]) == 0.0
    assert portfolio_decision._correlation([1.0], [1.0, 2.0]) == 0.0
    assert portfolio_decision._correlation([1.0, 1.0], [1.0, 2.0]) == 0.0

    evidence = portfolio_evidence()
    evidence["constraints"]["asset_venues"].pop("CANDIDATE")
    gate, metrics = portfolio_decision._constraint_gate(evidence)
    assert gate is False
    assert {item["constraint"] for item in metrics["violations"]} == {"asset_mapping"}


def test_portfolio_artifact_writer_rejects_untrusted_policy_bytes(
    owner_policy: PortfolioOwnerPolicy, tmp_path: Path
) -> None:
    tampered = replace(owner_policy, source_bytes=b"tampered")
    with pytest.raises(PortfolioNotVerifiable, match="POLICY_SOURCE_CUSTODY_MISMATCH"):
        portfolio_decision.write_decision_artifacts(
            tmp_path,
            policy=tampered,
            decision=_decision("a" * 64),
            counterexamples=[],
        )


def test_decision_store_rejects_invalid_jsonl_shapes(tmp_path: Path) -> None:
    not_object = tmp_path / "not-object.jsonl"
    not_object.write_text("[]\n", encoding="utf-8")
    with pytest.raises(DecisionStoreNotVerifiable, match="DECISION_RECORD_NOT_OBJECT"):
        AppendOnlyDecisionStore(not_object).replay()

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("{\n", encoding="utf-8")
    with pytest.raises(DecisionStoreNotVerifiable, match="DECISION_STORE_INVALID_JSONL"):
        AppendOnlyDecisionStore(invalid).replay()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda records: records[0].__setitem__("sequence", 2), "DECISION_SEQUENCE_INVALID"),
        (lambda records: records[0].__setitem__("previous_record_hash", "f" * 64), "DECISION_CHAIN_FORKED"),
        (lambda records: records[0].__setitem__("decision", []), "DECISION_PAYLOAD_INVALID"),
        (lambda records: records[0].__setitem__("decision_digest", "f" * 64), "DECISION_DIGEST_MISMATCH"),
        (lambda records: records[0].__setitem__("record_hash", "f" * 64), "DECISION_RECORD_HASH_MISMATCH"),
    ],
)
def test_decision_store_detects_hash_chain_tampering(tmp_path: Path, mutation: Any, reason: str) -> None:
    records, _, _ = _valid_store_records(tmp_path)
    mutation(records)
    with pytest.raises(DecisionStoreNotVerifiable, match=reason):
        AppendOnlyDecisionStore(tmp_path / "unused.jsonl")._validate(records)


def test_decision_store_validates_identity_side_effect_and_supersession_semantics(tmp_path: Path) -> None:
    records, _, _ = _valid_store_records(tmp_path)
    invalid_id = copy.deepcopy(records[:1])
    invalid_id[0]["decision"]["decision_id"] = "short"
    _reseal_store_record(invalid_id[0])
    with pytest.raises(DecisionStoreNotVerifiable, match="DECISION_ID_INVALID"):
        AppendOnlyDecisionStore(tmp_path / "unused-id.jsonl")._validate(invalid_id)

    side_effect = copy.deepcopy(records[:1])
    side_effect[0]["decision"]["promotable"] = True
    _reseal_store_record(side_effect[0])
    with pytest.raises(DecisionStoreNotVerifiable, match="RUNTIME_SIDE_EFFECT_REACHABLE"):
        AppendOnlyDecisionStore(tmp_path / "unused-side-effect.jsonl")._validate(side_effect)

    bad_genesis = copy.deepcopy(records[:1])
    bad_genesis[0]["decision"]["supersedes_decision_id"] = "0" * 64
    _reseal_store_record(bad_genesis[0])
    with pytest.raises(DecisionStoreNotVerifiable, match="SUPERSESSION_CHAIN_INVALID"):
        AppendOnlyDecisionStore(tmp_path / "unused-genesis.jsonl")._validate(bad_genesis)

    bad_second = copy.deepcopy(records)
    bad_second[1]["decision"]["supersedes_decision_id"] = "0" * 64
    _reseal_store_record(bad_second[1])
    with pytest.raises(DecisionStoreNotVerifiable, match="SUPERSESSION_CHAIN_INVALID"):
        AppendOnlyDecisionStore(tmp_path / "unused-second.jsonl")._validate(bad_second)


def test_decision_store_append_is_idempotent_and_rejects_conflicts(tmp_path: Path) -> None:
    first_id = "3" * 64
    store = AppendOnlyDecisionStore(tmp_path / "idempotent.jsonl")
    first = _decision(first_id)
    record = store.append(first)
    assert store.append(first) == record

    conflict = replace(first, status="FAIL")
    with pytest.raises(DecisionStoreError, match="DECISION_ID_CONFLICT"):
        store.append(conflict)

    with pytest.raises(DecisionStoreError, match="RUNTIME_SIDE_EFFECT_REACHABLE"):
        AppendOnlyDecisionStore(tmp_path / "side-effect.jsonl").append(_decision("4" * 64, promotable=True))

    with pytest.raises(DecisionStoreError, match="SUPERSESSION_CHAIN_INVALID"):
        AppendOnlyDecisionStore(tmp_path / "bad-genesis.jsonl").append(_decision("5" * 64, "0" * 64))

    with pytest.raises(DecisionStoreError, match="SUPERSESSION_CHAIN_INVALID"):
        store.append(_decision("6" * 64, "0" * 64))
