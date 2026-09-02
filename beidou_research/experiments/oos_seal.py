"""Opaque OOS boundary commitments with append-only access auditing."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .contracts import GENESIS_HASH, canonical_json

OOS_SEAL_SCHEMA_VERSION = "1.0"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class OOSSealError(RuntimeError):
    """Base error for sealed OOS operations."""


class OOSSealNotVerifiableError(OOSSealError):
    """The persisted seal is missing, mutable, or malformed."""


class OOSAuditNotVerifiableError(OOSSealError):
    """The append-only access chain cannot be independently replayed."""


class OOSAccessInvalidatedError(OOSSealError):
    """An access was audited but invalidated promotion evidence."""


OOSSealNotVerifiable = OOSSealNotVerifiableError
OOSAuditNotVerifiable = OOSAuditNotVerifiableError
OOSAccessInvalidated = OOSAccessInvalidatedError


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must use canonical UTC Z form")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError("timestamp must be UTC")
    return result


@dataclass(frozen=True)
class OOSBoundary:
    train_end: str
    oos_start: str
    oos_end: str
    timezone: str

    def __post_init__(self) -> None:
        if self.timezone != "UTC":
            raise ValueError("OOS_BOUNDARY_MUST_USE_UTC")
        train_end = _utc(self.train_end)
        oos_start = _utc(self.oos_start)
        oos_end = _utc(self.oos_end)
        if not train_end < oos_start <= oos_end:
            raise ValueError("INVALID_OOS_BOUNDARY_ORDER")

    def as_dict(self) -> dict[str, str]:
        return {
            "train_end": self.train_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "timezone": self.timezone,
        }


@dataclass(frozen=True)
class OOSSeal:
    seal_id: str
    boundary_commitment: str
    boundary_digest: str
    key_digest: str
    audit_anchor_digest: str
    lineage_digest: str
    run_id: str
    experiment_identity_digest: str
    checkpoint_digest: str
    sealed_at: str
    evaluation_not_before: str
    seal_digest: str
    schema_version: str = OOS_SEAL_SCHEMA_VERSION

    @property
    def digest(self) -> str:
        return self.seal_digest

    @property
    def governance_version(self) -> int:
        return 1

    @property
    def promotion_scope(self) -> str:
        return "LEGACY_RESEARCH_ONLY"

    @property
    def eligible_for_v2_promotion(self) -> bool:
        return False

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "seal_id": self.seal_id,
            "boundary_commitment": self.boundary_commitment,
            "boundary_digest": self.boundary_digest,
            "key_digest": self.key_digest,
            "audit_anchor_digest": self.audit_anchor_digest,
            "lineage_digest": self.lineage_digest,
            "run_id": self.run_id,
            "experiment_identity_digest": self.experiment_identity_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "sealed_at": self.sealed_at,
            "evaluation_not_before": self.evaluation_not_before,
            "seal_digest": self.seal_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OOSSeal":
        fields = {
            "schema_version",
            "seal_id",
            "boundary_commitment",
            "boundary_digest",
            "key_digest",
            "audit_anchor_digest",
            "lineage_digest",
            "run_id",
            "experiment_identity_digest",
            "checkpoint_digest",
            "sealed_at",
            "evaluation_not_before",
            "seal_digest",
        }
        missing = fields - set(value)
        if missing:
            raise OOSSealNotVerifiable(f"MISSING_SEAL_FIELDS:{','.join(sorted(missing))}")
        if value["schema_version"] != OOS_SEAL_SCHEMA_VERSION:
            raise OOSSealNotVerifiable("UNKNOWN_OOS_SEAL_VERSION")
        for field in (
            "seal_id",
            "boundary_commitment",
            "boundary_digest",
            "key_digest",
            "audit_anchor_digest",
            "lineage_digest",
            "experiment_identity_digest",
            "checkpoint_digest",
            "seal_digest",
        ):
            if _HEX64.fullmatch(str(value[field])) is None:
                raise OOSSealNotVerifiable(f"INVALID_SEAL_DIGEST:{field}")
        if not str(value["run_id"]).strip():
            raise OOSSealNotVerifiable("EMPTY_SEAL_RUN_ID")
        try:
            if _utc(str(value["sealed_at"])) >= _utc(str(value["evaluation_not_before"])):
                raise OOSSealNotVerifiable("SEAL_NOT_CREATED_BEFORE_EVALUATION")
        except ValueError as exc:
            raise OOSSealNotVerifiable("INVALID_SEAL_TIMESTAMP") from exc
        core = {field: value[field] for field in fields if field != "seal_digest"}
        if value["seal_digest"] != _digest(core):
            raise OOSSealNotVerifiable("OOS_SEAL_DIGEST_MISMATCH")
        return cls(
            seal_id=str(value["seal_id"]),
            boundary_commitment=str(value["boundary_commitment"]),
            boundary_digest=str(value["boundary_digest"]),
            key_digest=str(value["key_digest"]),
            audit_anchor_digest=str(value["audit_anchor_digest"]),
            lineage_digest=str(value["lineage_digest"]),
            run_id=str(value["run_id"]),
            experiment_identity_digest=str(value["experiment_identity_digest"]),
            checkpoint_digest=str(value["checkpoint_digest"]),
            sealed_at=str(value["sealed_at"]),
            evaluation_not_before=str(value["evaluation_not_before"]),
            seal_digest=str(value["seal_digest"]),
            schema_version=str(value["schema_version"]),
        )


@dataclass(frozen=True)
class OOSAccessEvent:
    sequence: int
    previous_hash: str
    event_hash: str
    audit_auth_tag: str
    seal_digest: str
    accessed_at: str
    run_id: str
    experiment_identity_digest: str
    checkpoint_digest: str
    boundary_digest: str
    purpose: str
    candidate_evaluation_complete: bool
    candidate_evaluation_digest: str
    decision: str
    reasons: tuple[str, ...]
    schema_version: str = OOS_SEAL_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            "audit_auth_tag": self.audit_auth_tag,
            "seal_digest": self.seal_digest,
            "accessed_at": self.accessed_at,
            "run_id": self.run_id,
            "experiment_identity_digest": self.experiment_identity_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "boundary_digest": self.boundary_digest,
            "purpose": self.purpose,
            "candidate_evaluation_complete": self.candidate_evaluation_complete,
            "candidate_evaluation_digest": self.candidate_evaluation_digest,
            "decision": self.decision,
            "reasons": list(self.reasons),
        }

    @classmethod
    def build(cls, *, audit_key: bytes, **facts: Any) -> "OOSAccessEvent":
        authenticated = {"schema_version": OOS_SEAL_SCHEMA_VERSION, **facts}
        auth_tag = hmac.new(audit_key, canonical_json(authenticated).encode("utf-8"), hashlib.sha256).hexdigest()
        core = {**authenticated, "audit_auth_tag": auth_tag}
        return cls(
            event_hash=_digest(core),
            audit_auth_tag=auth_tag,
            schema_version=OOS_SEAL_SCHEMA_VERSION,
            **facts,
        )

    def authenticated_by(self, audit_key: bytes) -> bool:
        authenticated = self.as_dict()
        authenticated.pop("event_hash")
        authenticated.pop("audit_auth_tag")
        expected = hmac.new(
            audit_key,
            canonical_json(authenticated).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, self.audit_auth_tag)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OOSAccessEvent":
        fields = {
            "schema_version",
            "sequence",
            "previous_hash",
            "event_hash",
            "audit_auth_tag",
            "seal_digest",
            "accessed_at",
            "run_id",
            "experiment_identity_digest",
            "checkpoint_digest",
            "boundary_digest",
            "purpose",
            "candidate_evaluation_complete",
            "candidate_evaluation_digest",
            "decision",
            "reasons",
        }
        if fields - set(value):
            raise OOSAuditNotVerifiable("MISSING_AUDIT_FIELDS")
        if value["schema_version"] != OOS_SEAL_SCHEMA_VERSION:
            raise OOSAuditNotVerifiable("UNKNOWN_AUDIT_VERSION")
        core = {field: value[field] for field in fields if field != "event_hash"}
        if value["event_hash"] != _digest(core):
            raise OOSAuditNotVerifiable("AUDIT_EVENT_DIGEST_MISMATCH")
        return cls(
            sequence=int(value["sequence"]),
            previous_hash=str(value["previous_hash"]),
            event_hash=str(value["event_hash"]),
            audit_auth_tag=str(value["audit_auth_tag"]),
            seal_digest=str(value["seal_digest"]),
            accessed_at=str(value["accessed_at"]),
            run_id=str(value["run_id"]),
            experiment_identity_digest=str(value["experiment_identity_digest"]),
            checkpoint_digest=str(value["checkpoint_digest"]),
            boundary_digest=str(value["boundary_digest"]),
            purpose=str(value["purpose"]),
            candidate_evaluation_complete=value["candidate_evaluation_complete"] is True,
            candidate_evaluation_digest=str(value["candidate_evaluation_digest"]),
            decision=str(value["decision"]),
            reasons=tuple(str(reason) for reason in value["reasons"]),
            schema_version=str(value["schema_version"]),
        )


@dataclass(frozen=True)
class OOSAccessReceipt:
    seal_digest: str
    sequence: int
    audit_head: str

    @property
    def governance_version(self) -> int:
        return 1

    @property
    def promotion_scope(self) -> str:
        return "LEGACY_RESEARCH_ONLY"

    @property
    def eligible_for_v2_promotion(self) -> bool:
        return False


@dataclass(frozen=True)
class OOSPromotionStatus:
    status: str
    reasons: tuple[str, ...]
    synthetic_economic_evidence: bool = False

    @property
    def governance_version(self) -> int:
        return 1

    @property
    def promotion_scope(self) -> str:
        return "LEGACY_RESEARCH_ONLY"

    @property
    def eligible_for_v2_promotion(self) -> bool:
        return False


class OOSSealStore:
    """Filesystem protocol: immutable seal plus hash-chained access attempts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.seal_path = self.root / "oos-seal.json"
        self.audit_path = self.root / "oos-access-audit.jsonl"
        self.lock_path = self.root / ".oos-access.lock"

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def create(
        self,
        *,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        lineage_digest: str,
        run_id: str,
        experiment_identity_digest: str,
        checkpoint_digest: str,
        sealed_at: str,
        evaluation_not_before: str,
    ) -> OOSSeal:
        with self._lock(exclusive=True):
            return self._create_unlocked(
                boundary=boundary,
                key=key,
                audit_key=audit_key,
                lineage_digest=lineage_digest,
                run_id=run_id,
                experiment_identity_digest=experiment_identity_digest,
                checkpoint_digest=checkpoint_digest,
                sealed_at=sealed_at,
                evaluation_not_before=evaluation_not_before,
            )

    def _create_unlocked(
        self,
        *,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        lineage_digest: str,
        run_id: str,
        experiment_identity_digest: str,
        checkpoint_digest: str,
        sealed_at: str,
        evaluation_not_before: str,
    ) -> OOSSeal:
        if self.seal_path.exists() or self.audit_path.exists():
            raise OOSSealNotVerifiable("IMMUTABLE_OOS_SEAL_ALREADY_EXISTS")
        if len(key) < 32:
            raise ValueError("OOS_SEAL_KEY_TOO_SHORT")
        if len(audit_key) < 32:
            raise ValueError("OOS_AUDIT_KEY_TOO_SHORT")
        if _utc(sealed_at) >= _utc(evaluation_not_before):
            raise ValueError("SEAL_MUST_PRECEDE_CANDIDATE_EVALUATION")
        boundary_bytes = canonical_json(boundary.as_dict()).encode("utf-8")
        boundary_digest = hashlib.sha256(boundary_bytes).hexdigest()
        commitment = hmac.new(key, boundary_bytes, hashlib.sha256).hexdigest()
        key_digest = hashlib.sha256(key).hexdigest()
        audit_anchor_digest = hashlib.sha256(audit_key).hexdigest()
        seal_id = hashlib.sha256(
            b"opaque-oos-seal-v1\0" + key + audit_anchor_digest.encode() + commitment.encode()
        ).hexdigest()
        core = {
            "schema_version": OOS_SEAL_SCHEMA_VERSION,
            "seal_id": seal_id,
            "boundary_commitment": commitment,
            "boundary_digest": boundary_digest,
            "key_digest": key_digest,
            "audit_anchor_digest": audit_anchor_digest,
            "lineage_digest": lineage_digest,
            "run_id": run_id,
            "experiment_identity_digest": experiment_identity_digest,
            "checkpoint_digest": checkpoint_digest,
            "sealed_at": sealed_at,
            "evaluation_not_before": evaluation_not_before,
        }
        seal = OOSSeal.from_dict({**core, "seal_digest": _digest(core)})
        payload = (canonical_json(seal.as_dict()) + "\n").encode("utf-8")
        try:
            with self.seal_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise OOSSealNotVerifiable("IMMUTABLE_OOS_SEAL_ALREADY_EXISTS") from exc
        self._fsync_directory(self.root)
        return seal

    def load_seal(self) -> OOSSeal:
        with self._lock(exclusive=False):
            return self._load_seal_unlocked()

    def _load_seal_unlocked(self) -> OOSSeal:
        if not self.seal_path.is_file():
            raise OOSSealNotVerifiable("MISSING_OOS_SEAL")
        try:
            raw = self.seal_path.read_bytes()
            value = json.loads(raw)
            seal = OOSSeal.from_dict(value)
        except OOSSealError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OOSSealNotVerifiable("INVALID_OOS_SEAL") from exc
        if raw != (canonical_json(seal.as_dict()) + "\n").encode("utf-8"):
            raise OOSSealNotVerifiable("NON_CANONICAL_OOS_SEAL_BYTES")
        return seal

    def audit_events(self) -> tuple[OOSAccessEvent, ...]:
        with self._lock(exclusive=False):
            return self._audit_events_unlocked()

    def _audit_events_unlocked(self) -> tuple[OOSAccessEvent, ...]:
        if not self.audit_path.exists():
            return ()
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise OOSAuditNotVerifiable("UNREADABLE_OOS_AUDIT") from exc
        events: list[OOSAccessEvent] = []
        previous_hash = GENESIS_HASH
        for expected_sequence, line in enumerate(lines, start=1):
            if not line:
                raise OOSAuditNotVerifiable("BLANK_OOS_AUDIT_EVENT")
            try:
                raw = json.loads(line)
                event = OOSAccessEvent.from_dict(raw)
            except OOSSealError:
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OOSAuditNotVerifiable("INVALID_OOS_AUDIT_EVENT") from exc
            if canonical_json(event.as_dict()) != line:
                raise OOSAuditNotVerifiable("NON_CANONICAL_OOS_AUDIT_EVENT")
            if event.sequence != expected_sequence or event.previous_hash != previous_hash:
                raise OOSAuditNotVerifiable("OOS_AUDIT_CHAIN_MISMATCH")
            previous_hash = event.event_hash
            events.append(event)
        return tuple(events)

    def _append(self, event: OOSAccessEvent) -> None:
        created = not self.audit_path.exists()
        payload = (canonical_json(event.as_dict()) + "\n").encode("utf-8")
        with self.audit_path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            self._fsync_directory(self.root)

    def access(
        self,
        *,
        seal: OOSSeal,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        run_id: str,
        experiment_identity_digest: str,
        checkpoint_digest: str,
        accessed_at: str,
        candidate_evaluation_complete: bool,
        candidate_evaluation_digest: str,
        purpose: str,
    ) -> OOSAccessReceipt:
        with self._lock(exclusive=True):
            return self._access_unlocked(
                seal=seal,
                boundary=boundary,
                key=key,
                audit_key=audit_key,
                run_id=run_id,
                experiment_identity_digest=experiment_identity_digest,
                checkpoint_digest=checkpoint_digest,
                accessed_at=accessed_at,
                candidate_evaluation_complete=candidate_evaluation_complete,
                candidate_evaluation_digest=candidate_evaluation_digest,
                purpose=purpose,
            )

    def _access_unlocked(
        self,
        *,
        seal: OOSSeal,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        run_id: str,
        experiment_identity_digest: str,
        checkpoint_digest: str,
        accessed_at: str,
        candidate_evaluation_complete: bool,
        candidate_evaluation_digest: str,
        purpose: str,
    ) -> OOSAccessReceipt:
        persisted = self._load_seal_unlocked()
        if persisted != seal:
            raise OOSSealNotVerifiable("SUPPLIED_SEAL_DOES_NOT_MATCH_IMMUTABLE_SEAL")
        events = self._audit_events_unlocked()
        reasons: list[str] = []
        try:
            if _utc(accessed_at) < _utc(seal.evaluation_not_before) or not candidate_evaluation_complete:
                reasons.append("EARLY_OOS_ACCESS")
        except ValueError:
            reasons.append("INVALID_OOS_ACCESS_TIMESTAMP")
        if events:
            reasons.append("REPEATED_OOS_ACCESS")
        if hashlib.sha256(audit_key).hexdigest() != seal.audit_anchor_digest:
            reasons.append("UNBOUND_OOS_AUDIT_ANCHOR")

        boundary_bytes = canonical_json(boundary.as_dict()).encode("utf-8")
        boundary_digest = hashlib.sha256(boundary_bytes).hexdigest()
        commitment = hmac.new(key, boundary_bytes, hashlib.sha256).hexdigest()
        if (
            hashlib.sha256(key).hexdigest() != seal.key_digest
            or not hmac.compare_digest(commitment, seal.boundary_commitment)
            or boundary_digest != seal.boundary_digest
        ):
            reasons.append("MUTATED_OOS_BOUNDARY")
        if (
            run_id != seal.run_id
            or experiment_identity_digest != seal.experiment_identity_digest
            or checkpoint_digest != seal.checkpoint_digest
        ):
            reasons.append("UNBOUND_OOS_ACCESS")
        if not purpose.strip():
            reasons.append("MISSING_OOS_ACCESS_PURPOSE")
        if _HEX64.fullmatch(candidate_evaluation_digest) is None:
            reasons.append("INVALID_CANDIDATE_EVALUATION_DIGEST")

        event = OOSAccessEvent.build(
            audit_key=audit_key,
            sequence=len(events) + 1,
            previous_hash=events[-1].event_hash if events else GENESIS_HASH,
            seal_digest=seal.digest,
            accessed_at=accessed_at,
            run_id=run_id,
            experiment_identity_digest=experiment_identity_digest,
            checkpoint_digest=checkpoint_digest,
            boundary_digest=boundary_digest,
            purpose=purpose,
            candidate_evaluation_complete=candidate_evaluation_complete,
            candidate_evaluation_digest=candidate_evaluation_digest,
            decision="DENIED" if reasons else "ALLOWED_ONCE",
            reasons=tuple(reasons),
        )
        self._append(event)
        if reasons:
            raise OOSAccessInvalidated(";".join(reasons))
        return OOSAccessReceipt(seal_digest=seal.digest, sequence=event.sequence, audit_head=event.event_hash)

    def promotion_status(
        self,
        *,
        seal: OOSSeal,
        receipt: OOSAccessReceipt,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        candidate_evaluation_digest: str,
    ) -> OOSPromotionStatus:
        with self._lock(exclusive=False):
            return self._promotion_status_unlocked(
                seal=seal,
                receipt=receipt,
                boundary=boundary,
                key=key,
                audit_key=audit_key,
                candidate_evaluation_digest=candidate_evaluation_digest,
            )

    def _promotion_status_unlocked(
        self,
        *,
        seal: OOSSeal,
        receipt: OOSAccessReceipt,
        boundary: OOSBoundary,
        key: bytes,
        audit_key: bytes,
        candidate_evaluation_digest: str,
    ) -> OOSPromotionStatus:
        persisted = self._load_seal_unlocked()
        events = self._audit_events_unlocked()
        reasons: list[str] = []
        if persisted != seal or receipt.seal_digest != seal.digest:
            reasons.append("OOS_SEAL_RECEIPT_MISMATCH")
        if len(events) != 1:
            reasons.append("OOS_ACCESS_COUNT_NOT_EXACTLY_ONE")
        if not events or events[-1].event_hash != receipt.audit_head or events[-1].sequence != receipt.sequence:
            reasons.append("OOS_AUDIT_HEAD_MISMATCH")
        if any(event.decision != "ALLOWED_ONCE" or event.reasons for event in events):
            reasons.append("OOS_ACCESS_INVALIDATION_PRESENT")
        boundary_bytes = canonical_json(boundary.as_dict()).encode("utf-8")
        expected_boundary_digest = hashlib.sha256(boundary_bytes).hexdigest()
        expected_commitment = hmac.new(key, boundary_bytes, hashlib.sha256).hexdigest()
        if (
            hashlib.sha256(key).hexdigest() != seal.key_digest
            or expected_boundary_digest != seal.boundary_digest
            or not hmac.compare_digest(expected_commitment, seal.boundary_commitment)
        ):
            reasons.append("MUTATED_OOS_BOUNDARY")
        if hashlib.sha256(audit_key).hexdigest() != seal.audit_anchor_digest:
            reasons.append("OOS_AUDIT_ANCHOR_MISMATCH")
        for event in events:
            if not event.authenticated_by(audit_key):
                reasons.append("OOS_AUDIT_AUTHENTICATION_MISMATCH")
            try:
                if _utc(event.accessed_at) < _utc(seal.evaluation_not_before):
                    reasons.append("EARLY_OOS_ACCESS")
            except ValueError:
                reasons.append("INVALID_OOS_ACCESS_TIMESTAMP")
            if event.candidate_evaluation_complete is not True:
                reasons.append("CANDIDATE_EVALUATION_NOT_COMPLETE")
            if event.candidate_evaluation_digest != candidate_evaluation_digest:
                reasons.append("CANDIDATE_EVALUATION_DIGEST_MISMATCH")
            if event.boundary_digest != expected_boundary_digest:
                reasons.append("MUTATED_OOS_BOUNDARY")
        if any(
            event.seal_digest != seal.digest
            or event.run_id != seal.run_id
            or event.experiment_identity_digest != seal.experiment_identity_digest
            or event.checkpoint_digest != seal.checkpoint_digest
            for event in events
        ):
            reasons.append("OOS_AUDIT_BINDING_MISMATCH")
        unique = tuple(dict.fromkeys(reasons))
        return OOSPromotionStatus(status="INVALIDATED" if unique else "PROMOTABLE", reasons=unique)

    def rollback_to_research_only(self, *, seal: OOSSeal, receipt: OOSAccessReceipt) -> dict[str, str | bool]:
        """Disable promotion without rewriting or discarding lineage/access evidence."""

        with self._lock(exclusive=False):
            persisted = self._load_seal_unlocked()
            events = self._audit_events_unlocked()
            if persisted != seal or not events or events[-1].event_hash != receipt.audit_head:
                raise OOSAuditNotVerifiable("ROLLBACK_EVIDENCE_BINDING_MISMATCH")
            return {
                "status": "NON_PROMOTABLE_RESEARCH_ONLY",
                "promotion_enabled": False,
                "preserved_seal_digest": seal.digest,
                "preserved_audit_head": events[-1].event_hash,
            }
