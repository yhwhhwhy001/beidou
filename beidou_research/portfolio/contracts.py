"""Pure contracts for the offline T08 portfolio decision."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from beidou_research.validation.contracts import ACCEPTED_T06_RESULT_SHA256, canonical_digest, canonical_json

SCHEMA_VERSION = "1.0.0"
APPROVED_POLICY_ID = "BD-AF-P3-T08-PORTFOLIO-OWNER-POLICY"
APPROVED_POLICY_VERSION = "1.0.0"
APPROVED_POLICY_SHA256 = "fdbe1751800f6ea92daba31e489b99efa8f71c017f1a9ffb5f51e12ab2356698"
APPROVED_POLICY_DIGEST = "deee8320e477b2e48ec82eeca50ac7358adc77eec62cfa3bd4b4e27055630363"
ACCEPTED_T07_RESULT_SHA256 = "9d0d66e79e2645f82a83b36f262b99a5bbb81614f45abb3f9e2cc1d7ce24774f"
ACCEPTED_T07_POLICY_DIGEST = "96dbcd31d32ffb53c532a24634c2c85082e4f7d045fd1da24d8521d9495d08b9"
GENESIS_HASH = "0" * 64

COST_COMPONENTS = frozenset({"maker_fee", "taker_fee", "half_spread", "slippage", "funding", "market_impact"})
# Frozen T08 policy's impact curve: basis points per 10k quote notional.
IMPACT_BPS_PER_10K = 0.1
REQUIRED_REGIMES = frozenset({"bull", "range", "crisis", "conflict", "cost_shock"})
REQUIRED_BINDINGS = frozenset(
    {
        "portfolio_policy_digest",
        "t07_result_sha256",
        "t07_metric_owner_policy_digest",
        "t06_result_sha256",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "lineage_manifest_digest",
        "oos_seal_digest",
        "cost_evidence_digest",
        "constraint_evidence_digest",
        "correlation_evidence_digest",
        "turnover_evidence_digest",
        "capacity_evidence_digest",
        "tail_evidence_digest",
        "regime_evidence_digest",
        "uncertainty_evidence_digest",
    }
)


class PortfolioNotVerifiableError(ValueError):
    """The input cannot support a portfolio decision."""


PortfolioNotVerifiable = PortfolioNotVerifiableError


@dataclass(frozen=True)
class PortfolioOwnerPolicy:
    document: dict[str, Any]
    digest: str
    source_sha256: str
    source_bytes: bytes


@dataclass(frozen=True)
class PortfolioDecision:
    status: str
    disposition: str
    reasons: tuple[str, ...]
    hard_gates: dict[str, bool]
    recomputed: dict[str, Any]
    decision_id: str
    portfolio_id: str
    candidate_id: str
    champion_id: str
    candidate_identity_digest: str
    champion_identity_digest: str
    policy_digest: str
    input_bundle_digest: str
    decision_timestamp_utc: str
    supersedes_decision_id: str | None
    promotable: bool = False
    side_effects: tuple[str, ...] = ()
    synthetic_economic_evidence: bool = True
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "hard_gates": self.hard_gates,
            "recomputed": self.recomputed,
            "decision_id": self.decision_id,
            "portfolio_id": self.portfolio_id,
            "candidate_id": self.candidate_id,
            "champion_id": self.champion_id,
            "candidate_identity_digest": self.candidate_identity_digest,
            "champion_identity_digest": self.champion_identity_digest,
            "policy_digest": self.policy_digest,
            "input_bundle_digest": self.input_bundle_digest,
            "decision_timestamp_utc": self.decision_timestamp_utc,
            "supersedes_decision_id": self.supersedes_decision_id,
            "promotable": self.promotable,
            "side_effects": list(self.side_effects),
            "synthetic_economic_evidence": self.synthetic_economic_evidence,
        }


def _load_json_strict(path: Path) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PortfolioNotVerifiable("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PortfolioNotVerifiable("JSON_SOURCE_INVALID") from exc
    if not isinstance(value, dict):
        raise PortfolioNotVerifiable("JSON_SOURCE_NOT_OBJECT")
    return value


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PortfolioNotVerifiable("NON_FINITE_INPUT")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _number(value: Any, reason: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioNotVerifiable(reason)
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0) or (nonnegative and result < 0.0):
        raise PortfolioNotVerifiable(reason)
    return result


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PortfolioNotVerifiable("CLOCK_FIELD_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioNotVerifiable("CLOCK_FIELD_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PortfolioNotVerifiable("CLOCK_NOT_UTC")
    return parsed.astimezone(timezone.utc)


def load_portfolio_owner_policy(source: str | Path | Mapping[str, Any]) -> PortfolioOwnerPolicy:
    """Load the exact frozen Owner policy; mappings cannot prove source custody."""

    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise PortfolioNotVerifiable("POLICY_SOURCE_UNREADABLE") from exc
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if source_sha256 != APPROVED_POLICY_SHA256:
            raise PortfolioNotVerifiable("POLICY_SOURCE_SHA256_MISMATCH")
        document = _load_json_strict(path)
    elif isinstance(source, Mapping):
        source_bytes = b""
        source_sha256 = "MAPPING_INPUT"
        document = copy.deepcopy(dict(source))
    else:
        raise PortfolioNotVerifiable("POLICY_SOURCE_INVALID")
    _reject_non_finite(document)
    try:
        embedded = document["digest"]["policy_digest"]
        scope = copy.deepcopy(document)
        del scope["digest"]["policy_digest"]
    except (KeyError, TypeError) as exc:
        raise PortfolioNotVerifiable("POLICY_DIGEST_MISSING") from exc
    actual = canonical_digest(scope)
    if embedded != actual or actual != APPROVED_POLICY_DIGEST:
        raise PortfolioNotVerifiable("POLICY_DIGEST_MISMATCH")
    if (
        document.get("policy_id") != APPROVED_POLICY_ID
        or document.get("policy_version") != APPROVED_POLICY_VERSION
        or document.get("status") != "FROZEN_OWNER_APPROVED"
        or document.get("source_context", {}).get("t07_task_result_sha256") != ACCEPTED_T07_RESULT_SHA256
        or document.get("source_context", {}).get("t07_metric_owner_policy_digest") != ACCEPTED_T07_POLICY_DIGEST
    ):
        raise PortfolioNotVerifiable("POLICY_IDENTITY_MISMATCH")
    return PortfolioOwnerPolicy(
        document=document, digest=actual, source_sha256=source_sha256, source_bytes=source_bytes
    )


def _identity_fields(prefix: str) -> frozenset[str]:
    return frozenset(
        {
            f"{prefix}_id",
            f"{prefix}_version",
            f"{prefix}_spec_digest",
            f"{prefix}_policy_digest",
            "dataset_manifest_digest",
            "pit_manifest_digest",
            "lineage_manifest_digest",
            "oos_seal_digest",
            "experiment_run_digest",
            "checkpoint_digest",
            "cost_model_digest",
            "constraint_policy_digest",
            "identity_digest",
        }
    )


def _validate_identity(identity: Any, prefix: str) -> None:
    if not isinstance(identity, dict) or set(identity) != _identity_fields(prefix):
        raise PortfolioNotVerifiable(f"{prefix.upper()}_IDENTITY_FIELDS_INVALID")
    if not isinstance(identity[f"{prefix}_id"], str) or not identity[f"{prefix}_id"]:
        raise PortfolioNotVerifiable(f"{prefix.upper()}_IDENTITY_FIELDS_INVALID")
    if not isinstance(identity[f"{prefix}_version"], str) or not identity[f"{prefix}_version"]:
        raise PortfolioNotVerifiable(f"{prefix.upper()}_IDENTITY_FIELDS_INVALID")
    digest_fields = set(identity) - {f"{prefix}_id", f"{prefix}_version"}
    if not all(_hex64(identity[field]) for field in digest_fields):
        raise PortfolioNotVerifiable(f"{prefix.upper()}_IDENTITY_DIGEST_INVALID")
    scope = {key: value for key, value in identity.items() if key != "identity_digest"}
    if canonical_digest(scope) != identity["identity_digest"]:
        raise PortfolioNotVerifiable(f"{prefix.upper()}_IDENTITY_DIGEST_MISMATCH")


def dimension_scopes(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return the raw immutable scope independently bound for each decision dimension."""

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


def validate_decision_evidence(policy: PortfolioOwnerPolicy, source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate identities, custody, aligned raw inputs, and independent dimension hashes."""

    if not isinstance(source, Mapping):
        raise PortfolioNotVerifiable("EVIDENCE_NOT_OBJECT")
    _reject_non_finite(source)
    evidence = copy.deepcopy(dict(source))
    if "uncertainty_spec" not in evidence:
        raise PortfolioNotVerifiable("UNCERTAINTY_INPUT_MISSING")
    required_top = {
        "schema_version",
        "bindings",
        "portfolio_id",
        "evaluation_timestamp_utc",
        "decision_timestamp_utc",
        "candidate_identity",
        "champion_identity",
        "constraints",
        "uncertainty_spec",
        "expected_keys",
        "bars",
        "supersedes_decision_id",
        "input_bundle_digest",
    }
    if set(evidence) != required_top or evidence["schema_version"] != SCHEMA_VERSION:
        raise PortfolioNotVerifiable("EVIDENCE_FIELDS_INVALID")
    if not isinstance(evidence["portfolio_id"], str) or not evidence["portfolio_id"]:
        raise PortfolioNotVerifiable("PORTFOLIO_ID_INVALID")
    _utc(evidence["evaluation_timestamp_utc"])
    _utc(evidence["decision_timestamp_utc"])
    supersedes = evidence["supersedes_decision_id"]
    if supersedes is not None and not _hex64(supersedes):
        raise PortfolioNotVerifiable("SUPERSESSION_ID_INVALID")

    bindings = evidence["bindings"]
    if not isinstance(bindings, dict) or set(bindings) != REQUIRED_BINDINGS:
        raise PortfolioNotVerifiable("BINDINGS_INCOMPLETE")
    if not all(_hex64(value) for value in bindings.values()):
        raise PortfolioNotVerifiable("BINDING_DIGEST_INVALID")
    if (
        bindings["portfolio_policy_digest"] != policy.digest
        or bindings["t06_result_sha256"] != ACCEPTED_T06_RESULT_SHA256
        or bindings["t07_result_sha256"] != ACCEPTED_T07_RESULT_SHA256
        or bindings["t07_metric_owner_policy_digest"] != ACCEPTED_T07_POLICY_DIGEST
    ):
        raise PortfolioNotVerifiable("DEPENDENCY_BINDING_MISMATCH")

    candidate = evidence["candidate_identity"]
    champion = evidence["champion_identity"]
    _validate_identity(candidate, "candidate")
    _validate_identity(champion, "champion")
    same_fields = {
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "lineage_manifest_digest",
        "oos_seal_digest",
        "experiment_run_digest",
        "checkpoint_digest",
        "cost_model_digest",
        "constraint_policy_digest",
    }
    if any(candidate[field] != champion[field] for field in same_fields):
        raise PortfolioNotVerifiable("IDENTITY_BINDING_MISMATCH")
    if any(
        candidate[field] != bindings[field]
        for field in ("dataset_manifest_digest", "pit_manifest_digest", "lineage_manifest_digest", "oos_seal_digest")
    ):
        raise PortfolioNotVerifiable("IDENTITY_BINDING_MISMATCH")

    constraints = evidence["constraints"]
    constraint_fields = {
        "constraint_policy_digest",
        "max_gross_exposure",
        "max_abs_net_exposure",
        "max_leverage",
        "max_asset_weight",
        "max_universe_exposure",
        "max_turnover_pct_per_bar",
        "max_participation_rate",
        "venue_limits",
        "min_cash_buffer",
        "asset_universes",
        "asset_venues",
        "snapshot_digest",
    }
    if not isinstance(constraints, dict) or set(constraints) != constraint_fields:
        raise PortfolioNotVerifiable("CONSTRAINT_INPUT_MISSING")
    constraint_scope = {key: value for key, value in constraints.items() if key != "snapshot_digest"}
    if (
        not _hex64(constraints["snapshot_digest"])
        or canonical_digest(constraint_scope) != constraints["snapshot_digest"]
        or constraints["snapshot_digest"] != bindings["constraint_evidence_digest"]
        or constraints["constraint_policy_digest"] != candidate["constraint_policy_digest"]
    ):
        raise PortfolioNotVerifiable("CONSTRAINT_BINDING_MISMATCH")

    uncertainty = evidence["uncertainty_spec"]
    if not isinstance(uncertainty, dict) or set(uncertainty) != {"method", "replicates", "seed", "source_digest"}:
        raise PortfolioNotVerifiable("UNCERTAINTY_INPUT_MISSING")
    uncertainty_scope = {key: value for key, value in uncertainty.items() if key != "source_digest"}
    if (
        uncertainty["method"] != "moving-block-bootstrap"
        or uncertainty["replicates"] != int(policy.document["uncertainty"]["replicates"])
        or isinstance(uncertainty["seed"], bool)
        or not isinstance(uncertainty["seed"], int)
        or uncertainty["seed"] < 0
        or canonical_digest(uncertainty_scope) != uncertainty["source_digest"]
        or uncertainty["source_digest"] != bindings["uncertainty_evidence_digest"]
    ):
        raise PortfolioNotVerifiable("UNCERTAINTY_BINDING_MISMATCH")

    bars = evidence["bars"]
    expected_keys = evidence["expected_keys"]
    if not isinstance(bars, list) or len(bars) < 2 or not isinstance(expected_keys, list):
        raise PortfolioNotVerifiable("ALIGNED_SAMPLES_INSUFFICIENT")
    if len(bars) != len(expected_keys):
        raise PortfolioNotVerifiable("EXPECTED_KEY_RECONCILIATION_MISMATCH")
    bar_fields = {
        "key",
        "asset_returns",
        "champion_weights",
        "combined_weights",
        "champion_cash_buffer",
        "combined_cash_buffer",
        "champion_cost_components_bps",
        "incremental_cost_components_bps",
        "champion_turnover_pct",
        "combined_turnover_pct",
        "portfolio_nav_quote",
        "champion_order_notional_quote",
        "incremental_order_notional_quote",
        "market_volume_quote",
        "regime",
    }
    key_fields = {"venue", "symbol", "interval", "observation_time", "horizon_bars", "revision"}
    seen_keys: set[str] = set()
    prior_order: tuple[str, str, str, datetime, int, int] | None = None
    candidate_id = candidate["candidate_id"]
    regime_counts = dict.fromkeys(REQUIRED_REGIMES, 0)
    for index, bar in enumerate(bars):
        if not isinstance(bar, dict) or set(bar) != bar_fields:
            if isinstance(bar, dict) and "market_volume_quote" not in bar:
                raise PortfolioNotVerifiable("CAPACITY_INPUT_MISSING")
            raise PortfolioNotVerifiable("BAR_FIELDS_INVALID")
        key = bar["key"]
        if not isinstance(key, dict) or set(key) != key_fields or key != expected_keys[index]:
            raise PortfolioNotVerifiable("EXPECTED_KEY_RECONCILIATION_MISMATCH")
        observed = _utc(key["observation_time"])
        if key["interval"] != policy.document["clock_and_units"]["primary_sampling_interval"]:
            raise PortfolioNotVerifiable("CLOCK_INTERVAL_MISMATCH")
        order = (
            str(key["venue"]),
            str(key["symbol"]),
            str(key["interval"]),
            observed,
            int(key["horizon_bars"]),
            int(key["revision"]),
        )
        key_digest = canonical_digest(key)
        if key_digest in seen_keys:
            raise PortfolioNotVerifiable("DUPLICATE_ALIGNMENT_KEY")
        if prior_order is not None and order <= prior_order:
            raise PortfolioNotVerifiable("ALIGNMENT_KEYS_NOT_STRICTLY_INCREASING")
        seen_keys.add(key_digest)
        prior_order = order
        returns = bar["asset_returns"]
        champion_weights = bar["champion_weights"]
        combined_weights = bar["combined_weights"]
        if (
            not isinstance(returns, dict)
            or not isinstance(champion_weights, dict)
            or not isinstance(combined_weights, dict)
            or set(returns) != set(combined_weights)
            or not set(champion_weights) < set(combined_weights)
            or candidate_id not in combined_weights
        ):
            raise PortfolioNotVerifiable("PORTFOLIO_WEIGHTS_INVALID")
        for mapping in (returns, champion_weights, combined_weights):
            for value in mapping.values():
                _number(value, "PORTFOLIO_VALUE_INVALID")
        for name in ("champion_cost_components_bps", "incremental_cost_components_bps"):
            costs = bar[name]
            if not isinstance(costs, dict) or set(costs) != COST_COMPONENTS:
                raise PortfolioNotVerifiable("COST_INPUT_MISSING")
            for value in costs.values():
                _number(value, "COST_INPUT_UNKNOWN", positive=True)
        _number(bar["portfolio_nav_quote"], "COST_INPUT_MISSING", positive=True)
        champion_order = _number(bar["champion_order_notional_quote"], "COST_INPUT_MISSING", positive=True)
        incremental_order = _number(bar["incremental_order_notional_quote"], "COST_INPUT_MISSING", positive=True)
        # Impact is not a free-standing bps fixture: bind it to the frozen
        # notional curve so a tampered component cannot silently lower cost.
        expected_champion_impact = IMPACT_BPS_PER_10K * champion_order / 10_000.0
        expected_incremental_impact = IMPACT_BPS_PER_10K * incremental_order / 10_000.0
        if not math.isclose(
            float(bar["champion_cost_components_bps"]["market_impact"]),
            expected_champion_impact,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ) or not math.isclose(
            float(bar["incremental_cost_components_bps"]["market_impact"]),
            expected_incremental_impact,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise PortfolioNotVerifiable("COST_IMPACT_BINDING_MISMATCH")
        _number(bar["champion_turnover_pct"], "TURNOVER_INPUT_MISSING", nonnegative=True)
        _number(bar["combined_turnover_pct"], "TURNOVER_INPUT_MISSING", nonnegative=True)
        _number(bar["incremental_order_notional_quote"], "CAPACITY_INPUT_MISSING", positive=True)
        _number(bar["market_volume_quote"], "CAPACITY_INPUT_MISSING", positive=True)
        _number(bar["champion_cash_buffer"], "CONSTRAINT_INPUT_MISSING", nonnegative=True)
        _number(bar["combined_cash_buffer"], "CONSTRAINT_INPUT_MISSING", nonnegative=True)
        if bar["regime"] not in REQUIRED_REGIMES:
            raise PortfolioNotVerifiable("REGIME_INPUT_INVALID")
        regime_counts[bar["regime"]] += 1
    minimum_regime = int(policy.document["regime_stability"]["minimum_samples_per_regime"])
    if any(count < minimum_regime for count in regime_counts.values()):
        raise PortfolioNotVerifiable("REGIME_INPUT_MISSING")

    for binding, scope in dimension_scopes(evidence).items():
        if canonical_digest(scope) != bindings[binding]:
            raise PortfolioNotVerifiable("DIMENSION_EVIDENCE_BINDING_MISMATCH")
    input_scope = {key: value for key, value in evidence.items() if key != "input_bundle_digest"}
    if not _hex64(evidence["input_bundle_digest"]) or canonical_digest(input_scope) != evidence["input_bundle_digest"]:
        raise PortfolioNotVerifiable("INPUT_BUNDLE_DIGEST_MISMATCH")
    return evidence


__all__ = [
    "APPROVED_POLICY_DIGEST",
    "APPROVED_POLICY_SHA256",
    "GENESIS_HASH",
    "PortfolioDecision",
    "PortfolioNotVerifiable",
    "PortfolioOwnerPolicy",
    "canonical_digest",
    "canonical_json",
    "dimension_scopes",
    "load_portfolio_owner_policy",
    "validate_decision_evidence",
]
