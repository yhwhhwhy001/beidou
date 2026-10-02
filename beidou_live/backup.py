"""Nightly copies of the live record (operator ruling 2026-10-02, the 09-30 system audit's D8 option B).

`.beidou/live` is the only out-of-sample evidence there is - the live period is the holdout (KILL-006) - and
until 2026-10-02 nothing copied it (the audit's S3).  The operator chose option B for now and Time Machine
once a disk is attached: a dated archive each night, the newest `BACKUP_KEEP` kept.  It guards against a
wrong command or an overwrite, not against the disk it sits on; that is the Time Machine half.

The copies stay on this machine and never inside the repository, which is public: `write_backup` refuses a
destination inside the checkout it runs from.  Owner-only permissions, because the record carries the
account's equity and positions.
"""

from __future__ import annotations

import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path

BACKUP_KEEP = 14
REPOSITORY = Path(__file__).resolve().parents[1]


class BackupRefused(ValueError):
    """The copy was not attempted: it would land in the repository, or there is no record to copy."""


def write_backup(source: Path, target: Path, keep: int = BACKUP_KEEP, *, now: datetime | None = None) -> Path:
    """Archive ``source`` into ``target`` as ``live-<UTC stamp>.tar.gz``; keep the newest ``keep``; return the path.

    Written under a hidden name and renamed, so an interrupted night leaves no archive that looks whole.
    """
    if keep < 1:
        raise BackupRefused(f"keep must be at least 1, not {keep}")
    if target.resolve().is_relative_to(REPOSITORY):
        raise BackupRefused(f"{target} is inside the repository, which is public: the live record never goes there")
    if not (source / "cycles.jsonl").is_file():
        raise BackupRefused(f"{source} holds no cycles.jsonl: nothing that looks like the live record")
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    name = f"live-{(now or datetime.now(UTC)):%Y%m%dT%H%M%SZ}.tar.gz"
    staging = target / f".{name}.partial"
    try:
        with (
            os.fdopen(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as raw,
            tarfile.open(fileobj=raw, mode="w:gz") as archive,
        ):
            archive.add(source, arcname="live")
        staging.replace(target / name)
    finally:
        staging.unlink(missing_ok=True)
    for old in sorted(target.glob("live-*.tar.gz"))[:-keep]:
        old.unlink()
    return target / name


__all__ = ["BACKUP_KEEP", "BackupRefused", "write_backup"]
