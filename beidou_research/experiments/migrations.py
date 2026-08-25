"""Additive local schema bootstrap for the ExperimentRun ledger."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import ExperimentRunIdentity
from .store import ExperimentRunLedger


@dataclass(frozen=True)
class MigrationResult:
    path: str
    additive: bool
    schema_digest: str


def apply_additive(
    path: str | Path, identity: ExperimentRunIdentity, writer_id: str, fencing_token: str
) -> MigrationResult:
    """Create only missing ledger tables; never rewrite or delete old data."""

    ledger = ExperimentRunLedger(path, identity, writer_id, fencing_token)
    try:
        return MigrationResult(str(path), True, ledger.schema_digest)
    finally:
        ledger.close()
