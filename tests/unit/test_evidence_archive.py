"""Evidence-preserving archive and rotating-handler contracts."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
from pathlib import Path

import pytest

import beidou_shared.evidence_archive as archive_module
from beidou_shared.evidence_archive import EvidenceArchiveRotatingFileHandler, archive_file


def test_archive_file_handles_absent_and_empty_sources(tmp_path: Path) -> None:
    assert archive_file(tmp_path / "missing.log", reason="TEST") is None
    empty = tmp_path / "empty.log"
    empty.touch()
    assert archive_file(empty, reason="TEST") is None


def test_archive_file_preserves_bytes_hash_permissions_and_manifest(tmp_path: Path) -> None:
    source = tmp_path / "evidence.log"
    original = ("evidence-line\n" * 100).encode()
    source.write_bytes(original)

    report = archive_file(source, reason="TEST_ROTATION")

    assert report is not None
    assert not source.exists()
    archived = Path(report["archive_path"])
    assert archived.stat().st_mode & 0o222 == 0
    with gzip.open(archived, "rb") as handle:
        assert handle.read() == original
    assert report["source_sha256"] == hashlib.sha256(original).hexdigest()
    manifest = json.loads((archived.parent / "manifest.jsonl").read_text(encoding="utf-8"))
    assert manifest["reason"] == "TEST_ROTATION"
    assert manifest["compression"] == "gzip"


def test_archive_file_keeps_raw_archive_when_compression_fails(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "failure.log"
    source.write_text("preserve me", encoding="utf-8")

    class BrokenCompressor:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("compression unavailable")

    monkeypatch.setattr(archive_module.gzip, "GzipFile", BrokenCompressor)

    with pytest.raises(RuntimeError, match="compression unavailable"):
        archive_file(source, reason="TEST_FAILURE")

    raw_archives = [path for path in (tmp_path / "failure.log.archive").iterdir() if not path.name.endswith(".tmp")]
    assert len(raw_archives) == 1
    assert raw_archives[0].read_text(encoding="utf-8") == "preserve me"


def test_archive_file_rejects_compressed_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "mismatch.log"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr(archive_module.gzip, "open", lambda *_args, **_kwargs: io.BytesIO(b"different"))

    with pytest.raises(OSError, match="hash verification"):
        archive_file(source, reason="TEST_MISMATCH")

    assert not list((tmp_path / "mismatch.log.archive").glob("*.tmp"))


def test_rotating_handler_archives_complete_generations(tmp_path: Path) -> None:
    path = tmp_path / "engine.log"
    logger = logging.getLogger("beidou-test-evidence-archive")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = EvidenceArchiveRotatingFileHandler(str(path), max_bytes=64)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("first-generation-%s", "x" * 80)
        logger.info("second-generation-%s", "y" * 80)
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()

    archives = sorted((tmp_path / "engine.log.archive").glob("*.gz"))
    assert archives
    archived_parts: list[str] = []
    for item in archives:
        with gzip.open(item, "rt", encoding="utf-8") as handle:
            archived_parts.append(handle.read())
    archived_text = "".join(archived_parts)
    assert "first-generation" in archived_text
    assert path.exists()
