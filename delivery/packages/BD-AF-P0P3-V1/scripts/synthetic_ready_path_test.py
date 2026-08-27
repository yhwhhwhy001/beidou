#!/usr/bin/env python3
# ruff: noqa: S603,T201
"""Exercise signed READY, acceptance, review, dependency, forgery, and tamper paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

APPROVAL_NAMESPACE = "beidou-alpha-first-task-approval-v1"
REVIEW_NAMESPACE = "beidou-alpha-first-task-review-v1"
APPROVER = "fixture-approver@example.invalid"
REVIEWER = "fixture-reviewer@example.invalid"


def run(argv: list[str], *, cwd: Path, expected: int = 0, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout)
    if result.returncode != expected:
        raise ValueError(
            f"unexpected exit {result.returncode}, expected {expected}, command={Path(argv[0]).name}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def sign(key: Path, namespace: str, payload: Path, *, cwd: Path) -> Path:
    run(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace, str(payload)], cwd=cwd)
    return Path(str(payload) + ".sig")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--original-repo", type=Path, required=True)
    args = parser.parse_args()
    source_package = args.root.resolve()
    original_repo = args.original_repo.resolve()
    try:
        if shutil.which("ssh-keygen") is None:
            raise ValueError("ssh-keygen is required for the signed READY-path test")
        with tempfile.TemporaryDirectory() as temporary_name:
            work = Path(temporary_name)
            package = work / source_package.name
            repo = work / "approved-clean-repo"
            results = work / "results"
            approver_key = work / "approver-key"
            reviewer_key = work / "reviewer-key"
            trust_root = work / "approval-trust-root.allowed_signers"
            shutil.copytree(source_package, package)
            repo.mkdir()
            (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n*.pyc\n", encoding="utf-8")
            (repo / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
            run(["git", "init", "-q"], cwd=repo)
            run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo)
            run(["git", "config", "user.name", "Fixture Human"], cwd=repo)
            run(["git", "add", ".gitignore", "test_smoke.py"], cwd=repo)
            run(["git", "commit", "-q", "-m", "synthetic approved baseline"], cwd=repo)
            head = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()

            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(approver_key)], cwd=work)
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(reviewer_key)], cwd=work)
            approver_public = Path(str(approver_key) + ".pub").read_text(encoding="utf-8").strip()
            reviewer_public = Path(str(reviewer_key) + ".pub").read_text(encoding="utf-8").strip()
            trust_root.write_text(
                f"{APPROVER} {approver_public}\n{REVIEWER} {reviewer_public}\n", encoding="utf-8", newline="\n"
            )

            manifest_path = package / "PACKAGE-MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_snapshot"]["implementation_baseline_commit"] = head
            manifest["approval_trust_root_sha256"] = sha256_file(trust_root)
            manifest["implementation_hold_reasons"] = []
            write_json(manifest_path, manifest)
            run(
                [sys.executable, str(package / "scripts/verify_checksums.py"), "--root", str(package), "--write"],
                cwd=package,
            )
            fingerprint = sha256_file(package / "CHECKSUMS.sha256")

            def approval_for(
                task_id: str,
                approval_ids: list[str],
                dependency_hashes: dict[str, str],
                label: str,
            ) -> tuple[Path, Path]:
                path = work / f"{label}.approval.json"
                approval = {
                    "schema_version": "1.0.0",
                    "package_id": "BD-AF-P0P3-V1",
                    "package_fingerprint": fingerprint,
                    "signature_namespace": APPROVAL_NAMESPACE,
                    "task_id": task_id,
                    "baseline_commit": head,
                    "approved_worktree": str(repo),
                    "protected_source_worktree": str(original_repo),
                    "scope": "LOCAL_IMPLEMENTATION_ONLY",
                    "approval_ids": approval_ids,
                    "owner_bindings": {},
                    "dependency_result_sha256": dependency_hashes,
                    "expires_at": "2035-01-01T00:00:00+00:00",
                    "approver": APPROVER,
                }
                write_json(path, approval)
                return path, sign(approver_key, APPROVAL_NAMESPACE, path, cwd=work)

            t00_approval, t00_signature = approval_for(
                "BD-AF-P0-T00", ["H0_BASELINE_ISOLATION", "H1_TASK_START_PER_TASK"], {}, "t00"
            )
            common = [
                "--root",
                str(package),
                "--repo",
                str(repo),
                "--task",
                "BD-AF-P0-T00",
                "--approval",
                str(t00_approval),
                "--approval-signature",
                str(t00_signature),
                "--approval-trust-root",
                str(trust_root),
            ]
            preflight = run([sys.executable, str(package / "scripts/preflight_execution.py"), *common], cwd=package)
            acceptance = run(
                [
                    sys.executable,
                    str(package / "scripts/run_acceptance.py"),
                    *common,
                    "--results-dir",
                    str(results),
                    "--implementer-identity",
                    "fixture-implementer",
                ],
                cwd=package,
            )
            acceptance_payload = json.loads(acceptance.stdout)
            if acceptance_payload["status"] != "PASS" or acceptance_payload["rollback_status"] != "PASS":
                raise ValueError("synthetic acceptance or rollback did not PASS")

            review_draft = Path(acceptance_payload["reviewer_record_draft"])
            review = json.loads(review_draft.read_text(encoding="utf-8"))
            review["reviewer_identity"] = REVIEWER
            review["reviewed_at"] = "2026-08-25T12:00:00+00:00"
            review_path = work / "t00.reviewer-record.json"
            write_json(review_path, review)
            review_signature = sign(reviewer_key, REVIEW_NAMESPACE, review_path, cwd=work)
            finalized = run(
                [
                    sys.executable,
                    str(package / "scripts/finalize_task_result.py"),
                    "--root",
                    str(package),
                    "--results-dir",
                    str(results),
                    "--task",
                    "BD-AF-P0-T00",
                    "--reviewer-record",
                    str(review_path),
                    "--reviewer-signature",
                    str(review_signature),
                    "--approval-trust-root",
                    str(trust_root),
                ],
                cwd=package,
            )
            t00_result = results / "BD-AF-P0-T00.task-result.json"
            t00_hash = sha256_file(t00_result)
            t01_approval, t01_signature = approval_for(
                "BD-AF-P1-T01", ["H1_TASK_START_PER_TASK"], {"BD-AF-P0-T00": t00_hash}, "t01"
            )
            t01_common = [
                "--root",
                str(package),
                "--repo",
                str(repo),
                "--task",
                "BD-AF-P1-T01",
                "--approval",
                str(t01_approval),
                "--approval-signature",
                str(t01_signature),
                "--approval-trust-root",
                str(trust_root),
                "--dependency-results-dir",
                str(results),
            ]
            t01_preflight = run(
                [sys.executable, str(package / "scripts/preflight_execution.py"), *t01_common], cwd=package
            )

            forged_results = work / "forged-results"
            shutil.copytree(results, forged_results)
            forged_result_path = forged_results / "BD-AF-P0-T00.task-result.json"
            forged_result = json.loads(forged_result_path.read_text(encoding="utf-8"))
            forged_result["implementer_status"] = "FAIL"
            write_json(forged_result_path, forged_result)
            forged_hash = sha256_file(forged_result_path)
            forged_approval, forged_signature = approval_for(
                "BD-AF-P1-T01", ["H1_TASK_START_PER_TASK"], {"BD-AF-P0-T00": forged_hash}, "t01-forged"
            )
            forged_preflight = run(
                [
                    sys.executable,
                    str(package / "scripts/preflight_execution.py"),
                    "--root",
                    str(package),
                    "--repo",
                    str(repo),
                    "--task",
                    "BD-AF-P1-T01",
                    "--approval",
                    str(forged_approval),
                    "--approval-signature",
                    str(forged_signature),
                    "--approval-trust-root",
                    str(trust_root),
                    "--dependency-results-dir",
                    str(forged_results),
                ],
                cwd=package,
                expected=3,
            )

            tampered_results = work / "tampered-results"
            shutil.copytree(results, tampered_results)
            tampered_stdout = tampered_results / "BD-AF-P0-T00/evidence/AC-BD-AF-P0-T00-01.stdout.txt"
            tampered_stdout.write_bytes(tampered_stdout.read_bytes() + b"tamper\n")
            tampered_args = list(t01_common)
            tampered_args[-1] = str(tampered_results)
            tampered_preflight = run(
                [sys.executable, str(package / "scripts/preflight_execution.py"), *tampered_args],
                cwd=package,
                expected=3,
            )
            result = {
                "status": "SYNTHETIC_READY_PATH_PASS",
                "t00_preflight": json.loads(preflight.stdout)["status"],
                "t00_acceptance": acceptance_payload["status"],
                "t00_rollback": acceptance_payload["rollback_status"],
                "t00_finalized": json.loads(finalized.stdout)["status"],
                "t01_dependency_preflight": json.loads(t01_preflight.stdout)["status"],
                "forged_dependency_preflight": json.loads(forged_preflight.stdout)["status"],
                "tampered_evidence_preflight": json.loads(tampered_preflight.stdout)["status"],
                "evidence_manifest_count": len(acceptance_payload["evidence_manifests"]),
            }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "SYNTHETIC_READY_PATH_FAILED", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
