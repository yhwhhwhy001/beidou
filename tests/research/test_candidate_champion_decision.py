"""BD-AF-P3-T08: offline Candidate-versus-Champion decision proofs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from apps.factor_miner.__main__ import cli
from beidou_research.mining.selection.marginal_contribution import recompute_marginal_contribution
from beidou_research.portfolio import (
    AppendOnlyDecisionStore,
    DecisionStoreNotVerifiable,
    decide_candidate_vs_champion,
    load_portfolio_owner_policy,
    rollback_decision_history,
    write_decision_artifacts,
)
from beidou_research.validation.contracts import canonical_digest

POLICY_PATH = Path(
    "/Users/maguannan/beidou-results/BD-AF-P0P3-V5-T00-acceptance/BD-AF-P3-T08-policy-draft/portfolio-owner-policy.json"
)
POLICY_SHA256 = "fdbe1751800f6ea92daba31e489b99efa8f71c017f1a9ffb5f51e12ab2356698"
POLICY_DIGEST = "deee8320e477b2e48ec82eeca50ac7358adc77eec62cfa3bd4b4e27055630363"
T07_RESULT_SHA256 = "9d0d66e79e2645f82a83b36f262b99a5bbb81614f45abb3f9e2cc1d7ce24774f"
T07_POLICY_DIGEST = "96dbcd31d32ffb53c532a24634c2c85082e4f7d045fd1da24d8521d9495d08b9"
T06_RESULT_SHA256 = "cbd8de393f60794a0da2db32528a8b661a81732a068ebf03852e86bf7ab3679a"
REGIMES = ("bull", "range", "crisis", "conflict", "cost_shock")
COST_COMPONENTS = ("maker_fee", "taker_fee", "half_spread", "slippage", "funding", "market_impact")


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _identity(role: str, *, shared: dict[str, str]) -> dict[str, Any]:
    prefix = "candidate" if role == "candidate" else "champion"
    identity = {
        f"{prefix}_id": "CANDIDATE" if role == "candidate" else "CHAMPION",
        f"{prefix}_version": "1.0.0",
        f"{prefix}_spec_digest": _hex(f"{role}-spec"),
        f"{prefix}_policy_digest": T07_POLICY_DIGEST,
        "dataset_manifest_digest": shared["dataset_manifest_digest"],
        "pit_manifest_digest": shared["pit_manifest_digest"],
        "lineage_manifest_digest": shared["lineage_manifest_digest"],
        "oos_seal_digest": shared["oos_seal_digest"],
        "experiment_run_digest": shared["experiment_run_digest"],
        "checkpoint_digest": shared["checkpoint_digest"],
        "cost_model_digest": shared["cost_model_digest"],
        "constraint_policy_digest": shared["constraint_policy_digest"],
    }
    identity["identity_digest"] = canonical_digest(identity)
    return identity


def _key(index: int) -> dict[str, Any]:
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    return {
        "venue": "BINANCE",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "observation_time": observed.isoformat(),
        "horizon_bars": 1,
        "revision": 0,
    }


def _costs(order_notional_quote: float) -> dict[str, float]:
    return {
        "maker_fee": 2.0,
        "taker_fee": 4.0,
        "half_spread": 0.5,
        "slippage": 1.0,
        "funding": 0.125,
        "market_impact": 0.1 * order_notional_quote / 10_000.0,
    }


def _dimension_scopes(evidence: dict[str, Any]) -> dict[str, Any]:
    bars = evidence["bars"]
    return {
        "cost_evidence_digest": [
            {
                "key": bar["key"],
                "champion": bar["champion_cost_components_bps"],
                "incremental": bar["incremental_cost_components_bps"],
                "champion_order": bar["champion_order_notional_quote"],
                "incremental_order": bar["incremental_order_notional_quote"],
                "portfolio_nav": bar["portfolio_nav_quote"],
            }
            for bar in bars
        ],
        "correlation_evidence_digest": [{"key": bar["key"], "asset_returns": bar["asset_returns"]} for bar in bars],
        "turnover_evidence_digest": [
            {
                "key": bar["key"],
                "champion": bar["champion_turnover_pct"],
                "combined": bar["combined_turnover_pct"],
            }
            for bar in bars
        ],
        "capacity_evidence_digest": [
            {
                "key": bar["key"],
                "order": bar["incremental_order_notional_quote"],
                "volume": bar["market_volume_quote"],
            }
            for bar in bars
        ],
        "tail_evidence_digest": [
            {
                "key": bar["key"],
                "asset_returns": bar["asset_returns"],
                "champion_weights": bar["champion_weights"],
                "combined_weights": bar["combined_weights"],
            }
            for bar in bars
        ],
        "regime_evidence_digest": [{"key": bar["key"], "regime": bar["regime"]} for bar in bars],
    }


def _refresh_bindings(evidence: dict[str, Any]) -> None:
    constraints = evidence["constraints"]
    constraint_scope = {key: value for key, value in constraints.items() if key != "snapshot_digest"}
    constraints["snapshot_digest"] = canonical_digest(constraint_scope)
    evidence["bindings"]["constraint_evidence_digest"] = constraints["snapshot_digest"]
    uncertainty = evidence["uncertainty_spec"]
    uncertainty_scope = {key: value for key, value in uncertainty.items() if key != "source_digest"}
    uncertainty["source_digest"] = canonical_digest(uncertainty_scope)
    evidence["bindings"]["uncertainty_evidence_digest"] = uncertainty["source_digest"]
    for binding, scope in _dimension_scopes(evidence).items():
        evidence["bindings"][binding] = canonical_digest(scope)


def _reseal(evidence: dict[str, Any], *, refresh_bindings: bool = False) -> dict[str, Any]:
    if refresh_bindings:
        _refresh_bindings(evidence)
    scope = {key: value for key, value in evidence.items() if key != "input_bundle_digest"}
    evidence["input_bundle_digest"] = canonical_digest(scope)
    return evidence


def _baseline_evidence() -> dict[str, Any]:
    shared = {
        "dataset_manifest_digest": _hex("dataset"),
        "pit_manifest_digest": _hex("pit"),
        "lineage_manifest_digest": _hex("lineage"),
        "oos_seal_digest": _hex("oos"),
        "experiment_run_digest": _hex("run"),
        "checkpoint_digest": _hex("checkpoint"),
        "cost_model_digest": _hex("cost-model"),
        "constraint_policy_digest": _hex("constraint-policy"),
    }
    candidate = _identity("candidate", shared=shared)
    champion = _identity("champion", shared=shared)
    bars: list[dict[str, Any]] = []
    for index in range(250):
        wave = math.sin(index / 4.0) + 0.35 * math.sin(index / 13.0)
        core_return = 0.0015 + 0.006 * wave
        candidate_return = 0.0060 - 0.0045 * wave
        bars.append(
            {
                "key": _key(index),
                "asset_returns": {"CORE": core_return, "CANDIDATE": candidate_return},
                "champion_weights": {"CORE": 1.0},
                "combined_weights": {"CORE": 0.85, "CANDIDATE": 0.15},
                "champion_cash_buffer": 0.0,
                "combined_cash_buffer": 0.0,
                "champion_cost_components_bps": _costs(10_000.0),
                "incremental_cost_components_bps": _costs(1_000.0),
                "champion_turnover_pct": 0.02,
                "combined_turnover_pct": 0.04,
                "portfolio_nav_quote": 1_000_000.0,
                "champion_order_notional_quote": 10_000.0,
                "incremental_order_notional_quote": 1_000.0,
                "market_volume_quote": 1_000_000.0,
                "regime": REGIMES[index // 50],
            }
        )
    evidence = {
        "schema_version": "1.0.0",
        "bindings": {
            "portfolio_policy_digest": POLICY_DIGEST,
            "t07_result_sha256": T07_RESULT_SHA256,
            "t07_metric_owner_policy_digest": T07_POLICY_DIGEST,
            "t06_result_sha256": T06_RESULT_SHA256,
            **{
                key: shared[key]
                for key in (
                    "dataset_manifest_digest",
                    "pit_manifest_digest",
                    "lineage_manifest_digest",
                    "oos_seal_digest",
                )
            },
            "cost_evidence_digest": "",
            "constraint_evidence_digest": "",
            "correlation_evidence_digest": "",
            "turnover_evidence_digest": "",
            "capacity_evidence_digest": "",
            "tail_evidence_digest": "",
            "regime_evidence_digest": "",
            "uncertainty_evidence_digest": "",
        },
        "portfolio_id": "PORTFOLIO-001",
        "evaluation_timestamp_utc": "2026-02-01T00:00:00+00:00",
        "decision_timestamp_utc": "2026-02-01T00:01:00+00:00",
        "candidate_identity": candidate,
        "champion_identity": champion,
        "constraints": {
            "constraint_policy_digest": shared["constraint_policy_digest"],
            "max_gross_exposure": 1.0,
            "max_abs_net_exposure": 1.0,
            "max_leverage": 1.0,
            "max_asset_weight": 1.0,
            "max_universe_exposure": 1.0,
            "max_turnover_pct_per_bar": 0.50,
            "max_participation_rate": 0.10,
            "venue_limits": {"BINANCE": 1.0},
            "min_cash_buffer": 0.0,
            "asset_universes": {"CORE": "CRYPTO", "CANDIDATE": "CRYPTO"},
            "asset_venues": {"CORE": "BINANCE", "CANDIDATE": "BINANCE"},
            "snapshot_digest": "",
        },
        "uncertainty_spec": {
            "method": "moving-block-bootstrap",
            "replicates": 10_000,
            "seed": 20260825,
            "source_digest": "",
        },
        "expected_keys": [bar["key"] for bar in bars],
        "bars": bars,
        "supersedes_decision_id": None,
    }
    return _reseal(evidence, refresh_bindings=True)


@pytest.fixture(scope="module")
def policy():
    return load_portfolio_owner_policy(POLICY_PATH)


@pytest.fixture(scope="module")
def positive_result(policy):
    return decide_candidate_vs_champion(policy, _baseline_evidence())


def test_frozen_owner_policy_source_and_digest(policy) -> None:
    assert policy.source_sha256 == POLICY_SHA256
    assert policy.digest == POLICY_DIGEST
    assert hashlib.sha256(policy.source_bytes).hexdigest() == POLICY_SHA256
    assert policy.document["status"] == "FROZEN_OWNER_APPROVED"


def test_diversifying_candidate_is_shadow_eligible_but_never_promotable(positive_result) -> None:
    assert positive_result.status == "PASS"
    assert positive_result.disposition == "SHADOW_ELIGIBLE"
    assert positive_result.hard_gates and all(positive_result.hard_gates.values())
    assert positive_result.promotable is False
    assert positive_result.side_effects == ()


def test_high_standalone_sharpe_redundant_candidate_is_rejected(policy) -> None:
    evidence = _baseline_evidence()
    for bar in evidence["bars"]:
        bar["asset_returns"]["CANDIDATE"] = bar["asset_returns"]["CORE"] + 0.004
    _reseal(evidence, refresh_bindings=True)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "FAIL"
    assert result.disposition == "REJECT"
    assert result.hard_gates["correlation"] is False
    assert result.recomputed["standalone_candidate_annualized_sharpe"] > 1.0


def test_costly_candidate_is_rejected_from_incremental_value(policy) -> None:
    evidence = _baseline_evidence()
    for bar in evidence["bars"]:
        bar["asset_returns"]["CANDIDATE"] = bar["asset_returns"]["CORE"] + 0.0003
        bar["incremental_order_notional_quote"] = 100_000.0
        bar["incremental_cost_components_bps"] = _costs(100_000.0)
        bar["combined_turnover_pct"] = 0.50
    _reseal(evidence, refresh_bindings=True)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "FAIL"
    assert result.disposition == "REJECT"
    assert result.hard_gates["turnover_cost"] is False
    assert result.promotable is False


def test_tail_worsening_candidate_is_rejected(policy) -> None:
    evidence = _baseline_evidence()
    for index, bar in enumerate(evidence["bars"]):
        if bar["regime"] == "crisis" and index % 7 == 0:
            bar["asset_returns"]["CANDIDATE"] = -0.25
    _reseal(evidence, refresh_bindings=True)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "FAIL"
    assert result.hard_gates["tail"] is False


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.pop("uncertainty_spec"), "UNCERTAINTY_INPUT_MISSING"),
        (lambda value: value["bars"][0].pop("market_volume_quote"), "CAPACITY_INPUT_MISSING"),
        (
            lambda value: [bar.__setitem__("regime", "bull") for bar in value["bars"] if bar["regime"] == "crisis"],
            "REGIME_INPUT_MISSING",
        ),
        (
            lambda value: value["bars"][0]["incremental_cost_components_bps"].pop("funding"),
            "COST_INPUT_MISSING",
        ),
    ],
)
def test_missing_required_dimension_is_not_verifiable(policy, mutation, reason: str) -> None:
    evidence = _baseline_evidence()
    mutation(evidence)
    _reseal(evidence)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert result.disposition == "NOT_VERIFIABLE"
    assert reason in result.reasons


@pytest.mark.parametrize("binding", ["t06_result_sha256", "t07_result_sha256", "t07_metric_owner_policy_digest"])
def test_unaccepted_dependency_binding_is_not_verifiable(policy, binding: str) -> None:
    evidence = _baseline_evidence()
    evidence["bindings"][binding] = _hex(f"tampered-{binding}")
    _reseal(evidence)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "DEPENDENCY_BINDING_MISMATCH" in result.reasons


def test_tampered_champion_identity_is_not_verifiable(policy) -> None:
    evidence = _baseline_evidence()
    evidence["champion_identity"]["identity_digest"] = _hex("tampered-champion")
    _reseal(evidence)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "CHAMPION_IDENTITY_DIGEST_MISMATCH" in result.reasons


@pytest.mark.parametrize(
    "field", ["dataset_manifest_digest", "oos_seal_digest", "cost_model_digest", "constraint_policy_digest"]
)
def test_candidate_champion_identity_mismatch_is_not_verifiable(policy, field: str) -> None:
    evidence = _baseline_evidence()
    evidence["champion_identity"][field] = _hex(f"other-{field}")
    identity = {key: value for key, value in evidence["champion_identity"].items() if key != "identity_digest"}
    evidence["champion_identity"]["identity_digest"] = canonical_digest(identity)
    _reseal(evidence)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert "IDENTITY_BINDING_MISMATCH" in result.reasons


@pytest.mark.parametrize("mutation", ["drop", "duplicate", "out_of_order", "non_utc", "non_finite"])
def test_alignment_clock_and_finite_values_fail_closed(policy, mutation: str) -> None:
    evidence = _baseline_evidence()
    if mutation == "drop":
        evidence["bars"].pop()
    elif mutation == "duplicate":
        evidence["bars"][1]["key"] = copy.deepcopy(evidence["bars"][0]["key"])
    elif mutation == "out_of_order":
        evidence["bars"][0], evidence["bars"][1] = evidence["bars"][1], evidence["bars"][0]
    elif mutation == "non_utc":
        evidence["bars"][0]["key"]["observation_time"] = "2026-01-01T00:00:00+08:00"
    else:
        evidence["bars"][0]["asset_returns"]["CORE"] = float("nan")
    if mutation != "non_finite":
        _reseal(evidence, refresh_bindings=True)
    result = decide_candidate_vs_champion(policy, evidence)
    assert result.status == "NOT_VERIFIABLE"
    assert result.promotable is False


def test_marginal_recomputation_rejects_silent_alignment_truncation() -> None:
    with pytest.raises(ValueError, match="RETURN_ALIGNMENT_MISMATCH"):
        recompute_marginal_contribution([0.01, 0.02], [0.01])


def test_append_only_store_and_supersession_chain(policy, positive_result, tmp_path: Path) -> None:
    store = AppendOnlyDecisionStore(tmp_path / "decisions.jsonl")
    first = store.append(positive_result)
    evidence = _baseline_evidence()
    evidence["decision_timestamp_utc"] = "2026-02-01T00:02:00+00:00"
    evidence["supersedes_decision_id"] = positive_result.decision_id
    _reseal(evidence)
    second_result = decide_candidate_vs_champion(policy, evidence)
    second = store.append(second_result)
    replay = store.replay()
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["previous_record_hash"] == first["record_hash"]
    assert second_result.supersedes_decision_id == positive_result.decision_id
    assert replay["decision_ids"] == [positive_result.decision_id, second_result.decision_id]
    assert not hasattr(store, "update") and not hasattr(store, "delete")


def test_tampered_store_is_not_verifiable(policy, positive_result, tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    store = AppendOnlyDecisionStore(path)
    store.append(positive_result)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["decision"]["disposition"] = (
        "SHADOW_ELIGIBLE" if positive_result.disposition != "SHADOW_ELIGIBLE" else "REJECT"
    )
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(DecisionStoreNotVerifiable):
        store.replay()


def test_rollback_preserves_history_and_has_no_runtime_side_effect(policy, positive_result, tmp_path: Path) -> None:
    store = AppendOnlyDecisionStore(tmp_path / "decisions.jsonl")
    store.append(positive_result)
    before = store.path.read_bytes()
    rollback = rollback_decision_history(store)
    assert store.path.read_bytes() == before
    assert rollback["new_decisions_enabled"] is False
    assert rollback["history_preserved"] is True
    assert rollback["paper_testnet_account_capital_side_effects"] is False


def test_acceptance_artifacts_preserve_policy_bytes_and_offline_inventory(
    policy, positive_result, tmp_path: Path
) -> None:
    evidence_dir = Path(os.environ.get("BEIDOU_EVIDENCE_DIR", tmp_path))
    write_decision_artifacts(
        evidence_dir,
        policy=policy,
        decision=positive_result,
        counterexamples=[{"id": "redundant", "disposition": "REJECT"}],
    )
    assert hashlib.sha256((evidence_dir / "portfolio-owner-policy.json").read_bytes()).hexdigest() == POLICY_SHA256
    assert json.loads((evidence_dir / "candidate-champion-decision.json").read_text())["promotable"] is False
    inventory = json.loads((evidence_dir / "side-effect-inventory.json").read_text())
    assert inventory == {
        "account_queries": 0,
        "capital_allocations": 0,
        "network_calls": 0,
        "order_actions": 0,
        "paper_promotions": 0,
        "testnet_promotions": 0,
    }


def test_compare_cli_is_offline_and_persists_only_local_decision(policy, tmp_path: Path) -> None:
    evidence_path = tmp_path / "input.json"
    evidence_path.write_text(json.dumps(_baseline_evidence(), sort_keys=True), encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "compare",
            "--candidate",
            "CANDIDATE",
            "--champion",
            "CHAMPION",
            "--evidence",
            str(evidence_path),
            "--policy",
            str(POLICY_PATH),
            "--store",
            str(tmp_path / "decisions.jsonl"),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["disposition"] == "SHADOW_ELIGIBLE"
    assert payload["promotable"] is False
    assert (tmp_path / "decisions.jsonl").is_file()
