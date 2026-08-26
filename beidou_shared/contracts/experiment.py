"""Neutral experiment/data references used by offline Alpha composition.

This module intentionally contains immutable value contracts only.  It does
not discover data, open a store, read environment variables, or register a
global service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from beidou_shared.types import SchemaVersion


class ExperimentStage(str, Enum):
    """The small, transport-independent stage vocabulary for a run."""

    CREATED = "CREATED"
    ALPHA_EVALUATED = "ALPHA_EVALUATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """An immutable reference to already-bound local input data."""

    dataset_id: str
    version: SchemaVersion
    content_hash: str
    source: str = "LOCAL"

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ValueError("dataset_id must be non-empty")
        if not str(self.version).strip():
            raise ValueError("version must be non-empty")
        if not self.content_hash.strip():
            raise ValueError("content_hash must be non-empty")
        if self.source != "LOCAL":
            raise ValueError("offline Alpha requires a LOCAL dataset reference")


@dataclass(frozen=True, slots=True)
class ExperimentRunRef:
    """Stable identity material for an experiment without a persistence API."""

    run_id: str
    code_hash: str
    policy_version: SchemaVersion
    dataset: DatasetRef
    started_at: datetime
    stage: ExperimentStage = ExperimentStage.CREATED

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.code_hash.strip():
            raise ValueError("run_id and code_hash must be non-empty")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
