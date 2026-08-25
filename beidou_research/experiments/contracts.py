"""Pure contracts for the ExperimentRun ledger.

The contracts deliberately have no filesystem, clock, database, or network
side effects.  The store injects timestamps and writer identities explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

EXPERIMENT_RUN_SCHEMA_VERSION = "1.0"
GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    """Return the one JSON representation used by all ledger hashes."""

    def reject_non_finite(item: Any) -> Any:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("NON_FINITE_JSON_VALUE")
        if isinstance(item, Mapping):
            return {str(k): reject_non_finite(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [reject_non_finite(v) for v in item]
        return item

    return json.dumps(reject_non_finite(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExperimentRunIdentity:
    """The complete immutable identity of one experiment run."""

    run_id: str
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
    schema_version: str = EXPERIMENT_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in (
            "run_id",
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
            "schema_version",
        ):
            if not str(self.as_dict()[field]).strip():
                raise ValueError(f"EMPTY_IDENTITY_FIELD:{field}")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        if self.schema_version not in {EXPERIMENT_RUN_SCHEMA_VERSION}:
            raise ValueError(f"UNKNOWN_SCHEMA_VERSION:{self.schema_version}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "code_commit": self.code_commit,
            "code_tree_digest": self.code_tree_digest,
            "policy_digest": self.policy_digest,
            "dataset_manifest_digest": self.dataset_manifest_digest,
            "pit_manifest_digest": self.pit_manifest_digest,
            "universe": self.universe,
            "timeframe": self.timeframe,
            "feature_digest": self.feature_digest,
            "label_digest": self.label_digest,
            "cost_model": self.cost_model,
            "seed": self.seed,
            "schema_version": self.schema_version,
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperimentRunIdentity":
        fields = {
            "run_id",
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
            "schema_version",
        }
        missing = fields - set(value)
        if missing:
            raise ValueError(f"MISSING_IDENTITY_FIELDS:{','.join(sorted(missing))}")
        return cls(**{name: value[name] for name in fields})


@dataclass(frozen=True)
class LedgerEvent:
    """Immutable event envelope; ``event_hash`` covers every fact field."""

    sequence: int
    previous_hash: str
    event_hash: str
    run_id: str
    writer_id: str
    fencing_token: str
    stage: str
    timestamp: str
    payload_json: str
    payload_digest: str
    idempotency_key: str
    schema_version: str = EXPERIMENT_RUN_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        sequence: int,
        previous_hash: str,
        run_id: str,
        writer_id: str,
        fencing_token: str,
        stage: str,
        timestamp: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        schema_version: str = EXPERIMENT_RUN_SCHEMA_VERSION,
    ) -> "LedgerEvent":
        payload_json = canonical_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        fact = {
            "sequence": sequence,
            "previous_hash": previous_hash,
            "run_id": run_id,
            "writer_id": writer_id,
            "fencing_token": fencing_token,
            "stage": stage,
            "timestamp": timestamp,
            "payload_json": payload_json,
            "payload_digest": payload_digest,
            "idempotency_key": idempotency_key,
            "schema_version": schema_version,
        }
        event_hash = hashlib.sha256(canonical_json(fact).encode("utf-8")).hexdigest()
        return cls(event_hash=event_hash, **fact)

    @property
    def fact(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "writer_id": self.writer_id,
            "fencing_token": self.fencing_token,
            "stage": self.stage,
            "timestamp": self.timestamp,
            "payload_json": self.payload_json,
            "payload_digest": self.payload_digest,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
        }

    def hash_input(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "previous_hash": self.previous_hash, **self.fact}

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            **self.fact,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LedgerEvent":
        return cls(
            sequence=int(row["sequence"]),
            previous_hash=str(row["previous_hash"]),
            event_hash=str(row["event_hash"]),
            run_id=str(row["run_id"]),
            writer_id=str(row["writer_id"]),
            fencing_token=str(row["fencing_token"]),
            stage=str(row["stage"]),
            timestamp=str(row["timestamp"]),
            payload_json=str(row["payload_json"]),
            payload_digest=str(row["payload_digest"]),
            idempotency_key=str(row["idempotency_key"]),
            schema_version=str(row["schema_version"]),
        )
