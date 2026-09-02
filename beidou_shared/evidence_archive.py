"""Evidence-preserving local file rotation with verified gzip archives."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_file(path: Path, *, reason: str, archive_dir: Path | None = None) -> dict[str, Any] | None:
    """Atomically move one closed file into a verified immutable gzip archive."""

    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        return None
    source_sha256 = _file_sha256(source)
    source_size = source.stat().st_size
    destination_dir = archive_dir or source.parent / f"{source.name}.archive"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    raw_archive = destination_dir / f"{source.name}.{stamp}.{source_sha256[:16]}"
    os.replace(source, raw_archive)
    _fsync_directory(source.parent)

    compressed_archive = raw_archive.with_suffix(f"{raw_archive.suffix}.gz")
    temporary = compressed_archive.with_suffix(f"{compressed_archive.suffix}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressor,
                raw_archive.open("rb") as input_file,
            ):
                for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                    compressor.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    restored_sha256 = hashlib.sha256()
    with gzip.open(temporary, "rb") as restored:
        for chunk in iter(lambda: restored.read(1024 * 1024), b""):
            restored_sha256.update(chunk)
    if restored_sha256.hexdigest() != source_sha256:
        temporary.unlink(missing_ok=True)
        raise OSError("compressed evidence archive failed hash verification")

    os.replace(temporary, compressed_archive)
    compressed_archive.chmod(0o400)
    raw_archive.unlink()
    _fsync_directory(destination_dir)
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason),
        "source_path": str(source),
        "source_size_bytes": source_size,
        "source_sha256": source_sha256,
        "archive_path": str(compressed_archive),
        "archive_size_bytes": compressed_archive.stat().st_size,
        "compression": "gzip",
    }
    manifest = destination_dir / "manifest.jsonl"
    manifest_descriptor = os.open(manifest, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            manifest_descriptor,
            (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        os.fsync(manifest_descriptor)
    finally:
        os.close(manifest_descriptor)
    _fsync_directory(destination_dir)
    return report


class EvidenceArchiveRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Size-based handler that archives every generation without deletion."""

    def __init__(self, filename: str, *, max_bytes: int) -> None:
        super().__init__(filename, maxBytes=max_bytes, backupCount=1, encoding="utf-8")

    def doRollover(self) -> None:  # noqa: N802 - logging.Handler API
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            archive_file(Path(self.baseFilename), reason="SIZE_ROTATION")
        finally:
            if not self.delay:
                self.stream = self._open()


__all__ = ["EvidenceArchiveRotatingFileHandler", "archive_file"]
