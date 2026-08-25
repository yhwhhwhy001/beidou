"""Fail-closed resolver for the Factor Miner resume worker."""

from __future__ import annotations

import json
import os
from pathlib import Path

from beidou_research.mining.runner import ResumableMiningRunner, ResumeRunError

DEFAULT_RESUME_ROOT = Path(".beidou/factor-miner-runs")


def resume_enabled() -> bool:
    """Rollback switch: only an explicit false value disables resume routing."""

    return os.environ.get("BEIDOU_FACTOR_MINER_RESUME_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def resolve_run_directory(state_root: str | Path, run_id: str) -> Path:
    """Resolve exactly one manifest whose immutable identity matches ``run_id``."""

    requested = run_id.strip()
    if not requested:
        raise ResumeRunError("RUN_ID_REQUIRED")
    root = Path(state_root)
    if not root.is_dir():
        raise ResumeRunError("RESUME_ROOT_MISSING")

    matches: list[Path] = []
    corrupt_named_match = False
    for manifest_path in sorted(root.glob(f"**/{ResumableMiningRunner.manifest_name}")):
        try:
            manifest = json.loads(manifest_path.read_text())
            identity = manifest["identity"]
            if isinstance(identity, dict) and identity.get("run_id") == requested:
                matches.append(manifest_path.parent)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            if manifest_path.parent.name == requested:
                corrupt_named_match = True

    if corrupt_named_match and not matches:
        raise ResumeRunError("CORRUPT_RUN_MANIFEST")
    if not matches:
        raise ResumeRunError("RUN_ID_NOT_FOUND")
    if len(matches) != 1:
        raise ResumeRunError("AMBIGUOUS_RUN_ID")
    return matches[0]


def resume_run(run_id: str, *, state_root: str | Path = DEFAULT_RESUME_ROOT) -> dict:
    """Resume one run; rejected routing can never call the fresh-run API."""

    if not resume_enabled():
        raise ResumeRunError("RESUME_DISABLED")
    run_dir = resolve_run_directory(state_root, run_id)
    return ResumableMiningRunner.resume(run_dir)
