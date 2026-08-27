#!/usr/bin/env python3
# ruff: noqa: T201
"""Create or verify the package's exact-file-set SHA-256 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

CHECKSUM_NAME = "CHECKSUMS.sha256"
LINE_RE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"symlink is forbidden: {relative}")
        if path.is_file() and relative != CHECKSUM_NAME:
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                raise ValueError(f"generated Python cache is forbidden: {relative}")
            files[relative] = path
    return files


def parse_manifest(root: Path) -> dict[str, str]:
    manifest_path = root / CHECKSUM_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f"missing regular {CHECKSUM_NAME}")
    entries: dict[str, str] = {}
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        match = LINE_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid checksum line {line_number}")
        digest, relative = match.groups()
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
            raise ValueError(f"unsafe checksum path on line {line_number}: {relative}")
        if relative == CHECKSUM_NAME:
            raise ValueError("checksum manifest cannot hash itself")
        if relative in entries:
            raise ValueError(f"duplicate checksum path: {relative}")
        entries[relative] = digest
    if list(entries) != sorted(entries):
        raise ValueError("checksum entries are not lexically sorted")
    return entries


def write_manifest(root: Path) -> dict[str, str]:
    files = package_files(root)
    entries = {relative: sha256(path) for relative, path in files.items()}
    payload = "".join(f"{digest}  {relative}\n" for relative, digest in sorted(entries.items()))
    (root / CHECKSUM_NAME).write_text(payload, encoding="utf-8", newline="\n")
    return entries


def verify(root: Path) -> dict[str, str]:
    expected = parse_manifest(root)
    actual_files = package_files(root)
    missing = sorted(set(expected) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected))
    if missing or extra:
        raise ValueError(f"exact file set mismatch: missing={missing}, extra={extra}")
    mismatched = [relative for relative, digest in expected.items() if sha256(actual_files[relative]) != digest]
    if mismatched:
        raise ValueError(f"checksum mismatch: {mismatched}")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        entries = write_manifest(root) if args.write else verify(root)
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "CHECKSUMS_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    status = "CHECKSUMS_WRITTEN" if args.write else "CHECKSUMS_OK"
    fingerprint = hashlib.sha256((root / CHECKSUM_NAME).read_bytes()).hexdigest()
    print(json.dumps({"status": status, "files": len(entries), "package_fingerprint": fingerprint}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
