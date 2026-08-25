"""Single fail-closed gate for turning completed research into promotable evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_json


@dataclass(frozen=True)
class PromotableExperimentDecision:
    """Promotion is true only when every independent binding is verifiable."""

    status: str
    reasons: tuple[str, ...]
    promotable: bool
    synthetic_economic_evidence: bool = False


def validate_promotable_experiment(
    *,
    runner_result: Mapping[str, Any] | None = None,
    lineage_manifest: Any | None = None,
    lineage_root: str | Path | None = None,
    identity: Any | None = None,
    checkpoint: Any | None = None,
    oos_store: Any | None = None,
    oos_seal: Any | None = None,
    oos_receipt: Any | None = None,
    oos_boundary: Any | None = None,
    boundary_key: bytes | None = None,
    audit_key: bytes | None = None,
) -> PromotableExperimentDecision:
    """Validate the real ExperimentRun/checkpoint and sealed-OOS promotion seam.

    Resumable research is intentionally allowed to finish without this data,
    but its result remains ``NOT_VERIFIABLE`` and cannot be promoted.  No
    implicit latest/default lineage or receipt-as-trust-anchor fallback exists.
    """

    reasons: list[str] = []
    lineage_inputs = (lineage_manifest, lineage_root, identity, checkpoint)
    oos_inputs = (oos_store, oos_seal, oos_receipt, oos_boundary, boundary_key, audit_key)
    if any(value is None for value in lineage_inputs):
        reasons.append("MISSING_PIT_LINEAGE")
    if any(value is None for value in oos_inputs):
        reasons.append("MISSING_OOS_SEAL")
    if not isinstance(runner_result, Mapping):
        reasons.append("MISSING_RUNNER_RESULT")
    elif runner_result.get("status") != "COMPLETED":
        reasons.append("RUNNER_NOT_COMPLETED")

    if reasons:
        return _decision(reasons)

    assert runner_result is not None
    assert lineage_manifest is not None and lineage_root is not None
    assert identity is not None and checkpoint is not None
    assert oos_store is not None and oos_seal is not None and oos_receipt is not None
    assert oos_boundary is not None and boundary_key is not None and audit_key is not None

    try:
        from beidou_research.data.pit_lineage import inspect_lineage, require_experiment_binding

        lineage_report = inspect_lineage(
            lineage_manifest.as_dict(),
            root=lineage_root,
            expected_manifest_digest=lineage_manifest.digest,
        )
        if lineage_report.status != "VERIFIABLE":
            reasons.extend(f"PIT:{reason}" for reason in lineage_report.reasons)
        require_experiment_binding(lineage_manifest, identity, checkpoint)
    except Exception as exc:
        reasons.append(f"PIT_EXPERIMENT_BINDING_INVALID:{type(exc).__name__}:{exc}")

    if runner_result.get("run_id") != identity.run_id:
        reasons.append("RUNNER_RUN_BINDING_MISMATCH")
    if runner_result.get("identity_digest") != identity.digest:
        reasons.append("RUNNER_IDENTITY_BINDING_MISMATCH")

    try:
        persisted_seal = oos_store.load_seal()
        if persisted_seal != oos_seal:
            reasons.append("IMMUTABLE_OOS_SEAL_MISMATCH")
        if (
            oos_seal.lineage_digest != lineage_manifest.digest
            or oos_seal.run_id != identity.run_id
            or oos_seal.experiment_identity_digest != identity.digest
            or oos_seal.checkpoint_digest != checkpoint.digest
        ):
            reasons.append("OOS_EXPERIMENT_BINDING_MISMATCH")
        oos_status = oos_store.promotion_status(
            seal=oos_seal,
            receipt=oos_receipt,
            boundary=oos_boundary,
            key=boundary_key,
            audit_key=audit_key,
            candidate_evaluation_digest=hashlib.sha256(canonical_json(dict(runner_result)).encode("utf-8")).hexdigest(),
        )
        if oos_status.status != "PROMOTABLE":
            reasons.extend(f"OOS:{reason}" for reason in oos_status.reasons)
    except Exception as exc:
        reasons.append(f"OOS_PROMOTION_BINDING_INVALID:{type(exc).__name__}:{exc}")

    return _decision(reasons)


def _decision(reasons: list[str]) -> PromotableExperimentDecision:
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return PromotableExperimentDecision(status="NOT_VERIFIABLE", reasons=unique, promotable=False)
    return PromotableExperimentDecision(status="PROMOTABLE", reasons=(), promotable=True)
