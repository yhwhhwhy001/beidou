#!/usr/bin/env python3
# ruff: noqa: S603,S607,T201
"""Capture or verify hashes for pre-existing tracked and untracked worktree changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def run_git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed without exposing repository content")
    return result.stdout


def run_git_text(repo: Path, *args: str) -> str:
    return run_git_bytes(repo, *args).decode("utf-8", errors="strict").strip()


def content_fact(repo: Path, relative: str) -> tuple[str, str | None]:
    path = repo / relative
    if path.is_symlink():
        return "SYMLINK", sha256_bytes(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    if not path.exists():
        return "DELETED", None
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "REGULAR", digest.hexdigest()
    return "OTHER", None


def normalize_ignored_prefixes(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = value.rstrip("/")
        path = Path(candidate)
        if not candidate or path.is_absolute() or ".." in path.parts or "\x00" in candidate:
            raise ValueError(f"unsafe ignored untracked prefix: {value}")
        normalized.append(candidate + "/")
    if len(normalized) != len(set(normalized)):
        raise ValueError("duplicate ignored untracked prefix")
    return sorted(normalized)


def is_ignored_untracked(relative: str, ignored_prefixes: list[str]) -> bool:
    return any(relative.startswith(prefix) for prefix in ignored_prefixes)


def capture(repo: Path, ignored_untracked_prefixes: list[str] | None = None) -> dict[str, Any]:
    repo = repo.resolve()
    ignored_prefixes = normalize_ignored_prefixes(ignored_untracked_prefixes or [])
    if run_git_text(repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError("repository is not a Git worktree")
    head = run_git_text(repo, "rev-parse", "HEAD")
    payload = run_git_bytes(repo, "diff", "--name-status", "--no-renames", "-z", "HEAD", "--")
    fields = payload.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise ValueError("unexpected Git name-status record shape")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(0, len(fields), 2):
        status = fields[index].decode("ascii", errors="strict")[:1]
        relative = fields[index + 1].decode("utf-8", errors="surrogateescape")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or "\x00" in relative:
            raise ValueError("Git returned an unsafe changed path")
        if relative in seen:
            raise ValueError("duplicate tracked changed path")
        seen.add(relative)
        kind, digest = content_fact(repo, relative)
        entries.append({"path": relative, "status": status, "kind": kind, "sha256": digest})
    tracked_change_count = len(entries)
    untracked_payload = run_git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z", "--")
    for raw_relative in untracked_payload.split(b"\0"):
        if not raw_relative:
            continue
        relative = raw_relative.decode("utf-8", errors="surrogateescape")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or "\x00" in relative:
            raise ValueError("Git returned an unsafe untracked path")
        if is_ignored_untracked(relative, ignored_prefixes):
            continue
        if relative in seen:
            raise ValueError("duplicate changed path")
        seen.add(relative)
        kind, digest = content_fact(repo, relative)
        entries.append({"path": relative, "status": "?", "kind": kind, "sha256": digest})
    entries.sort(key=lambda item: item["path"])
    entries_payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": "1.0.0",
        "capture_mode": "TRACKED_AND_UNTRACKED_CHANGES_NO_CONTENT",
        "repository": str(repo),
        "head": head,
        "tracked_change_count": tracked_change_count,
        "untracked_change_count": len(entries) - tracked_change_count,
        "ignored_untracked_prefixes": ignored_prefixes,
        "entries": entries,
        "entries_sha256": sha256_bytes(entries_payload),
    }


def load_json(path: Path) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(value, dict):
        raise ValueError("custody manifest must be an object")
    return value


def validate_manifest(package_root: Path, manifest_path: Path) -> dict[str, Any]:
    validator = [
        sys.executable,
        str(package_root / "scripts/validate_package.py"),
        "--validate-baseline-custody",
        str(manifest_path),
    ]
    result = subprocess.run(
        validator,
        cwd=package_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"custody schema invalid: {(result.stderr or result.stdout).strip()}")
    return load_json(manifest_path)


def write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def verify(
    *,
    package_root: Path,
    original_repo: Path,
    approved_repo: Path,
    manifest_path: Path,
    output: Path,
) -> None:
    expected = validate_manifest(package_root, manifest_path)
    actual = capture(original_repo, expected["ignored_untracked_prefixes"])
    reasons: list[str] = []
    for field in (
        "repository",
        "head",
        "tracked_change_count",
        "untracked_change_count",
        "ignored_untracked_prefixes",
        "entries",
        "entries_sha256",
    ):
        if expected[field] != actual[field]:
            reasons.append(f"PROTECTED_SOURCE_{field.upper()}_MISMATCH")
    package_manifest = load_json(package_root / "PACKAGE-MANIFEST.json")
    approved_head = run_git_text(approved_repo.resolve(), "rev-parse", "HEAD")
    approved_dirty = run_git_text(approved_repo.resolve(), "status", "--porcelain=v1", "--untracked-files=all")
    implementation_baseline = package_manifest["source_snapshot"]["implementation_baseline_commit"]
    if implementation_baseline is None:
        reasons.append("IMPLEMENTATION_BASELINE_COMMIT_UNSET")
    elif approved_head != implementation_baseline:
        reasons.append("APPROVED_BASELINE_HEAD_MISMATCH")
    if approved_dirty:
        reasons.append("APPROVED_BASELINE_NOT_CLEAN")
    result = {
        "schema_version": "1.0.0",
        "status": "PASS" if not reasons else "FAIL",
        "protected_source_manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "protected_entries_sha256": actual["entries_sha256"],
        "protected_change_count": actual["tracked_change_count"] + actual["untracked_change_count"],
        "protected_tracked_change_count": actual["tracked_change_count"],
        "protected_untracked_change_count": actual["untracked_change_count"],
        "approved_head": approved_head,
        "approved_clean": not bool(approved_dirty),
        "failure_reasons": reasons,
    }
    write_new(output, result)
    if reasons:
        raise ValueError("baseline custody verification failed: " + ",".join(reasons))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--repo", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--ignore-untracked-prefix", action="append", default=[])
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--original-repo", type=Path, required=True)
    verify_parser.add_argument("--approved-repo", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package_root = args.root.resolve()
    try:
        if args.command == "capture":
            output = args.output.resolve()
            write_new(output, capture(args.repo.resolve(), args.ignore_untracked_prefix))
            result = {"status": "CUSTODY_CAPTURED", "output": str(output)}
        else:
            output = args.output.resolve()
            verify(
                package_root=package_root,
                original_repo=args.original_repo.resolve(),
                approved_repo=args.approved_repo.resolve(),
                manifest_path=args.manifest.resolve(),
                output=output,
            )
            result = {"status": "CUSTODY_VERIFIED", "output": str(output)}
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "CUSTODY_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
