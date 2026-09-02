"""Fail-closed, content-addressed point-in-time lineage.

The manifest is deliberately explicit.  A promotable run must bind every
required input to an immutable snapshot and an ``available_as_of`` cutoff;
there is no "latest" or default provenance path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from beidou_research.experiments.contracts import canonical_json

PIT_LINEAGE_SCHEMA_VERSION = "1.0"
REQUIRED_LINEAGE_ROLES = frozenset(
    {
        "dataset",
        "universe_membership",
        "corporate_actions",
        "revisions",
        "calendar",
        "timestamps",
        "timezone",
        "features",
        "labels",
        "costs",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "role",
        "source_id",
        "source_path",
        "content_sha256",
        "available_as_of",
        "event_time_start",
        "event_time_end",
        "timezone",
        "revision_id",
        "point_in_time",
        "artifact_digest",
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_AMBIGUOUS_PROVENANCE = re.compile(r"(?:^|[-_/])(latest|default|current)(?:$|[-_/])", re.IGNORECASE)
_PAIRWISE_FEATURE_KIND = "BEIDOU_PAIRWISE_FEATURE_LINEAGE"
_PAIRWISE_LABEL_KIND = "BEIDOU_PAIRWISE_LABEL_LINEAGE"


class LineageNotVerifiableError(RuntimeError):
    """Raised when lineage cannot authorize promotable evidence."""


LineageNotVerifiable = LineageNotVerifiableError


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be canonical UTC with Z suffix")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must be UTC")
    return parsed


def _pairwise_lineage_reasons(
    *, root: Path, feature_artifact: LineageArtifact, label_artifact: LineageArtifact
) -> tuple[bool, tuple[str, ...]]:
    """Validate record-level feature/label availability when both sources opt in.

    The original coarse artifact windows remain the fallback.  Pairwise files
    are accepted only when every record is content-bound, uniquely paired, and
    proves feature availability no later than decision time and label
    availability strictly after it.
    """

    try:
        feature_payload = json.loads((root / feature_artifact.source_path).read_text(encoding="utf-8"))
        label_payload = json.loads((root / label_artifact.source_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ()
    if not isinstance(feature_payload, Mapping) or not isinstance(label_payload, Mapping):
        return False, ()
    declared = (
        feature_payload.get("kind") == _PAIRWISE_FEATURE_KIND or label_payload.get("kind") == _PAIRWISE_LABEL_KIND
    )
    if not declared:
        return False, ()
    reasons: list[str] = []
    expected_top = {"schema_version", "kind", "lineage_id", "records", "records_digest"}
    if set(feature_payload) != expected_top or feature_payload.get("kind") != _PAIRWISE_FEATURE_KIND:
        reasons.append("PAIRWISE_FEATURE_FILE_INVALID")
    if set(label_payload) != expected_top or label_payload.get("kind") != _PAIRWISE_LABEL_KIND:
        reasons.append("PAIRWISE_LABEL_FILE_INVALID")
    if feature_payload.get("schema_version") != "1.0" or label_payload.get("schema_version") != "1.0":
        reasons.append("PAIRWISE_SCHEMA_VERSION_INVALID")
    if not isinstance(feature_payload.get("lineage_id"), str) or feature_payload.get("lineage_id") != label_payload.get(
        "lineage_id"
    ):
        reasons.append("PAIRWISE_LINEAGE_ID_MISMATCH")
    feature_records = feature_payload.get("records")
    label_records = label_payload.get("records")
    if not isinstance(feature_records, list) or not isinstance(label_records, list):
        return True, tuple(dict.fromkeys([*reasons, "PAIRWISE_RECORDS_INVALID"]))
    if feature_payload.get("records_digest") != _digest(feature_records):
        reasons.append("PAIRWISE_FEATURE_RECORDS_DIGEST_MISMATCH")
    if label_payload.get("records_digest") != _digest(label_records):
        reasons.append("PAIRWISE_LABEL_RECORDS_DIGEST_MISMATCH")
    feature_fields = {
        "record_id",
        "symbol",
        "decision_time",
        "available_as_of",
        "history_digest",
        "feature_digest",
        "policy_digest",
    }
    label_fields = {
        "record_id",
        "symbol",
        "decision_time",
        "available_as_of",
        "label_end",
        "label_digest",
    }
    features_by_id: dict[str, Mapping[str, Any]] = {}
    labels_by_id: dict[str, Mapping[str, Any]] = {}
    for record in feature_records:
        if not isinstance(record, Mapping) or set(record) != feature_fields:
            reasons.append("PAIRWISE_FEATURE_RECORD_INVALID")
            continue
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or _HEX64.fullmatch(record_id) is None or record_id in features_by_id:
            reasons.append("PAIRWISE_FEATURE_RECORD_ID_INVALID")
            continue
        features_by_id[record_id] = record
    for record in label_records:
        if not isinstance(record, Mapping) or set(record) != label_fields:
            reasons.append("PAIRWISE_LABEL_RECORD_INVALID")
            continue
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or _HEX64.fullmatch(record_id) is None or record_id in labels_by_id:
            reasons.append("PAIRWISE_LABEL_RECORD_ID_INVALID")
            continue
        labels_by_id[record_id] = record
    if not features_by_id or set(features_by_id) != set(labels_by_id):
        reasons.append("PAIRWISE_RECORD_SET_MISMATCH")
    for record_id in sorted(set(features_by_id) & set(labels_by_id)):
        feature = features_by_id[record_id]
        label = labels_by_id[record_id]
        try:
            feature_available = _utc_timestamp(feature["available_as_of"])
            feature_decision = _utc_timestamp(feature["decision_time"])
            label_decision = _utc_timestamp(label["decision_time"])
            label_available = _utc_timestamp(label["available_as_of"])
            label_end = _utc_timestamp(label["label_end"])
        except (KeyError, TypeError, ValueError):
            reasons.append("PAIRWISE_RECORD_TIMESTAMP_INVALID")
            continue
        if feature.get("symbol") != label.get("symbol") or feature_decision != label_decision:
            reasons.append("PAIRWISE_RECORD_SCOPE_MISMATCH")
        if feature_available > feature_decision:
            reasons.append("PAIRWISE_FEATURE_AVAILABLE_AFTER_DECISION")
        if label_available <= label_decision or label_available < label_end:
            reasons.append("PAIRWISE_LABEL_AVAILABLE_TOO_EARLY")
        for field in ("history_digest", "feature_digest", "policy_digest"):
            if _HEX64.fullmatch(str(feature.get(field))) is None:
                reasons.append(f"PAIRWISE_FEATURE_DIGEST_INVALID:{field}")
        if _HEX64.fullmatch(str(label.get("label_digest"))) is None:
            reasons.append("PAIRWISE_LABEL_DIGEST_INVALID")
    return True, tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class LineageArtifact:
    role: str
    source_id: str
    source_path: str
    content_sha256: str
    available_as_of: str
    event_time_start: str
    event_time_end: str
    timezone: str
    revision_id: str
    point_in_time: bool
    artifact_digest: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LineageArtifact":
        missing = _ARTIFACT_FIELDS - set(value)
        if missing:
            raise ValueError(f"MISSING_ARTIFACT_FIELDS:{','.join(sorted(missing))}")
        extra = set(value) - _ARTIFACT_FIELDS
        if extra:
            raise ValueError(f"UNKNOWN_ARTIFACT_FIELDS:{','.join(sorted(extra))}")
        for field in _ARTIFACT_FIELDS - {"point_in_time"}:
            if not isinstance(value[field], str):
                raise TypeError(f"INVALID_ARTIFACT_FIELD_TYPE:{field}")
        if not isinstance(value["point_in_time"], bool):
            raise TypeError("INVALID_ARTIFACT_FIELD_TYPE:point_in_time")
        return cls(**{field: value[field] for field in _ARTIFACT_FIELDS})

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "content_sha256": self.content_sha256,
            "available_as_of": self.available_as_of,
            "event_time_start": self.event_time_start,
            "event_time_end": self.event_time_end,
            "timezone": self.timezone,
            "revision_id": self.revision_id,
            "point_in_time": self.point_in_time,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True)
class PITLineageManifest:
    as_of: str
    artifacts: Mapping[str, LineageArtifact]
    manifest_digest: str
    schema_version: str = PIT_LINEAGE_SCHEMA_VERSION

    @property
    def digest(self) -> str:
        return self.manifest_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of,
            "artifacts": {role: artifact.as_dict() for role, artifact in sorted(self.artifacts.items())},
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True)
class LineageVerification:
    status: str
    reasons: tuple[str, ...]
    covered_roles: tuple[str, ...]
    coverage_percent: int
    recomputed_digest: str


def build_lineage_artifact(
    *,
    role: str,
    path: str | Path,
    root: str | Path,
    source_id: str,
    revision_id: str,
    available_as_of: str,
    event_time_start: str,
    event_time_end: str,
    timezone: str,
    point_in_time: bool,
) -> LineageArtifact:
    """Snapshot one explicit file without interpreting its filename as provenance."""

    root_path = Path(root).resolve()
    source = Path(path).resolve()
    try:
        relative = source.relative_to(root_path)
    except ValueError as exc:
        raise LineageNotVerifiable("SOURCE_OUTSIDE_LINEAGE_ROOT") from exc
    if not source.is_file():
        raise LineageNotVerifiable("MISSING_LINEAGE_SOURCE")
    core = {
        "role": role,
        "source_id": source_id,
        "source_path": relative.as_posix(),
        "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "available_as_of": available_as_of,
        "event_time_start": event_time_start,
        "event_time_end": event_time_end,
        "timezone": timezone,
        "revision_id": revision_id,
        "point_in_time": point_in_time,
    }
    return LineageArtifact.from_dict({**core, "artifact_digest": _digest(core)})


def build_pit_manifest(*, as_of: str, artifacts: Mapping[str, LineageArtifact], root: str | Path) -> PITLineageManifest:
    core = {
        "schema_version": PIT_LINEAGE_SCHEMA_VERSION,
        "as_of": as_of,
        "artifacts": {role: artifact.as_dict() for role, artifact in sorted(artifacts.items())},
    }
    payload = {**core, "manifest_digest": _digest(core)}
    return load_pit_manifest(payload, root=root, expected_manifest_digest=str(payload["manifest_digest"]))


def inspect_lineage(
    payload: Mapping[str, Any], *, root: str | Path, expected_manifest_digest: str | None = None
) -> LineageVerification:
    """Independently recompute bytes and semantics; all uncertainty is fail-closed."""

    reasons: list[str] = []
    manifest_fields = {"schema_version", "as_of", "artifacts", "manifest_digest"}
    missing_manifest_fields = manifest_fields - set(payload)
    unknown_manifest_fields = set(payload) - manifest_fields
    if missing_manifest_fields:
        reasons.append(f"MISSING_MANIFEST_FIELDS:{','.join(sorted(missing_manifest_fields))}")
    if unknown_manifest_fields:
        reasons.append(f"UNKNOWN_MANIFEST_FIELDS:{','.join(sorted(unknown_manifest_fields))}")
    if payload.get("schema_version") != PIT_LINEAGE_SCHEMA_VERSION:
        reasons.append("UNKNOWN_LINEAGE_SCHEMA_VERSION")
    try:
        as_of = _utc_timestamp(payload.get("as_of"))
    except (TypeError, ValueError):
        as_of = None
        reasons.append("INVALID_MANIFEST_AS_OF")

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raw_artifacts = {}
        reasons.append("INVALID_ARTIFACT_MAP")
    present_roles = {str(role) for role in raw_artifacts}
    missing_roles = REQUIRED_LINEAGE_ROLES - present_roles
    extra_roles = present_roles - REQUIRED_LINEAGE_ROLES
    if missing_roles:
        reasons.append(f"MISSING_ROLES:{','.join(sorted(missing_roles))}")
    if extra_roles:
        reasons.append(f"UNKNOWN_ROLES:{','.join(sorted(extra_roles))}")

    root_path = Path(root).resolve()
    artifacts: dict[str, LineageArtifact] = {}
    for role in sorted(REQUIRED_LINEAGE_ROLES & present_roles):
        raw = raw_artifacts[role]
        if not isinstance(raw, Mapping):
            reasons.append(f"INVALID_ARTIFACT:{role}")
            continue
        try:
            artifact = LineageArtifact.from_dict(raw)
        except (TypeError, ValueError):
            reasons.append(f"INVALID_ARTIFACT:{role}")
            continue
        artifacts[role] = artifact
        if artifact.role != role:
            reasons.append(f"ROLE_KEY_MISMATCH:{role}")
        core = artifact.as_dict()
        core.pop("artifact_digest")
        if artifact.artifact_digest != _digest(core):
            reasons.append(f"ARTIFACT_DIGEST_MISMATCH:{role}")
        if _HEX64.fullmatch(str(artifact.content_sha256)) is None:
            reasons.append(f"INVALID_CONTENT_DIGEST:{role}")
        if (
            not artifact.source_id.strip()
            or not artifact.revision_id.strip()
            or _AMBIGUOUS_PROVENANCE.search(artifact.source_id)
            or _AMBIGUOUS_PROVENANCE.search(artifact.revision_id)
        ):
            reasons.append(f"AMBIGUOUS_PROVENANCE:{role}")
        relative = Path(artifact.source_path)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in {"", "."}:
            reasons.append(f"UNSAFE_SOURCE_PATH:{role}")
        else:
            source = (root_path / relative).resolve()
            try:
                source.relative_to(root_path)
            except ValueError:
                reasons.append(f"UNSAFE_SOURCE_PATH:{role}")
            else:
                if not source.is_file():
                    reasons.append(f"MISSING_SOURCE_FILE:{role}")
                elif hashlib.sha256(source.read_bytes()).hexdigest() != artifact.content_sha256:
                    reasons.append(f"CONTENT_DIGEST_MISMATCH:{role}")
        if artifact.timezone != "UTC":
            reasons.append(f"NON_UTC_TIMEZONE:{role}")
        if artifact.point_in_time is not True:
            reasons.append(f"NOT_POINT_IN_TIME:{role}")
        try:
            available = _utc_timestamp(artifact.available_as_of)
            start = _utc_timestamp(artifact.event_time_start)
            end = _utc_timestamp(artifact.event_time_end)
            if start > end:
                reasons.append(f"INVALID_EVENT_WINDOW:{role}")
            if as_of is not None and available > as_of:
                reasons.append(f"AVAILABLE_AFTER_AS_OF:{role}")
            if as_of is not None and end > as_of:
                reasons.append(f"EVENT_AFTER_AS_OF:{role}")
        except (TypeError, ValueError):
            reasons.append(f"INVALID_TIMESTAMP:{role}")

    if {"features", "labels"} <= set(artifacts):
        pairwise_declared, pairwise_reasons = _pairwise_lineage_reasons(
            root=root_path,
            feature_artifact=artifacts["features"],
            label_artifact=artifacts["labels"],
        )
        if pairwise_declared:
            reasons.extend(pairwise_reasons)
        else:
            try:
                feature_end = _utc_timestamp(artifacts["features"].event_time_end)
                label_start = _utc_timestamp(artifacts["labels"].event_time_start)
                if feature_end >= label_start:
                    reasons.append("FEATURE_LABEL_WINDOW_LEAKAGE")
            except ValueError:
                pass

    core_manifest = {key: value for key, value in payload.items() if key != "manifest_digest"}
    recomputed = _digest(core_manifest)
    claimed = payload.get("manifest_digest")
    if _HEX64.fullmatch(str(claimed)) is None or claimed != recomputed:
        reasons.append("MANIFEST_DIGEST_MISMATCH")
    if expected_manifest_digest is not None and claimed != expected_manifest_digest:
        reasons.append("EXPECTED_MANIFEST_DIGEST_MISMATCH")

    unique_reasons = tuple(dict.fromkeys(reasons))
    covered = tuple(sorted(artifacts))
    coverage = int(100 * len(covered) / len(REQUIRED_LINEAGE_ROLES))
    return LineageVerification(
        status="NOT_VERIFIABLE" if unique_reasons else "VERIFIABLE",
        reasons=unique_reasons,
        covered_roles=covered,
        coverage_percent=coverage,
        recomputed_digest=recomputed,
    )


def load_pit_manifest(
    payload: Mapping[str, Any], *, root: str | Path, expected_manifest_digest: str | None = None
) -> PITLineageManifest:
    report = inspect_lineage(payload, root=root, expected_manifest_digest=expected_manifest_digest)
    if report.status != "VERIFIABLE":
        raise LineageNotVerifiable(";".join(report.reasons))
    raw_artifacts = payload["artifacts"]
    return PITLineageManifest(
        as_of=str(payload["as_of"]),
        artifacts=MappingProxyType(
            {role: LineageArtifact.from_dict(raw_artifacts[role]) for role in sorted(raw_artifacts)}
        ),
        manifest_digest=str(payload["manifest_digest"]),
        schema_version=str(payload["schema_version"]),
    )


def require_experiment_binding(manifest: PITLineageManifest, identity: Any, checkpoint: Any) -> dict[str, str]:
    """Require the PIT manifest to be carried by both ExperimentRun and checkpoint."""

    artifacts = manifest.artifacts
    reasons: list[str] = []
    expected = {
        "pit_manifest_digest": manifest.digest,
        "dataset_manifest_digest": artifacts["dataset"].artifact_digest,
        "feature_digest": artifacts["features"].artifact_digest,
        "label_digest": artifacts["labels"].artifact_digest,
        "cost_model": artifacts["costs"].artifact_digest,
        "universe": artifacts["universe_membership"].source_id,
    }
    identity_values = {
        "pit_manifest_digest": identity.pit_manifest_digest,
        "dataset_manifest_digest": identity.dataset_manifest_digest,
        "feature_digest": identity.feature_digest,
        "label_digest": identity.label_digest,
        "cost_model": identity.cost_model,
        "universe": identity.universe,
    }
    checkpoint_values = {
        "pit_manifest_digest": checkpoint.pit_manifest_digest,
        "dataset_manifest_digest": checkpoint.dataset_manifest_digest,
        "feature_digest": checkpoint.feature_digest,
        "label_digest": checkpoint.label_digest,
        "cost_model": checkpoint.cost_model,
        "universe": checkpoint.universe,
    }
    for field, value in expected.items():
        if identity_values[field] != value:
            reasons.append(f"{field.upper()}_BINDING_MISMATCH")
        if checkpoint_values[field] != value:
            reasons.append(f"CHECKPOINT_{field.upper()}_BINDING_MISMATCH")
    if checkpoint.identity_digest != identity.digest:
        reasons.append("CHECKPOINT_IDENTITY_BINDING_MISMATCH")
    if checkpoint.run_id != identity.run_id:
        reasons.append("CHECKPOINT_RUN_BINDING_MISMATCH")
    if reasons:
        raise LineageNotVerifiable(";".join(reasons))
    return {
        "status": "BOUND",
        "manifest_digest": manifest.digest,
        "identity_digest": identity.digest,
        "checkpoint_digest": checkpoint.digest,
    }
