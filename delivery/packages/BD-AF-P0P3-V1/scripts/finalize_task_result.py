#!/usr/bin/env python3
# ruff: noqa: S603,T201
"""Assemble a final task result from a human-signed independent review record."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REVIEW_NAMESPACE = "beidou-alpha-first-task-review-v1"


def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout)


def validate_document(package_root: Path, option: str, path: Path) -> None:
    result = run(
        [sys.executable, str(package_root / "scripts/validate_package.py"), option, str(path)], cwd=package_root
    )
    if result.returncode != 0:
        raise ValueError(f"{option} failed for {path.name}")


def verify_signature(payload: Path, signature: Path, trust_root: Path, identity: str) -> bool:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        return False
    result = subprocess.run(
        [
            ssh_keygen,
            "-Y",
            "verify",
            "-f",
            str(trust_root),
            "-I",
            identity,
            "-n",
            REVIEW_NAMESPACE,
            "-s",
            str(signature),
        ],
        cwd=payload.parent,
        input=payload.read_bytes(),
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def require_regular(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--reviewer-record", type=Path, required=True)
    parser.add_argument("--reviewer-signature", type=Path, required=True)
    parser.add_argument("--approval-trust-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    package_root = args.root.resolve()
    results_dir = args.results_dir.resolve()
    review_source = args.reviewer_record.resolve()
    signature_source = args.reviewer_signature.resolve()
    trust_root = args.approval_trust_root.resolve()
    try:
        for script, extra in (("verify_checksums.py", []), ("validate_package.py", ["--negative-fixtures"])):
            result = run([sys.executable, str(package_root / "scripts" / script), *extra], cwd=package_root)
            if result.returncode != 0:
                raise ValueError(f"package gate {script} failed")
        if not results_dir.is_dir() or results_dir.is_symlink():
            raise ValueError("results directory must be an existing non-symlink directory")
        for path, label in (
            (review_source, "reviewer record"),
            (signature_source, "reviewer signature"),
            (trust_root, "approval trust root"),
        ):
            require_regular(path, label)
        try:
            results_dir.relative_to(package_root)
        except ValueError:
            pass
        else:
            raise ValueError("results directory must be outside the package")
        manifest = load_json(package_root / "PACKAGE-MANIFEST.json")
        bound_trust_hash = manifest["approval_trust_root_sha256"]
        if bound_trust_hash is None:
            print(json.dumps({"status": "FINALIZE_HOLD", "reason": "APPROVAL_TRUST_ROOT_UNSET"}, sort_keys=True))
            return 3
        if sha256_file(trust_root) != bound_trust_hash:
            print(
                json.dumps({"status": "FINALIZE_HOLD", "reason": "APPROVAL_TRUST_ROOT_HASH_MISMATCH"}, sort_keys=True)
            )
            return 3
        draft_path = results_dir / f"{args.task}.task-result.draft.json"
        require_regular(draft_path, "task-result draft")
        validate_document(package_root, "--validate-task-result", draft_path)
        validate_document(package_root, "--validate-reviewer-record", review_source)
        draft = load_json(draft_path)
        review = load_json(review_source)
        evidence_hashes = {item["criterion_id"]: item["sha256"] for item in draft["evidence_manifests"]}
        expected_review = {
            "schema_version": "1.0.0",
            "package_id": draft["package_id"],
            "package_fingerprint": draft["package_fingerprint"],
            "signature_namespace": REVIEW_NAMESPACE,
            "task_id": draft["task_id"],
            "baseline_commit": draft["baseline_commit"],
            "actual_head": draft["actual_head"],
            "dependency_result_sha256": draft["dependency_result_sha256"],
            "approval_sha256": draft["approval_sha256"],
            "approval_signature_sha256": draft["approval_signature_sha256"],
            "evidence_manifest_sha256": evidence_hashes,
            "changed_paths": draft["changed_paths"],
            "implementer_identity": draft["implementer_identity"],
            "implementer_status": "PASS",
            "decision": "ACCEPTED",
            "reviewer_identity": review["reviewer_identity"],
            "reviewed_at": review["reviewed_at"],
            "residual_risks": review["residual_risks"],
        }
        if review != expected_review:
            raise ValueError("signed reviewer record does not exactly bind the task-result draft")
        if not verify_signature(review_source, signature_source, trust_root, review["reviewer_identity"]):
            print(json.dumps({"status": "FINALIZE_HOLD", "reason": "REVIEW_SIGNATURE_INVALID"}, sort_keys=True))
            return 3

        review_dir = results_dir / args.task / "reviews"
        if review_dir.exists():
            raise ValueError("review custody directory already exists")
        final_path = results_dir / f"{args.task}.task-result.json"
        if final_path.exists():
            raise ValueError("final task result already exists")
        review_dir.mkdir(parents=True, exist_ok=False)
        review_copy = review_dir / f"{args.task}.reviewer-record.json"
        signature_copy = review_dir / f"{args.task}.reviewer-record.json.sig"
        review_copy.write_bytes(review_source.read_bytes())
        signature_copy.write_bytes(signature_source.read_bytes())
        final = dict(draft)
        final.update(
            {
                "reviewer_status": "ACCEPTED",
                "reviewer_identity": review["reviewer_identity"],
                "reviewed_at": review["reviewed_at"],
                "reviewer_record_relative_path": review_copy.relative_to(results_dir).as_posix(),
                "reviewer_record_sha256": sha256_file(review_copy),
                "reviewer_signature_relative_path": signature_copy.relative_to(results_dir).as_posix(),
                "reviewer_signature_sha256": sha256_file(signature_copy),
                "residual_risks": review["residual_risks"],
            }
        )
        temporary = final_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        validate_document(package_root, "--validate-task-result", temporary)
        temporary.replace(final_path)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "FINALIZE_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "TASK_RESULT_FINALIZED",
                "task_id": args.task,
                "path": str(final_path),
                "sha256": sha256_file(final_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
