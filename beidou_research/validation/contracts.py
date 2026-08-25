"""Canonical T07 policy, raw-evidence, and result contracts.

These contracts intentionally do not import factor-mining calculators.  The
Metric Owner policy is an external custody artifact and is accepted only when
its source bytes and canonical semantic digest match the signed H2 binding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

APPROVED_POLICY_ID = "BD-AF-P3-T07-METRIC-OWNER-POLICY"
APPROVED_POLICY_VERSION = "1.0.0"
APPROVED_POLICY_SHA256 = "2b7f5bf287432b6051e9252f525023b0c23dfa17c2c26aa4e95ab18002e61f70"
APPROVED_POLICY_DIGEST = "96dbcd31d32ffb53c532a24634c2c85082e4f7d045fd1da24d8521d9495d08b9"
SCHEMA_VERSION = "1.0.0"
_HEX64 = frozenset("0123456789abcdef")


class ContractNotVerifiable(ValueError):  # noqa: N818 - domain status, not an ordinary exception category
    """Raised when canonical policy or evidence cannot be verified."""


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractNotVerifiable("NON_FINITE_EVIDENCE")
        return
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ContractNotVerifiable("NON_STRING_JSON_KEY")
        for item in value.values():
            _reject_non_finite(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)
        return
    raise ContractNotVerifiable(f"UNSUPPORTED_CANONICAL_TYPE:{type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return the frozen compact JSON representation for the policy domain."""

    _reject_non_finite(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _load_json_strict(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractNotVerifiable(f"DUPLICATE_JSON_KEY:{key}")
            result[key] = value
        return result

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractNotVerifiable(f"POLICY_SOURCE_UNREADABLE:{type(exc).__name__}") from exc
    if not isinstance(loaded, dict):
        raise ContractNotVerifiable("POLICY_NOT_OBJECT")
    return loaded


@dataclass(frozen=True)
class MetricOwnerPolicy:
    """Verified frozen Metric Owner policy."""

    document: dict[str, Any]
    digest: str
    source_sha256: str


@dataclass(frozen=True)
class ScientificValidationResult:
    """Immutable result of independent recomputation."""

    status: str
    reasons: tuple[str, ...]
    hard_gates: dict[str, bool]
    recomputed: dict[str, Any]
    policy_digest: str
    family_digest: str
    raw_evidence_digest: str
    implementation_agreement: str
    promotable: bool = False
    synthetic_economic_evidence: bool = True
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "reasons": list(self.reasons),
            "hard_gates": self.hard_gates,
            "recomputed": self.recomputed,
            "policy_digest": self.policy_digest,
            "family_digest": self.family_digest,
            "raw_evidence_digest": self.raw_evidence_digest,
            "implementation_agreement": self.implementation_agreement,
            "promotable": self.promotable,
            "synthetic_economic_evidence": self.synthetic_economic_evidence,
        }


def load_metric_owner_policy(source: str | Path | Mapping[str, Any]) -> MetricOwnerPolicy:
    """Load and verify the exact H2 policy bytes and semantic digest.

    Mapping input is supported for adversarial mutation tests.  Only a file
    source can prove the signed source-byte SHA; mapping input must still match
    the approved canonical digest.
    """

    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise ContractNotVerifiable(f"POLICY_SOURCE_UNREADABLE:{type(exc).__name__}") from exc
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if source_sha256 != APPROVED_POLICY_SHA256:
            raise ContractNotVerifiable("POLICY_SOURCE_SHA256_MISMATCH")
        document = _load_json_strict(path)
    elif isinstance(source, Mapping):
        document = copy.deepcopy(dict(source))
        source_sha256 = "MAPPING_INPUT"
    else:
        raise ContractNotVerifiable("POLICY_SOURCE_INVALID")

    try:
        embedded_digest = document["digest"]["policy_digest"]
        digest_scope = copy.deepcopy(document)
        del digest_scope["digest"]["policy_digest"]
    except (KeyError, TypeError) as exc:
        raise ContractNotVerifiable("POLICY_DIGEST_MISSING") from exc
    actual_digest = canonical_digest(digest_scope)
    if embedded_digest != actual_digest or actual_digest != APPROVED_POLICY_DIGEST:
        raise ContractNotVerifiable("POLICY_DIGEST_MISMATCH")
    if (
        document.get("policy_id") != APPROVED_POLICY_ID
        or document.get("policy_version") != APPROVED_POLICY_VERSION
        or document.get("status") != "FROZEN_OWNER_APPROVED"
    ):
        raise ContractNotVerifiable("POLICY_IDENTITY_MISMATCH")
    return MetricOwnerPolicy(document=document, digest=actual_digest, source_sha256=source_sha256)


def validate_raw_evidence(policy: MetricOwnerPolicy, source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable evidence structure and return an isolated copy."""

    if not isinstance(source, Mapping):
        raise ContractNotVerifiable("EVIDENCE_NOT_OBJECT")
    _reject_non_finite(source)
    evidence = copy.deepcopy(dict(source))
    required_top = {
        "schema_version",
        "bindings",
        "family",
        "samples",
        "recorded_wfo_folds",
        "recorded_cpcv_paths",
        "stability",
        "implementation_claim",
        "raw_evidence_digest",
    }
    if set(evidence) != required_top:
        raise ContractNotVerifiable("EVIDENCE_FIELDS_INVALID")
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise ContractNotVerifiable("EVIDENCE_SCHEMA_VERSION_UNKNOWN")
    supplied_digest = evidence["raw_evidence_digest"]
    digest_scope = {key: value for key, value in evidence.items() if key != "raw_evidence_digest"}
    if not _is_hex64(supplied_digest) or canonical_digest(digest_scope) != supplied_digest:
        raise ContractNotVerifiable("RAW_EVIDENCE_DIGEST_MISMATCH")

    bindings = evidence["bindings"]
    required_bindings = {
        "policy_digest",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "lineage_manifest_digest",
        "oos_seal_digest",
        "oos_audit_head",
        "experiment_identity_digest",
        "checkpoint_digest",
    }
    if not isinstance(bindings, dict) or set(bindings) != required_bindings:
        raise ContractNotVerifiable("BINDINGS_INCOMPLETE")
    if bindings["policy_digest"] != policy.digest or not all(_is_hex64(value) for value in bindings.values()):
        raise ContractNotVerifiable("BINDING_DIGEST_INVALID")

    family = evidence["family"]
    required_family = {
        "policy_id",
        "policy_version",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "universe",
        "primary_interval",
        "horizon_bars",
        "generator_scope",
        "search_scope",
        "family_id",
        "family_registry_digest",
        "frozen_before_evaluation",
        "declared_denominator",
        "selected_candidate_id",
        "candidates",
    }
    if not isinstance(family, dict) or set(family) != required_family:
        raise ContractNotVerifiable("FAMILY_FIELDS_INVALID")
    basis_keys = {
        "policy_id",
        "policy_version",
        "dataset_manifest_digest",
        "pit_manifest_digest",
        "universe",
        "primary_interval",
        "horizon_bars",
        "generator_scope",
        "search_scope",
    }
    if canonical_digest({key: family[key] for key in basis_keys}) != family["family_id"]:
        raise ContractNotVerifiable("FAMILY_ID_MISMATCH")
    if (
        family["policy_id"] != APPROVED_POLICY_ID
        or family["policy_version"] != APPROVED_POLICY_VERSION
        or family["dataset_manifest_digest"] != bindings["dataset_manifest_digest"]
        or family["pit_manifest_digest"] != bindings["pit_manifest_digest"]
        or family["primary_interval"] != policy.document["sampling_clock"]["primary_interval"]
        or family["horizon_bars"] != policy.document["sampling_clock"]["horizon_bars"]
    ):
        raise ContractNotVerifiable("FAMILY_BINDING_MISMATCH")
    candidates = family["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ContractNotVerifiable("FAMILY_EMPTY")
    if canonical_digest(candidates) != family["family_registry_digest"]:
        raise ContractNotVerifiable("FAMILY_REGISTRY_DIGEST_MISMATCH")
    if family["frozen_before_evaluation"] is not True or family["declared_denominator"] != len(candidates):
        raise ContractNotVerifiable("FAMILY_DENOMINATOR_DRIFT")
    candidate_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {
            "candidate_id",
            "candidate_spec_digest",
            "evaluation_status",
            "p_value",
        }:
            raise ContractNotVerifiable("CANDIDATE_FIELDS_INVALID")
        candidate_id = candidate["candidate_id"]
        if candidate_id != canonical_digest(
            {"family_id": family["family_id"], "candidate_spec_digest": candidate["candidate_spec_digest"]}
        ):
            raise ContractNotVerifiable("CANDIDATE_ID_MISMATCH")
        if candidate["evaluation_status"] not in {"PASS", "FAIL", "NOT_VERIFIABLE", "ABORTED", "TIMED_OUT"}:
            raise ContractNotVerifiable("CANDIDATE_STATUS_INVALID")
        if not 0.0 <= candidate["p_value"] <= 1.0:
            raise ContractNotVerifiable("P_VALUE_OUT_OF_RANGE")
        candidate_ids.append(candidate_id)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ContractNotVerifiable("FAMILY_CANDIDATE_DUPLICATE")
    if family["selected_candidate_id"] not in candidate_ids:
        raise ContractNotVerifiable("SELECTED_CANDIDATE_UNKNOWN")

    if not isinstance(evidence["samples"], list) or not evidence["samples"]:
        raise ContractNotVerifiable("SAMPLES_MISSING")
    if not isinstance(evidence["recorded_wfo_folds"], list):
        raise ContractNotVerifiable("WFO_FOLDS_MISSING")
    if not isinstance(evidence["recorded_cpcv_paths"], list):
        raise ContractNotVerifiable("CPCV_PATHS_MISSING")
    if not isinstance(evidence["stability"], dict):
        raise ContractNotVerifiable("STABILITY_EVIDENCE_INVALID")
    claim = evidence["implementation_claim"]
    if not isinstance(claim, dict) or set(claim) != {"status", "family_denominator", "policy_digest"}:
        raise ContractNotVerifiable("IMPLEMENTATION_CLAIM_INVALID")
    if claim["status"] not in {"PASS", "FAIL", "NOT_VERIFIABLE"}:
        raise ContractNotVerifiable("IMPLEMENTATION_STATUS_INVALID")
    return evidence


__all__ = [
    "APPROVED_POLICY_DIGEST",
    "APPROVED_POLICY_ID",
    "APPROVED_POLICY_SHA256",
    "APPROVED_POLICY_VERSION",
    "ContractNotVerifiable",
    "MetricOwnerPolicy",
    "ScientificValidationResult",
    "canonical_digest",
    "canonical_json",
    "load_metric_owner_policy",
    "validate_raw_evidence",
]
