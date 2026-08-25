"""Canonical, hash-bound and atomically persisted experiment checkpoints.

This slice intentionally supports one deterministic scope only: Template Grid
over one dataset, universe, and timeframe with ``jobs=1``.  A rejected
checkpoint is never interpreted as permission to start a fresh run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import ExperimentRunIdentity, canonical_json
from .store import ExperimentRunLedger

CHECKPOINT_SCHEMA_VERSION = "1.0"
_SCOPE = {"search": "TEMPLATE_GRID", "datasets": 1, "universes": 1, "timeframes": 1, "jobs": 1}


class CheckpointError(RuntimeError):
    """Base error for rejected checkpoint operations."""


class CheckpointNotVerifiableError(CheckpointError):
    """Checkpoint bytes, fields, or generation pointer cannot be verified."""


class CheckpointCompatibilityError(CheckpointError):
    """Checkpoint is valid but does not match the declared resume inputs."""


class CheckpointStaleWriterError(CheckpointError):
    """Checkpoint writer has been fenced by a newer token."""


CheckpointNotVerifiable = CheckpointNotVerifiableError


_CORE_FIELDS = (
    "run_id",
    "event_sequence",
    "previous_hash",
    "event_hash",
    "code_commit",
    "code_tree_digest",
    "policy_digest",
    "dataset_manifest_digest",
    "pit_manifest_digest",
    "universe",
    "timeframe",
    "feature_digest",
    "label_digest",
    "cost_model",
    "seed",
    "rng_state",
    "search_state",
    "optimizer_state",
    "candidate_queue",
    "completed_candidate_ids",
    "multiple_testing_family",
    "multiple_testing_denominator",
    "stage_cursor",
    "worker_count",
    "scope",
    "idempotency_keys",
    "schema_version",
    "writer_id",
    "fencing_token",
)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    """All state required to resume exactly the declared experiment."""

    run_id: str
    event_sequence: int
    previous_hash: str
    event_hash: str
    code_commit: str
    code_tree_digest: str
    policy_digest: str
    dataset_manifest_digest: str
    pit_manifest_digest: str
    universe: str
    timeframe: str
    feature_digest: str
    label_digest: str
    cost_model: str
    seed: int
    rng_state: Mapping[str, Any]
    search_state: Mapping[str, Any]
    optimizer_state: Mapping[str, Any]
    candidate_queue: tuple[str, ...]
    completed_candidate_ids: tuple[str, ...]
    multiple_testing_family: str
    multiple_testing_denominator: int
    stage_cursor: str
    worker_count: int
    scope: Mapping[str, Any]
    idempotency_keys: tuple[str, ...]
    schema_version: str
    writer_id: str
    fencing_token: str
    atomic_storage_digest: str
    checkpoint_digest: str

    @classmethod
    def build(
        cls,
        *,
        identity: ExperimentRunIdentity,
        event_sequence: int,
        previous_hash: str,
        event_hash: str,
        rng_state: Mapping[str, Any],
        search_state: Mapping[str, Any],
        optimizer_state: Mapping[str, Any],
        candidate_queue: list[str] | tuple[str, ...],
        completed_candidate_ids: list[str] | tuple[str, ...],
        multiple_testing_family: str,
        multiple_testing_denominator: int,
        stage_cursor: str,
        worker_count: int,
        scope: Mapping[str, Any],
        idempotency_keys: list[str] | tuple[str, ...],
        writer_id: str,
        fencing_token: str,
        schema_version: str = CHECKPOINT_SCHEMA_VERSION,
    ) -> "Checkpoint":
        core = {
            **identity.as_dict(),
            "event_sequence": event_sequence,
            "previous_hash": previous_hash,
            "event_hash": event_hash,
            "rng_state": dict(rng_state),
            "search_state": dict(search_state),
            "optimizer_state": dict(optimizer_state),
            "candidate_queue": list(candidate_queue),
            "completed_candidate_ids": list(completed_candidate_ids),
            "multiple_testing_family": multiple_testing_family,
            "multiple_testing_denominator": multiple_testing_denominator,
            "stage_cursor": stage_cursor,
            "worker_count": worker_count,
            "scope": dict(scope),
            "idempotency_keys": list(idempotency_keys),
            "schema_version": schema_version,
            "writer_id": writer_id,
            "fencing_token": fencing_token,
        }
        return cls.from_dict(
            {
                **core,
                "atomic_storage_digest": _hash(core),
                "checkpoint_digest": _hash({**core, "atomic_storage_digest": _hash(core)}),
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Checkpoint":
        missing = set(_CORE_FIELDS) - set(value)
        if missing:
            raise ValueError(f"MISSING_CHECKPOINT_FIELDS:{','.join(sorted(missing))}")
        if value["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointNotVerifiable("UNKNOWN_CHECKPOINT_VERSION")
        if "atomic_storage_digest" not in value or "checkpoint_digest" not in value:
            raise CheckpointNotVerifiable("MISSING_CHECKPOINT_DIGEST")
        if value.get("worker_count") != 1 or dict(value.get("scope", {})) != _SCOPE:
            raise ValueError("UNSUPPORTED_RESUME_SCOPE")
        if not isinstance(value["multiple_testing_denominator"], int) or value["multiple_testing_denominator"] <= 0:
            raise ValueError("INVALID_MULTIPLE_TESTING_DENOMINATOR")
        if int(value["event_sequence"]) < 1 or int(value["worker_count"]) != 1:
            raise ValueError("INVALID_CHECKPOINT_SEQUENCE_OR_WORKERS")
        for key in ("writer_id", "fencing_token", "multiple_testing_family", "stage_cursor"):
            if not str(value[key]).strip():
                raise ValueError(f"EMPTY_CHECKPOINT_FIELD:{key}")
        core = {key: value[key] for key in _CORE_FIELDS}
        expected_storage = _hash(core)
        storage_digest = str(value["atomic_storage_digest"])
        if storage_digest != expected_storage:
            raise CheckpointNotVerifiable("ATOMIC_STORAGE_DIGEST_MISMATCH")
        expected_checkpoint = _hash({**core, "atomic_storage_digest": expected_storage})
        checkpoint_digest = str(value["checkpoint_digest"])
        if checkpoint_digest != expected_checkpoint:
            raise CheckpointNotVerifiable("CHECKPOINT_DIGEST_MISMATCH")
        return cls(
            run_id=str(value["run_id"]),
            event_sequence=int(value["event_sequence"]),
            previous_hash=str(value["previous_hash"]),
            event_hash=str(value["event_hash"]),
            code_commit=str(value["code_commit"]),
            code_tree_digest=str(value["code_tree_digest"]),
            policy_digest=str(value["policy_digest"]),
            dataset_manifest_digest=str(value["dataset_manifest_digest"]),
            pit_manifest_digest=str(value["pit_manifest_digest"]),
            universe=str(value["universe"]),
            timeframe=str(value["timeframe"]),
            feature_digest=str(value["feature_digest"]),
            label_digest=str(value["label_digest"]),
            cost_model=str(value["cost_model"]),
            seed=int(value["seed"]),
            rng_state=dict(value["rng_state"]),
            search_state=dict(value["search_state"]),
            optimizer_state=dict(value["optimizer_state"]),
            candidate_queue=tuple(str(x) for x in value["candidate_queue"]),
            completed_candidate_ids=tuple(str(x) for x in value["completed_candidate_ids"]),
            multiple_testing_family=str(value["multiple_testing_family"]),
            multiple_testing_denominator=int(value["multiple_testing_denominator"]),
            stage_cursor=str(value["stage_cursor"]),
            worker_count=int(value["worker_count"]),
            scope=dict(value["scope"]),
            idempotency_keys=tuple(str(x) for x in value["idempotency_keys"]),
            schema_version=str(value["schema_version"]),
            writer_id=str(value["writer_id"]),
            fencing_token=str(value["fencing_token"]),
            atomic_storage_digest=storage_digest,
            checkpoint_digest=checkpoint_digest,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **{key: self.__dict__[key] for key in _CORE_FIELDS},
            "rng_state": dict(self.rng_state),
            "search_state": dict(self.search_state),
            "optimizer_state": dict(self.optimizer_state),
            "candidate_queue": list(self.candidate_queue),
            "completed_candidate_ids": list(self.completed_candidate_ids),
            "scope": dict(self.scope),
            "idempotency_keys": list(self.idempotency_keys),
            "atomic_storage_digest": self.atomic_storage_digest,
            "checkpoint_digest": self.checkpoint_digest,
        }

    @property
    def digest(self) -> str:
        return self.checkpoint_digest

    def canonical_bytes(self) -> bytes:
        return (canonical_json(self.as_dict()) + "\n").encode("utf-8")


class CheckpointStore:
    """Real local filesystem adapter with immutable generations and fencing."""

    def __init__(
        self,
        root: str | Path,
        identity: ExperimentRunIdentity,
        writer_id: str,
        fencing_token: str,
        *,
        register: bool = True,
    ) -> None:
        if not writer_id or not fencing_token:
            raise ValueError("writer_id and fencing_token are required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.writer_id = writer_id
        self.fencing_token = fencing_token
        self._fence_path = self.root / "writer-fence.json"
        if register:
            self._atomic_bytes(
                self._fence_path,
                (canonical_json({"writer_id": writer_id, "fencing_token": fencing_token}) + "\n").encode(),
            )
        elif not self._fence_path.exists():
            raise CheckpointNotVerifiable("MISSING_WRITER_FENCE")

    def _atomic_bytes(self, path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _check_fence(self) -> None:
        try:
            current = json.loads(self._fence_path.read_text())
        except (OSError, ValueError) as exc:
            raise CheckpointNotVerifiable("INVALID_WRITER_FENCE") from exc
        if current.get("writer_id") != self.writer_id or current.get("fencing_token") != self.fencing_token:
            raise CheckpointStaleWriterError("STALE_CHECKPOINT_WRITER")

    def _pointer(self) -> dict[str, Any]:
        path = self.root / "checkpoint.current"
        if not path.exists():
            generations = list(self.root.glob("checkpoint-*.json"))
            if generations:
                raise CheckpointNotVerifiable("MISSING_CHECKPOINT_POINTER")
            raise CheckpointNotVerifiable("NO_CHECKPOINT_GENERATION")
        try:
            pointer = json.loads(path.read_text())
            if not isinstance(pointer, dict) or not {"filename", "digest", "generation"} <= set(pointer):
                raise ValueError("incomplete pointer")
            filename = str(pointer["filename"])
            if Path(filename).name != filename or re.fullmatch(r"checkpoint-[0-9]{6}\.json", filename) is None:
                raise ValueError("unsafe generation filename")
            if int(pointer["generation"]) < 1 or re.fullmatch(r"[0-9a-f]{64}", str(pointer["digest"])) is None:
                raise ValueError("invalid generation metadata")
            return pointer
        except (OSError, ValueError, TypeError) as exc:
            raise CheckpointNotVerifiable("INVALID_CHECKPOINT_POINTER") from exc

    def save(self, checkpoint: Checkpoint, *, ledger: ExperimentRunLedger | None = None) -> Checkpoint:
        self._check_fence()
        if (
            checkpoint.run_id != self.identity.run_id
            or checkpoint.writer_id != self.writer_id
            or checkpoint.fencing_token != self.fencing_token
        ):
            raise CheckpointStaleWriterError("CHECKPOINT_WRITER_OR_RUN_MISMATCH")
        if ledger is not None:
            events = ledger.events()
            if (
                not events
                or checkpoint.event_sequence != events[-1].sequence
                or checkpoint.event_hash != events[-1].event_hash
                or checkpoint.previous_hash != events[-1].previous_hash
            ):
                raise CheckpointNotVerifiable("LEDGER_CHECKPOINT_SEQUENCE_MISMATCH")
            if checkpoint.stage_cursor != events[-1].stage or tuple(checkpoint.idempotency_keys) != tuple(
                event.idempotency_key for event in events
            ):
                raise CheckpointNotVerifiable("LEDGER_CHECKPOINT_STATE_MISMATCH")
        try:
            pointer = self._pointer()
            generation = int(pointer["generation"]) + 1
        except CheckpointNotVerifiable as exc:
            if str(exc) != "NO_CHECKPOINT_GENERATION":
                raise
            generation = 1
        payload = checkpoint.canonical_bytes()
        filename = f"checkpoint-{generation:06d}.json"
        target = self.root / filename
        if target.exists() and target.read_bytes() != payload:
            raise CheckpointNotVerifiable("IMMUTABLE_GENERATION_CONFLICT")
        if not target.exists():
            self._atomic_bytes(target, payload)
        pointer_payload = {
            "filename": filename,
            "generation": generation,
            "digest": hashlib.sha256(payload).hexdigest(),
        }
        self._atomic_bytes(self.root / "checkpoint.current", (canonical_json(pointer_payload) + "\n").encode())
        return checkpoint

    def current_path(self) -> Path:
        return self.root / str(self._pointer()["filename"])

    def load(
        self,
        *,
        expected_identity: ExperimentRunIdentity | None = None,
        expected_policy_digest: str | None = None,
        expected_denominator: int | None = None,
        ledger: ExperimentRunLedger | None = None,
    ) -> Checkpoint:
        pointer = self._pointer()
        path = self.root / str(pointer["filename"])
        if not path.is_file():
            raise CheckpointNotVerifiable("MISSING_CHECKPOINT_PAYLOAD")
        try:
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != pointer["digest"]:
                raise CheckpointNotVerifiable("CHECKPOINT_STORAGE_DIGEST_MISMATCH")
            value = json.loads(payload)
            checkpoint = Checkpoint.from_dict(value)
            if checkpoint.canonical_bytes() != payload:
                raise CheckpointNotVerifiable("NON_CANONICAL_CHECKPOINT_BYTES")
        except CheckpointError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CheckpointNotVerifiable("INVALID_CHECKPOINT_PAYLOAD") from exc
        expected = expected_identity or self.identity
        if (
            checkpoint.run_id != expected.run_id
            or checkpoint.code_commit != expected.code_commit
            or checkpoint.code_tree_digest != expected.code_tree_digest
            or checkpoint.policy_digest != expected.policy_digest
            or checkpoint.dataset_manifest_digest != expected.dataset_manifest_digest
            or checkpoint.pit_manifest_digest != expected.pit_manifest_digest
            or checkpoint.feature_digest != expected.feature_digest
            or checkpoint.label_digest != expected.label_digest
            or checkpoint.universe != expected.universe
            or checkpoint.timeframe != expected.timeframe
            or checkpoint.cost_model != expected.cost_model
            or checkpoint.seed != expected.seed
        ):
            raise CheckpointCompatibilityError("IDENTITY_DRIFT")
        if expected_policy_digest is not None and checkpoint.policy_digest != expected_policy_digest:
            raise CheckpointCompatibilityError("POLICY_DRIFT")
        if expected_denominator is not None and checkpoint.multiple_testing_denominator != expected_denominator:
            raise CheckpointCompatibilityError("DENOMINATOR_DRIFT")
        if ledger is not None:
            events = ledger.events()
            if (
                not events
                or checkpoint.event_sequence != events[-1].sequence
                or checkpoint.event_hash != events[-1].event_hash
                or checkpoint.previous_hash != events[-1].previous_hash
            ):
                raise CheckpointNotVerifiable("LEDGER_CHECKPOINT_SEQUENCE_MISMATCH")
            if checkpoint.stage_cursor != events[-1].stage or tuple(checkpoint.idempotency_keys) != tuple(
                event.idempotency_key for event in events
            ):
                raise CheckpointNotVerifiable("LEDGER_CHECKPOINT_STATE_MISMATCH")
        return checkpoint
