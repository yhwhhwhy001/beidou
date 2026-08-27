#!/usr/bin/env python3
# ruff: noqa: S603,T201
"""Build and verify a deterministic, path-safe package ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is forbidden: {path.relative_to(root)}")
        if path.is_file():
            result.append(path)
    return result


def run_gate(root: Path, script: str, *args: str) -> None:
    command = [sys.executable, str(root / "scripts" / script), *args]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False, timeout=60)
    if result.returncode != 0:
        raise ValueError(f"{script} failed: {(result.stderr or result.stdout).strip()}")


def normalized_mode(root: Path, path: Path) -> int:
    relative = path.relative_to(root)
    return 0o755 if relative.parts[0] == "scripts" and path.suffix == ".py" else 0o644


def build(root: Path, output: Path, *, force: bool) -> str:
    try:
        output.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("archive output must be outside the package root")
    if output.exists() and not force:
        raise ValueError(f"output already exists; use --force to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_gate(root, "verify_checksums.py")
    run_gate(root, "validate_package.py", "--negative-fixtures")

    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        raise ValueError(f"temporary output already exists: {temporary}")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in package_files(root):
                relative = path.relative_to(root).as_posix()
                member = f"{root.name}/{relative}"
                info = zipfile.ZipInfo(member, FIXED_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | normalized_mode(root, path)) << 16
                info.flag_bits |= 0x800
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    verify_archive(root, output)
    return digest_file(output)


def verify_archive(root: Path, archive_path: Path) -> dict[str, object]:
    expected_paths = package_files(root)
    expected = {f"{root.name}/{path.relative_to(root).as_posix()}": path for path in expected_paths}
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("archive contains duplicate member names")
        if names != sorted(names):
            raise ValueError("archive members are not sorted")
        for info in infos:
            pure = PurePosixPath(info.filename)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != root.name:
                raise ValueError(f"unsafe archive member: {info.filename}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"archive member is not a normalized regular file: {info.filename}")
        if set(names) != set(expected):
            missing = sorted(set(expected) - set(names))
            extra = sorted(set(names) - set(expected))
            raise ValueError(f"archive exact member set mismatch: missing={missing}, extra={extra}")
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.date_time != FIXED_TIME:
                raise ValueError(f"archive member timestamp is not deterministic: {info.filename}")
            expected_mode = stat.S_IFREG | normalized_mode(root, expected[info.filename])
            if mode != expected_mode:
                raise ValueError(f"archive member mode mismatch: {info.filename}")
            payload = archive.read(info)
            if digest_bytes(payload) != digest_file(expected[info.filename]):
                raise ValueError(f"archive member content mismatch: {info.filename}")
    return {"members": len(expected), "archive_sha256": digest_file(archive_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.output:
            output = args.output.resolve()
            archive_sha = build(root, output, force=args.force)
            result = {"status": "ARCHIVE_BUILT_AND_VERIFIED", "path": str(output), "archive_sha256": archive_sha}
        else:
            archive = args.verify.resolve()
            result = {"status": "ARCHIVE_VERIFIED", "path": str(archive), **verify_archive(root, archive)}
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "ARCHIVE_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
