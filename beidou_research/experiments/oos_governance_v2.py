"""Governed OOS v2 bindings layered over the immutable v1 seal/audit facts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from beidou_research.data.dataset_manifest import (
    ECONOMIC_RESEARCH,
    PUBLIC_READ_ONLY,
    MarketDataProvenance,
)

from .contracts import ExperimentRunIdentity, canonical_json
from .oos_seal import OOSAccessReceipt, OOSBoundary, OOSSeal, OOSSealError, OOSSealStore

OOS_GOVERNANCE_V2_SCHEMA_VERSION = "2.0"
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UPPER_ID = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}")
_OWNER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_INTERVAL = re.compile(r"[1-9][0-9]*(?:m|h|d|w|M)")
_GOVERNED_STORE_AUTHORITY = object()


class OOSGovernanceV2Error(RuntimeError):
    """Stable failure for preseal, governed seal, receipt, or promotion."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _domain_digest(domain: str, value: Mapping[str, Any]) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OOSGovernanceV2Error("OOS_V2_TIMESTAMP_NOT_AWARE")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OOSGovernanceV2Error("OOS_V2_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OOSGovernanceV2Error("OOS_V2_TIMESTAMP_INVALID") from exc
    if _canonical_time(parsed) != value:
        raise OOSGovernanceV2Error("OOS_V2_TIMESTAMP_NON_CANONICAL")
    return parsed


def _require_digest(value: object, code: str) -> None:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None or set(value) == {"0"}:
        raise OOSGovernanceV2Error(code)


def _require_exact_id(value: str, pattern: re.Pattern[str], code: str) -> None:
    if not isinstance(value, str) or value != value.strip() or pattern.fullmatch(value) is None:
        raise OOSGovernanceV2Error(code)


@dataclass(frozen=True, slots=True)
class OOSWindowCustodySpecV2:
    boundary: OOSBoundary
    venue: str
    symbols: tuple[str, ...]
    interval: str
    source_endpoint: str
    source_contract_digest: str
    dataset_manifest_digest: str
    pit_manifest_digest: str
    custodian_id: str
    custody_sealed_at: datetime
    first_read_not_before: datetime
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION:
            raise OOSGovernanceV2Error("OOS_WINDOW_SCHEMA_UNSUPPORTED")
        if not isinstance(self.boundary, OOSBoundary):
            raise OOSGovernanceV2Error("OOS_BOUNDARY_V1_TYPE_REQUIRED")
        _require_exact_id(self.venue, _UPPER_ID, "OOS_VENUE_INVALID")
        if self.venue != "BINANCE_USDM":
            raise OOSGovernanceV2Error("OOS_VENUE_NOT_AUTHORIZED")
        if not self.symbols or tuple(sorted(set(self.symbols))) != self.symbols:
            raise OOSGovernanceV2Error("OOS_SYMBOLS_NOT_CANONICAL")
        for symbol in self.symbols:
            _require_exact_id(symbol, _UPPER_ID, "OOS_SYMBOL_INVALID")
        if self.interval != self.interval.strip() or _INTERVAL.fullmatch(self.interval) is None:
            raise OOSGovernanceV2Error("OOS_INTERVAL_INVALID")
        if not isinstance(self.source_endpoint, str) or self.source_endpoint != self.source_endpoint.strip():
            raise OOSGovernanceV2Error("OOS_SOURCE_ENDPOINT_INVALID")
        provenance = MarketDataProvenance.from_endpoint(
            self.source_endpoint,
            retrieved_at=_canonical_time(self.custody_sealed_at),
            venue=self.venue,
        )
        if provenance.environment != PUBLIC_READ_ONLY or provenance.intended_use != ECONOMIC_RESEARCH:
            raise OOSGovernanceV2Error("OOS_SOURCE_NOT_ECONOMIC_RESEARCH")
        for value, code in (
            (self.source_contract_digest, "OOS_SOURCE_CONTRACT_DIGEST_INVALID"),
            (self.dataset_manifest_digest, "OOS_DATASET_MANIFEST_DIGEST_INVALID"),
            (self.pit_manifest_digest, "OOS_PIT_MANIFEST_DIGEST_INVALID"),
        ):
            _require_digest(value, code)
        _require_exact_id(self.custodian_id, _OWNER_ID, "OOS_CUSTODIAN_ID_INVALID")
        custody = self.custody_sealed_at.astimezone(timezone.utc)
        first_read = self.first_read_not_before.astimezone(timezone.utc)
        _canonical_time(self.first_read_not_before)
        if custody >= first_read:
            raise OOSGovernanceV2Error("OOS_CUSTODY_NOT_BEFORE_FIRST_READ")

    @property
    def source_class(self) -> str:
        return MarketDataProvenance.from_endpoint(
            self.source_endpoint,
            retrieved_at=_canonical_time(self.custody_sealed_at),
            venue=self.venue,
        ).source_class

    @property
    def canonical_universe(self) -> str:
        return f"{self.venue}|{','.join(self.symbols)}"

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "boundary": self.boundary.as_dict(),
            "venue": self.venue,
            "symbols": list(self.symbols),
            "interval": self.interval,
            "source_endpoint": self.source_endpoint,
            "source_class": self.source_class,
            "source_contract_digest": self.source_contract_digest,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "pit_manifest_digest": self.pit_manifest_digest,
            "custodian_id": self.custodian_id,
            "custody_sealed_at": _canonical_time(self.custody_sealed_at),
            "first_read_not_before": _canonical_time(self.first_read_not_before),
        }

    @property
    def digest(self) -> str:
        return _domain_digest("beidou.oos.window-custody.v2", self.canonical_payload())


@dataclass(frozen=True, slots=True)
class OOSCandidateFreezeV2:
    code_commit: str
    code_tree_digest: str
    strategy_policy_digest: str
    metric_policy_digest: str
    cost_policy_digest: str
    metric_owner_id: str
    candidate_frozen_at: datetime
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION:
            raise OOSGovernanceV2Error("OOS_CANDIDATE_SCHEMA_UNSUPPORTED")
        if _HEX40.fullmatch(self.code_commit) is None:
            raise OOSGovernanceV2Error("OOS_CODE_COMMIT_INVALID")
        for value, code in (
            (self.code_tree_digest, "OOS_CODE_TREE_DIGEST_INVALID"),
            (self.strategy_policy_digest, "OOS_STRATEGY_POLICY_DIGEST_INVALID"),
            (self.metric_policy_digest, "OOS_METRIC_POLICY_DIGEST_INVALID"),
            (self.cost_policy_digest, "OOS_COST_POLICY_DIGEST_INVALID"),
        ):
            _require_digest(value, code)
        _require_exact_id(self.metric_owner_id, _OWNER_ID, "OOS_METRIC_OWNER_ID_INVALID")
        _canonical_time(self.candidate_frozen_at)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "code_commit": self.code_commit,
            "code_tree_digest": self.code_tree_digest,
            "strategy_policy_digest": self.strategy_policy_digest,
            "metric_policy_digest": self.metric_policy_digest,
            "cost_policy_digest": self.cost_policy_digest,
            "metric_owner_id": self.metric_owner_id,
            "candidate_frozen_at": _canonical_time(self.candidate_frozen_at),
        }

    @property
    def digest(self) -> str:
        return _domain_digest("beidou.oos.candidate-freeze.v2", self.canonical_payload())


@dataclass(frozen=True, slots=True)
class OOSPresealAssessmentV2:
    window: OOSWindowCustodySpecV2 | None
    candidate: OOSCandidateFreezeV2 | None
    status: str
    reasons: tuple[str, ...]
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION:
            raise OOSGovernanceV2Error("OOS_PRESEAL_SCHEMA_UNSUPPORTED")
        expected = _preseal_reasons(self.window, self.candidate)
        expected_status = "DRAFT_BLOCKED" if expected else "READY_FOR_V2_SEAL"
        if self.reasons != expected or self.status != expected_status:
            raise OOSGovernanceV2Error("OOS_PRESEAL_ASSESSMENT_INCONSISTENT")

    @property
    def eligible_for_v2_seal(self) -> bool:
        return self.status == "READY_FOR_V2_SEAL" and not self.reasons

    @property
    def assessment_digest(self) -> str:
        return _domain_digest(
            "beidou.oos.preseal-assessment.v2",
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "reasons": list(self.reasons),
                "window_custody_digest_or_null": self.window.digest if self.window else None,
                "candidate_freeze_digest_or_null": self.candidate.digest if self.candidate else None,
            },
        )

    @property
    def preseal_digest(self) -> str:
        self.require_ready()
        assert self.window is not None and self.candidate is not None
        return _domain_digest(
            "beidou.oos.preseal-package.v2",
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "window_custody_digest": self.window.digest,
                "candidate_freeze_digest": self.candidate.digest,
            },
        )

    @property
    def identity_policy_digest(self) -> str:
        self.require_ready()
        assert self.candidate is not None
        return _domain_digest(
            "beidou.oos.identity-policy.v2",
            {
                "schema_version": self.schema_version,
                "preseal_digest": self.preseal_digest,
                "candidate_freeze_digest": self.candidate.digest,
                "strategy_policy_digest": self.candidate.strategy_policy_digest,
                "metric_policy_digest": self.candidate.metric_policy_digest,
                "cost_policy_digest": self.candidate.cost_policy_digest,
                "metric_owner_id": self.candidate.metric_owner_id,
            },
        )

    def require_ready(self) -> OOSPresealAssessmentV2:
        if not self.eligible_for_v2_seal:
            raise OOSGovernanceV2Error("PRESEAL_NOT_READY", ",".join(self.reasons))
        return self


def _preseal_reasons(
    window: OOSWindowCustodySpecV2 | None,
    candidate: OOSCandidateFreezeV2 | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if window is None:
        reasons.append("MISSING_WINDOW_CUSTODY_SPEC")
    if candidate is None:
        reasons.append("MISSING_CANDIDATE_FREEZE")
    if window is not None and candidate is not None:
        custody = window.custody_sealed_at.astimezone(timezone.utc)
        frozen = candidate.candidate_frozen_at.astimezone(timezone.utc)
        first_read = window.first_read_not_before.astimezone(timezone.utc)
        if frozen < custody:
            reasons.append("CANDIDATE_FROZEN_BEFORE_CUSTODY")
        if frozen >= first_read:
            reasons.append("CANDIDATE_NOT_FROZEN_BEFORE_FIRST_READ")
    return tuple(reasons)


def assess_oos_preseal_v2(
    *,
    window: OOSWindowCustodySpecV2 | None,
    candidate: OOSCandidateFreezeV2 | None,
) -> OOSPresealAssessmentV2:
    reasons = _preseal_reasons(window, candidate)
    return OOSPresealAssessmentV2(
        window=window,
        candidate=candidate,
        status="DRAFT_BLOCKED" if reasons else "READY_FOR_V2_SEAL",
        reasons=reasons,
    )


def require_preseal_identity_binding_v2(
    preseal: OOSPresealAssessmentV2,
    identity: ExperimentRunIdentity,
) -> None:
    if type(preseal) is not OOSPresealAssessmentV2:
        raise OOSGovernanceV2Error("V2_TYPE_REQUIRED")
    preseal.require_ready()
    assert preseal.window is not None and preseal.candidate is not None
    window = preseal.window
    candidate = preseal.candidate
    mismatches: list[str] = []
    for name, actual, expected in (
        ("code_commit", identity.code_commit, candidate.code_commit),
        ("code_tree_digest", identity.code_tree_digest, candidate.code_tree_digest),
        ("policy_digest", identity.policy_digest, preseal.identity_policy_digest),
        ("dataset_manifest_digest", identity.dataset_manifest_digest, window.dataset_manifest_digest),
        ("pit_manifest_digest", identity.pit_manifest_digest, window.pit_manifest_digest),
        ("universe", identity.universe, window.canonical_universe),
        ("timeframe", identity.timeframe, window.interval),
        ("cost_model", identity.cost_model, candidate.cost_policy_digest),
    ):
        if actual != expected:
            mismatches.append(name)
    if mismatches:
        raise OOSGovernanceV2Error("PRESEAL_IDENTITY_BINDING_MISMATCH", ",".join(mismatches))


@dataclass(frozen=True, slots=True)
class GovernedOOSSealV2:
    preseal_digest: str
    window_custody_digest: str
    candidate_freeze_digest: str
    experiment_identity_digest: str
    legacy_seal_digest: str
    governed_at: str
    governed_seal_digest: str
    governance_version: int = 2
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION or self.governance_version != 2:
            raise OOSGovernanceV2Error("GOVERNED_SEAL_VERSION_INVALID")
        for value in (
            self.preseal_digest,
            self.window_custody_digest,
            self.candidate_freeze_digest,
            self.experiment_identity_digest,
            self.legacy_seal_digest,
            self.governed_seal_digest,
        ):
            _require_digest(value, "GOVERNED_SEAL_DIGEST_INVALID")
        _parse_time(self.governed_at)
        core = {
            "schema_version": self.schema_version,
            "governance_version": self.governance_version,
            "preseal_digest": self.preseal_digest,
            "window_custody_digest": self.window_custody_digest,
            "candidate_freeze_digest": self.candidate_freeze_digest,
            "experiment_identity_digest": self.experiment_identity_digest,
            "legacy_seal_digest": self.legacy_seal_digest,
            "governed_at": self.governed_at,
        }
        if self.governed_seal_digest != _domain_digest("beidou.oos.governed-seal.v2", core):
            raise OOSGovernanceV2Error("GOVERNED_SEAL_DIGEST_MISMATCH")

    @property
    def digest(self) -> str:
        return self.governed_seal_digest

    @property
    def promotion_scope(self) -> str:
        return "GOVERNED_OOS_V2"

    @property
    def eligible_for_v2_promotion(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "governance_version": self.governance_version,
            "preseal_digest": self.preseal_digest,
            "window_custody_digest": self.window_custody_digest,
            "candidate_freeze_digest": self.candidate_freeze_digest,
            "experiment_identity_digest": self.experiment_identity_digest,
            "legacy_seal_digest": self.legacy_seal_digest,
            "governed_at": self.governed_at,
            "governed_seal_digest": self.governed_seal_digest,
        }

    @classmethod
    def _build_from_verified_store(
        cls,
        *,
        preseal: OOSPresealAssessmentV2,
        identity: ExperimentRunIdentity,
        legacy_seal: OOSSeal,
        governed_at: datetime,
    ) -> GovernedOOSSealV2:
        preseal.require_ready()
        require_preseal_identity_binding_v2(preseal, identity)
        assert preseal.window is not None and preseal.candidate is not None
        if (
            legacy_seal.lineage_digest != preseal.window.pit_manifest_digest
            or legacy_seal.run_id != identity.run_id
            or legacy_seal.experiment_identity_digest != identity.digest
            or legacy_seal.evaluation_not_before != _canonical_time(preseal.window.first_read_not_before)
        ):
            raise OOSGovernanceV2Error("GOVERNED_LEGACY_SEAL_BINDING_MISMATCH")
        governed_at_canonical = _canonical_time(governed_at)
        core = {
            "schema_version": OOS_GOVERNANCE_V2_SCHEMA_VERSION,
            "governance_version": 2,
            "preseal_digest": preseal.preseal_digest,
            "window_custody_digest": preseal.window.digest,
            "candidate_freeze_digest": preseal.candidate.digest,
            "experiment_identity_digest": identity.digest,
            "legacy_seal_digest": legacy_seal.digest,
            "governed_at": governed_at_canonical,
        }
        return cls(
            preseal_digest=preseal.preseal_digest,
            window_custody_digest=preseal.window.digest,
            candidate_freeze_digest=preseal.candidate.digest,
            experiment_identity_digest=identity.digest,
            legacy_seal_digest=legacy_seal.digest,
            governed_at=governed_at_canonical,
            governed_seal_digest=_domain_digest("beidou.oos.governed-seal.v2", core),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GovernedOOSSealV2:
        fields = {
            "schema_version",
            "governance_version",
            "preseal_digest",
            "window_custody_digest",
            "candidate_freeze_digest",
            "experiment_identity_digest",
            "legacy_seal_digest",
            "governed_at",
            "governed_seal_digest",
        }
        if set(value) != fields:
            raise OOSGovernanceV2Error("GOVERNED_SEAL_FIELDS_INVALID")
        if value["schema_version"] != OOS_GOVERNANCE_V2_SCHEMA_VERSION or value["governance_version"] != 2:
            raise OOSGovernanceV2Error("GOVERNED_SEAL_VERSION_INVALID")
        for field in (
            "preseal_digest",
            "window_custody_digest",
            "candidate_freeze_digest",
            "experiment_identity_digest",
            "legacy_seal_digest",
            "governed_seal_digest",
        ):
            _require_digest(value[field], "GOVERNED_SEAL_DIGEST_INVALID")
        _parse_time(str(value["governed_at"]))
        core = {field: value[field] for field in fields if field != "governed_seal_digest"}
        if value["governed_seal_digest"] != _domain_digest("beidou.oos.governed-seal.v2", core):
            raise OOSGovernanceV2Error("GOVERNED_SEAL_DIGEST_MISMATCH")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class GovernedOOSAccessReceiptV2:
    governed_seal_digest: str
    preseal_digest: str
    legacy_seal_digest: str
    legacy_sequence: int
    legacy_audit_head: str
    governed_receipt_digest: str
    governance_version: int = 2
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION or self.governance_version != 2:
            raise OOSGovernanceV2Error("GOVERNED_RECEIPT_VERSION_INVALID")
        for value in (
            self.governed_seal_digest,
            self.preseal_digest,
            self.legacy_seal_digest,
            self.legacy_audit_head,
            self.governed_receipt_digest,
        ):
            _require_digest(value, "GOVERNED_RECEIPT_DIGEST_INVALID")
        if (
            not isinstance(self.legacy_sequence, int)
            or isinstance(self.legacy_sequence, bool)
            or self.legacy_sequence < 1
        ):
            raise OOSGovernanceV2Error("GOVERNED_RECEIPT_SEQUENCE_INVALID")
        core = {
            "schema_version": self.schema_version,
            "governance_version": self.governance_version,
            "governed_seal_digest": self.governed_seal_digest,
            "preseal_digest": self.preseal_digest,
            "legacy_seal_digest": self.legacy_seal_digest,
            "legacy_sequence": self.legacy_sequence,
            "legacy_audit_head": self.legacy_audit_head,
        }
        if self.governed_receipt_digest != _domain_digest("beidou.oos.governed-receipt.v2", core):
            raise OOSGovernanceV2Error("GOVERNED_RECEIPT_DIGEST_MISMATCH")

    @property
    def digest(self) -> str:
        return self.governed_receipt_digest

    @property
    def eligible_for_v2_promotion(self) -> bool:
        return False

    @classmethod
    def _build_from_verified_store(
        cls,
        *,
        seal: GovernedOOSSealV2,
        receipt: OOSAccessReceipt,
    ) -> GovernedOOSAccessReceiptV2:
        if receipt.seal_digest != seal.legacy_seal_digest:
            raise OOSGovernanceV2Error("GOVERNED_RECEIPT_LEGACY_SEAL_MISMATCH")
        core = {
            "schema_version": OOS_GOVERNANCE_V2_SCHEMA_VERSION,
            "governance_version": 2,
            "governed_seal_digest": seal.digest,
            "preseal_digest": seal.preseal_digest,
            "legacy_seal_digest": receipt.seal_digest,
            "legacy_sequence": receipt.sequence,
            "legacy_audit_head": receipt.audit_head,
        }
        return cls(
            governed_seal_digest=seal.digest,
            preseal_digest=seal.preseal_digest,
            legacy_seal_digest=receipt.seal_digest,
            legacy_sequence=receipt.sequence,
            legacy_audit_head=receipt.audit_head,
            governed_receipt_digest=_domain_digest("beidou.oos.governed-receipt.v2", core),
        )


@dataclass(frozen=True, slots=True)
class GovernedPromotionDecisionV2:
    status: str
    reasons: tuple[str, ...]
    eligible_for_v2_promotion: bool
    governed_seal_digest: str
    governed_receipt_digest: str
    preseal_digest: str
    experiment_identity_digest: str
    candidate_evaluation_digest: str
    governed_promotion_digest: str
    governance_version: int = 2
    schema_version: str = OOS_GOVERNANCE_V2_SCHEMA_VERSION
    _store_authority: InitVar[object | None] = None

    def __post_init__(self, _store_authority: object | None) -> None:
        if self.schema_version != OOS_GOVERNANCE_V2_SCHEMA_VERSION or self.governance_version != 2:
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_VERSION_INVALID")
        if (
            not isinstance(self.reasons, tuple)
            or any(not isinstance(reason, str) or not reason for reason in self.reasons)
            or len(set(self.reasons)) != len(self.reasons)
        ):
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_REASONS_INVALID")
        expected_eligible = self.status == "PROMOTABLE_V2" and not self.reasons
        if expected_eligible and _store_authority is not _GOVERNED_STORE_AUTHORITY:
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_STORE_AUTHORITY_REQUIRED")
        if (
            self.status not in {"PROMOTABLE_V2", "NOT_VERIFIABLE_V2"}
            or self.eligible_for_v2_promotion is not expected_eligible
        ):
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_STATE_INVALID")
        for value in (
            self.governed_seal_digest,
            self.governed_receipt_digest,
            self.preseal_digest,
            self.experiment_identity_digest,
            self.candidate_evaluation_digest,
            self.governed_promotion_digest,
        ):
            _require_digest(value, "GOVERNED_PROMOTION_DIGEST_INVALID")
        core = {
            "schema_version": self.schema_version,
            "governance_version": self.governance_version,
            "status": self.status,
            "reasons": list(self.reasons),
            "eligible_for_v2_promotion": self.eligible_for_v2_promotion,
            "governed_seal_digest": self.governed_seal_digest,
            "governed_receipt_digest": self.governed_receipt_digest,
            "preseal_digest": self.preseal_digest,
            "experiment_identity_digest": self.experiment_identity_digest,
            "candidate_evaluation_digest": self.candidate_evaluation_digest,
        }
        if self.governed_promotion_digest != _domain_digest("beidou.oos.governed-promotion.v2", core):
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_DIGEST_MISMATCH")

    @property
    def digest(self) -> str:
        return self.governed_promotion_digest

    @classmethod
    def _build_from_verified_store(
        cls,
        *,
        status: str,
        reasons: tuple[str, ...],
        eligible: bool,
        seal: GovernedOOSSealV2,
        receipt: GovernedOOSAccessReceiptV2,
        identity: ExperimentRunIdentity,
        candidate_evaluation_digest: str,
    ) -> GovernedPromotionDecisionV2:
        if (
            seal.experiment_identity_digest != identity.digest
            or receipt.governed_seal_digest != seal.digest
            or receipt.preseal_digest != seal.preseal_digest
            or receipt.legacy_seal_digest != seal.legacy_seal_digest
        ):
            raise OOSGovernanceV2Error("GOVERNED_PROMOTION_BINDING_MISMATCH")
        _require_digest(candidate_evaluation_digest, "OOS_CANDIDATE_EVALUATION_DIGEST_INVALID")
        core = {
            "schema_version": OOS_GOVERNANCE_V2_SCHEMA_VERSION,
            "governance_version": 2,
            "status": status,
            "reasons": list(reasons),
            "eligible_for_v2_promotion": eligible,
            "governed_seal_digest": seal.digest,
            "governed_receipt_digest": receipt.digest,
            "preseal_digest": seal.preseal_digest,
            "experiment_identity_digest": identity.digest,
            "candidate_evaluation_digest": candidate_evaluation_digest,
        }
        return cls(
            status=status,
            reasons=reasons,
            eligible_for_v2_promotion=eligible,
            governed_seal_digest=seal.digest,
            governed_receipt_digest=receipt.digest,
            preseal_digest=seal.preseal_digest,
            experiment_identity_digest=identity.digest,
            candidate_evaluation_digest=candidate_evaluation_digest,
            governed_promotion_digest=_domain_digest("beidou.oos.governed-promotion.v2", core),
            _store_authority=_GOVERNED_STORE_AUTHORITY,
        )


FaultInjector = Callable[[str], None]


class GovernedOOSSealStoreV2:
    """V2-only facade that forces preseal bindings around the v1 fact store."""

    governed_name = "oos-governed-seal-v2.json"

    def __init__(self, root: str | Path, *, fault_injector: FaultInjector | None = None) -> None:
        self.root = Path(root)
        self.legacy_store = OOSSealStore(self.root)
        self.governed_path = self.root / self.governed_name
        self.fault_injector = fault_injector

    def _fault(self, boundary: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(boundary)

    def create(
        self,
        *,
        preseal: OOSPresealAssessmentV2,
        identity: ExperimentRunIdentity,
        checkpoint_digest: str,
        boundary_key: bytes,
        audit_key: bytes,
        governed_at: datetime,
    ) -> GovernedOOSSealV2:
        if type(preseal) is not OOSPresealAssessmentV2:
            raise OOSGovernanceV2Error("V2_TYPE_REQUIRED")
        preseal.require_ready()
        require_preseal_identity_binding_v2(preseal, identity)
        _require_digest(checkpoint_digest, "OOS_CHECKPOINT_DIGEST_INVALID")
        assert preseal.window is not None and preseal.candidate is not None
        governed_time = governed_at.astimezone(timezone.utc)
        if not preseal.candidate.candidate_frozen_at.astimezone(timezone.utc) <= governed_time:
            raise OOSGovernanceV2Error("GOVERNED_SEAL_BEFORE_CANDIDATE_FREEZE")
        if governed_time >= preseal.window.first_read_not_before.astimezone(timezone.utc):
            raise OOSGovernanceV2Error("GOVERNED_SEAL_NOT_BEFORE_FIRST_READ")
        with self.legacy_store._lock(exclusive=True):
            if self.legacy_store.seal_path.exists() and not self.governed_path.exists():
                raise OOSGovernanceV2Error("ORPHAN_LEGACY_SEAL_NOT_V2_UPGRADABLE")
            if self.legacy_store.seal_path.exists() or self.governed_path.exists():
                raise OOSGovernanceV2Error("GOVERNED_STORE_NOT_EMPTY")
            self._fault("before_legacy_seal_write")
            legacy = self.legacy_store._create_unlocked(
                boundary=preseal.window.boundary,
                key=boundary_key,
                audit_key=audit_key,
                lineage_digest=preseal.window.pit_manifest_digest,
                run_id=identity.run_id,
                experiment_identity_digest=identity.digest,
                checkpoint_digest=checkpoint_digest,
                sealed_at=_canonical_time(governed_at),
                evaluation_not_before=_canonical_time(preseal.window.first_read_not_before),
            )
            self._fault("after_legacy_seal_durable")
            governed = GovernedOOSSealV2._build_from_verified_store(
                preseal=preseal,
                identity=identity,
                legacy_seal=legacy,
                governed_at=governed_at,
            )
            payload = (canonical_json(governed.as_dict()) + "\n").encode("utf-8")
            try:
                with self.governed_path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:
                raise OOSGovernanceV2Error("GOVERNED_STORE_NOT_EMPTY") from exc
            self.legacy_store._fsync_directory(self.root)
            self._fault("after_governed_seal_durable")
            loaded = self._load_governed_unlocked()
            if loaded != governed:
                raise OOSGovernanceV2Error("GOVERNED_SEAL_RELOAD_MISMATCH")
            return governed

    def load_governed_seal(self) -> GovernedOOSSealV2:
        with self.legacy_store._lock(exclusive=False):
            governed, _legacy = self._load_complete_governed_unlocked()
            return governed

    def _load_governed_unlocked(self) -> GovernedOOSSealV2:
        if not self.governed_path.is_file():
            if self.legacy_store.seal_path.is_file():
                raise OOSGovernanceV2Error("ORPHAN_LEGACY_SEAL_NOT_V2_UPGRADABLE")
            raise OOSGovernanceV2Error("MISSING_GOVERNED_SEAL_V2")
        try:
            raw = self.governed_path.read_bytes()
            value = json.loads(raw)
            governed = GovernedOOSSealV2.from_dict(value)
        except OOSGovernanceV2Error:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OOSGovernanceV2Error("INVALID_GOVERNED_SEAL_V2") from exc
        if raw != (canonical_json(governed.as_dict()) + "\n").encode("utf-8"):
            raise OOSGovernanceV2Error("NON_CANONICAL_GOVERNED_SEAL_V2")
        return governed

    def _load_complete_governed_unlocked(self) -> tuple[GovernedOOSSealV2, OOSSeal]:
        governed = self._load_governed_unlocked()
        try:
            legacy = self.legacy_store._load_seal_unlocked()
        except OOSSealError as exc:
            raise OOSGovernanceV2Error("GOVERNED_LEGACY_SEAL_NOT_VERIFIABLE") from exc
        if governed.legacy_seal_digest != legacy.digest:
            raise OOSGovernanceV2Error("GOVERNED_LEGACY_SEAL_NOT_VERIFIABLE")
        return governed, legacy

    def _verify_governed_unlocked(
        self,
        *,
        seal: GovernedOOSSealV2,
        preseal: OOSPresealAssessmentV2,
        identity: ExperimentRunIdentity,
    ) -> OOSSeal:
        if type(seal) is not GovernedOOSSealV2 or type(preseal) is not OOSPresealAssessmentV2:
            raise OOSGovernanceV2Error("V2_TYPE_REQUIRED")
        preseal.require_ready()
        require_preseal_identity_binding_v2(preseal, identity)
        persisted, legacy = self._load_complete_governed_unlocked()
        if persisted != seal:
            raise OOSGovernanceV2Error("GOVERNED_SEAL_MISMATCH")
        if (
            seal.preseal_digest != preseal.preseal_digest
            or seal.experiment_identity_digest != identity.digest
            or seal.legacy_seal_digest != legacy.digest
        ):
            raise OOSGovernanceV2Error("GOVERNED_BINDING_MISMATCH")
        assert preseal.window is not None and preseal.candidate is not None
        if (
            seal.window_custody_digest != preseal.window.digest
            or seal.candidate_freeze_digest != preseal.candidate.digest
            or legacy.lineage_digest != preseal.window.pit_manifest_digest
            or legacy.run_id != identity.run_id
            or legacy.experiment_identity_digest != identity.digest
            or legacy.evaluation_not_before != _canonical_time(preseal.window.first_read_not_before)
        ):
            raise OOSGovernanceV2Error("GOVERNED_DOWNSTREAM_BINDING_MISMATCH")
        return legacy

    def access(
        self,
        *,
        seal: GovernedOOSSealV2,
        preseal: OOSPresealAssessmentV2,
        identity: ExperimentRunIdentity,
        checkpoint_digest: str,
        boundary_key: bytes,
        audit_key: bytes,
        accessed_at: datetime,
        candidate_evaluation_complete: bool,
        candidate_evaluation_digest: str,
        purpose: str,
    ) -> GovernedOOSAccessReceiptV2:
        _require_digest(checkpoint_digest, "OOS_CHECKPOINT_DIGEST_INVALID")
        _require_digest(candidate_evaluation_digest, "OOS_CANDIDATE_EVALUATION_DIGEST_INVALID")
        if type(seal) is not GovernedOOSSealV2:
            raise OOSGovernanceV2Error("V2_TYPE_REQUIRED")
        with self.legacy_store._lock(exclusive=True):
            legacy = self._verify_governed_unlocked(seal=seal, preseal=preseal, identity=identity)
            if legacy.checkpoint_digest != checkpoint_digest:
                raise OOSGovernanceV2Error("GOVERNED_CHECKPOINT_BINDING_MISMATCH")
            assert preseal.window is not None
            receipt = self.legacy_store._access_unlocked(
                seal=legacy,
                boundary=preseal.window.boundary,
                key=boundary_key,
                audit_key=audit_key,
                run_id=identity.run_id,
                experiment_identity_digest=identity.digest,
                checkpoint_digest=checkpoint_digest,
                accessed_at=_canonical_time(accessed_at),
                candidate_evaluation_complete=candidate_evaluation_complete,
                candidate_evaluation_digest=candidate_evaluation_digest,
                purpose=purpose,
            )
            self._fault("after_legacy_access_durable")
            return GovernedOOSAccessReceiptV2._build_from_verified_store(seal=seal, receipt=receipt)

    def promotion_status(
        self,
        *,
        seal: GovernedOOSSealV2,
        receipt: GovernedOOSAccessReceiptV2,
        preseal: OOSPresealAssessmentV2,
        identity: ExperimentRunIdentity,
        boundary_key: bytes,
        audit_key: bytes,
        candidate_evaluation_digest: str,
    ) -> GovernedPromotionDecisionV2:
        if type(seal) is not GovernedOOSSealV2 or type(receipt) is not GovernedOOSAccessReceiptV2:
            raise OOSGovernanceV2Error("V2_TYPE_REQUIRED")
        _require_digest(candidate_evaluation_digest, "OOS_CANDIDATE_EVALUATION_DIGEST_INVALID")
        reasons: list[str] = []
        with self.legacy_store._lock(exclusive=False):
            legacy = self._verify_governed_unlocked(seal=seal, preseal=preseal, identity=identity)
            if (
                receipt.governed_seal_digest != seal.digest
                or receipt.preseal_digest != preseal.preseal_digest
                or receipt.legacy_seal_digest != legacy.digest
            ):
                reasons.append("GOVERNED_RECEIPT_BINDING_MISMATCH")
            legacy_receipt = OOSAccessReceipt(
                seal_digest=receipt.legacy_seal_digest,
                sequence=receipt.legacy_sequence,
                audit_head=receipt.legacy_audit_head,
            )
            assert preseal.window is not None
            legacy_status = self.legacy_store._promotion_status_unlocked(
                seal=legacy,
                receipt=legacy_receipt,
                boundary=preseal.window.boundary,
                key=boundary_key,
                audit_key=audit_key,
                candidate_evaluation_digest=candidate_evaluation_digest,
            )
            if legacy_status.status != "PROMOTABLE":
                reasons.extend(f"LEGACY:{reason}" for reason in legacy_status.reasons)
        unique = tuple(dict.fromkeys(reasons))
        eligible = not unique
        return GovernedPromotionDecisionV2._build_from_verified_store(
            status="PROMOTABLE_V2" if eligible else "NOT_VERIFIABLE_V2",
            reasons=unique,
            eligible=eligible,
            seal=seal,
            receipt=receipt,
            identity=identity,
            candidate_evaluation_digest=candidate_evaluation_digest,
        )


__all__ = [
    "OOS_GOVERNANCE_V2_SCHEMA_VERSION",
    "GovernedOOSAccessReceiptV2",
    "GovernedOOSSealStoreV2",
    "GovernedOOSSealV2",
    "GovernedPromotionDecisionV2",
    "OOSCandidateFreezeV2",
    "OOSGovernanceV2Error",
    "OOSPresealAssessmentV2",
    "OOSWindowCustodySpecV2",
    "assess_oos_preseal_v2",
    "require_preseal_identity_binding_v2",
]
