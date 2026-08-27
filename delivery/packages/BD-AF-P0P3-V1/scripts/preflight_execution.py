#!/usr/bin/env python3
# ruff: noqa: S603,T201
"""Fail-closed, read-only task preflight with signed approval and result custody."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

APPROVAL_NAMESPACE = "beidou-alpha-first-task-approval-v1"
REVIEW_NAMESPACE = "beidou-alpha-first-task-review-v1"
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
NON_HUMAN_IDENTITY = re.compile(r"\b(agent|codex|openai|gpt)\b", re.IGNORECASE)


def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def run(command: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout)


def gate_package(package_root: Path) -> None:
    for script, extra in (("verify_checksums.py", []), ("validate_package.py", ["--negative-fixtures"])):
        result = run([sys.executable, str(package_root / "scripts" / script), *extra], cwd=package_root, timeout=60)
        if result.returncode != 0:
            raise ValueError(f"package gate {script} failed: {(result.stderr or result.stdout).strip()}")


def validate_document(package_root: Path, option: str, path: Path) -> bool:
    result = run(
        [sys.executable, str(package_root / "scripts/validate_package.py"), option, str(path)],
        cwd=package_root,
    )
    return result.returncode == 0


def git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", str(repo), *args], cwd=repo)
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed without exposing repository content")
    return result.stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    return run(["git", "-C", str(repo), *args], cwd=repo).returncode == 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_expiry(value: str) -> dt.datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("approval expires_at must include a timezone")
    return parsed


def require_external_regular_file(path: Path, *protected_roots: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    resolved = path.resolve()
    for protected in protected_roots:
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        return False
    return True


def safe_result_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"unsafe result-relative path: {relative}")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("dependency result root must be a non-symlink directory")
    cursor = root
    for part in candidate.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlink is forbidden in result custody: {relative}")
    resolved = (root / candidate).resolve(strict=True)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"result-relative path escapes custody root: {relative}") from exc
    if not resolved.is_file():
        raise ValueError(f"result-relative path is not a regular file: {relative}")
    return resolved


def allowed_path(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or (prefix.endswith("/") and path.startswith(prefix)) for prefix in prefixes)


def verify_ssh_signature(
    payload_path: Path,
    signature_path: Path,
    trust_root: Path,
    identity: str,
    namespace: str,
) -> bool:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        return False
    try:
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
                namespace,
                "-s",
                str(signature_path),
            ],
            cwd=payload_path.parent,
            input=payload_path.read_bytes(),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def validate_approval_semantics(
    approval: dict[str, Any],
    *,
    manifest: dict[str, Any],
    acceptance: dict[str, Any],
    task_id: str,
    repo: Path,
    task_start_head: str,
    package_fingerprint: str,
    dependency_hashes: dict[str, str],
) -> list[str]:
    reasons: list[str] = []
    expected_pairs = {
        "schema_version": "1.0.0",
        "package_id": manifest["package_id"],
        "package_fingerprint": package_fingerprint,
        "signature_namespace": APPROVAL_NAMESPACE,
        "task_id": task_id,
        "baseline_commit": task_start_head,
        "scope": "LOCAL_IMPLEMENTATION_ONLY",
    }
    for field, expected in expected_pairs.items():
        if approval.get(field) != expected:
            reasons.append(f"APPROVAL_{field.upper()}_MISMATCH")
    try:
        approved_worktree = Path(approval["approved_worktree"])
        if not approved_worktree.is_absolute() or approved_worktree.resolve() != repo.resolve():
            reasons.append("APPROVAL_WORKTREE_MISMATCH")
    except (KeyError, TypeError, OSError):
        reasons.append("APPROVAL_WORKTREE_INVALID")
    try:
        protected_source = Path(approval["protected_source_worktree"])
        manifest_source = Path(manifest["source_snapshot"]["repository"])
        if not protected_source.is_absolute() or protected_source.resolve() != manifest_source.resolve():
            reasons.append("APPROVAL_PROTECTED_SOURCE_MISMATCH")
    except (KeyError, TypeError, OSError):
        reasons.append("APPROVAL_PROTECTED_SOURCE_INVALID")
    required_ids = set(acceptance["preflight"]["approval_ids"])
    actual_ids = approval.get("approval_ids")
    if not isinstance(actual_ids, list) or set(actual_ids) != required_ids:
        reasons.append("APPROVAL_IDS_MISMATCH")
    owners = approval.get("owner_bindings")
    if not isinstance(owners, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
        for key, value in owners.items()
    ):
        reasons.append("OWNER_BINDINGS_INVALID")
        owners = {}
    if "H2_METRIC_OWNER_BEFORE_BD_AF_P3_T07" in required_ids and not owners.get("METRIC_OWNER"):
        reasons.append("METRIC_OWNER_UNBOUND")
    if "H3_PORTFOLIO_OWNER_BEFORE_BD_AF_P3_T08" in required_ids and not owners.get("PORTFOLIO_OWNER"):
        reasons.append("PORTFOLIO_OWNER_UNBOUND")
    approver = approval.get("approver")
    if not isinstance(approver, str) or not approver.strip():
        reasons.append("APPROVER_MISSING")
    elif NON_HUMAN_IDENTITY.search(approver):
        reasons.append("AGENT_SELF_APPROVAL_FORBIDDEN")
    if approval.get("dependency_result_sha256") != dependency_hashes:
        reasons.append("APPROVAL_DEPENDENCY_HASHES_MISMATCH")
    try:
        if parse_expiry(approval["expires_at"]) <= dt.datetime.now(dt.timezone.utc):
            reasons.append("APPROVAL_EXPIRED")
    except (KeyError, TypeError, ValueError):
        reasons.append("APPROVAL_EXPIRY_INVALID")
    return reasons


def expected_criterion_ids(acceptance: dict[str, Any], task_id: str) -> set[str]:
    return {item["id"] for item in acceptance["criteria"]} | {f"RB-{task_id}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-signature", type=Path)
    parser.add_argument("--approval-trust-root", type=Path)
    parser.add_argument("--dependency-results-dir", type=Path)
    parser.add_argument("--mode", choices=("start", "acceptance"), default="start")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    package_root = args.root.resolve()
    repo = args.repo.resolve()
    reasons: list[str] = []
    try:
        gate_package(package_root)
        manifest = load_json(package_root / "PACKAGE-MANIFEST.json")
        delivery = load_json(package_root / "delivery.yaml")
        tasks = {task["id"]: task for task in delivery["tasks"]}
        if args.task not in tasks:
            raise ValueError(f"unknown task: {args.task}")
        task = tasks[args.task]
        acceptances = {
            item["id"]: load_json(package_root / item["path"] / "ACCEPTANCE.yaml") for item in delivery["tasks"]
        }
        acceptance = acceptances[args.task]
        package_fingerprint = sha256_file(package_root / "CHECKSUMS.sha256")

        if git(repo, "rev-parse", "--is-inside-work-tree") != "true":
            reasons.append("REPOSITORY_NOT_GIT_WORKTREE")
        head = git(repo, "rev-parse", "HEAD")
        dirty_lines = [
            line for line in git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines() if line
        ]
        if dirty_lines:
            reasons.append(f"WORKTREE_NOT_CLEAN count={len(dirty_lines)}")

        root_baseline = manifest["source_snapshot"]["implementation_baseline_commit"]
        if root_baseline is None:
            reasons.append("IMPLEMENTATION_BASELINE_COMMIT_UNSET")
        elif HEX_40.fullmatch(root_baseline) is None:
            raise ValueError("manifest implementation baseline is not 40-hex")
        elif not git_ok(repo, "cat-file", "-e", f"{root_baseline}^{{commit}}"):
            reasons.append("IMPLEMENTATION_BASELINE_COMMIT_UNAVAILABLE")

        trust_root: Path | None = None
        bound_trust_hash = manifest.get("approval_trust_root_sha256")
        if bound_trust_hash is None:
            reasons.append("APPROVAL_TRUST_ROOT_UNSET")
        elif not isinstance(bound_trust_hash, str) or HEX_64.fullmatch(bound_trust_hash) is None:
            raise ValueError("manifest approval trust root hash is invalid")
        elif args.approval_trust_root is None:
            reasons.append("APPROVAL_TRUST_ROOT_MISSING")
        else:
            candidate = args.approval_trust_root.resolve()
            if not require_external_regular_file(candidate, repo, package_root):
                reasons.append("APPROVAL_TRUST_ROOT_NOT_EXTERNAL_REGULAR_FILE")
            elif sha256_file(candidate) != bound_trust_hash:
                reasons.append("APPROVAL_TRUST_ROOT_HASH_MISMATCH")
            elif shutil.which("ssh-keygen") is None:
                reasons.append("SSH_SIGNATURE_VERIFIER_UNAVAILABLE")
            else:
                trust_root = candidate

        result_root: Path | None = None
        if args.dependency_results_dir is not None:
            candidate = args.dependency_results_dir.resolve()
            if candidate.is_dir() and not candidate.is_symlink():
                outside_repo = False
                outside_package = False
                try:
                    candidate.relative_to(repo)
                except ValueError:
                    outside_repo = True
                try:
                    candidate.relative_to(package_root)
                except ValueError:
                    outside_package = True
                if outside_repo and outside_package:
                    result_root = candidate
            if result_root is None:
                reasons.append("DEPENDENCY_RESULTS_DIR_NOT_EXTERNAL_REGULAR_DIRECTORY")

        verified_results: dict[str, dict[str, Any]] = {}
        verified_result_hashes: dict[str, str] = {}
        verifying: set[str] = set()

        def verify_dependency_result(task_id: str) -> dict[str, Any] | None:
            if task_id in verified_results:
                return verified_results[task_id]
            if task_id in verifying:
                reasons.append(f"DEPENDENCY_RESULT_CYCLE {task_id}")
                return None
            if result_root is None:
                return None
            verifying.add(task_id)
            dependency_task = tasks[task_id]
            direct_dependency_results: dict[str, dict[str, Any]] = {}
            for dependency in dependency_task["dependencies"]:
                verified = verify_dependency_result(dependency)
                if verified is not None:
                    direct_dependency_results[dependency] = verified
            expected_dependency_hashes = {
                dependency: verified_result_hashes[dependency] for dependency in direct_dependency_results
            }
            if set(direct_dependency_results) != set(dependency_task["dependencies"]):
                reasons.append(f"DEPENDENCY_CHAIN_INCOMPLETE {task_id}")
            if dependency_task["dependencies"]:
                dependency_heads = {item["actual_head"] for item in direct_dependency_results.values()}
                if len(dependency_heads) != 1:
                    reasons.append(f"DEPENDENCY_HEADS_DIVERGE {task_id}")
                    expected_start = head
                else:
                    expected_start = next(iter(dependency_heads))
            else:
                expected_start = root_baseline if isinstance(root_baseline, str) else head

            try:
                result_path = safe_result_file(result_root, f"{task_id}.task-result.json")
            except (OSError, ValueError):
                reasons.append(f"DEPENDENCY_RESULT_MISSING_OR_UNSAFE {task_id}")
                verifying.remove(task_id)
                return None
            if not validate_document(package_root, "--validate-task-result", result_path):
                reasons.append(f"DEPENDENCY_RESULT_SCHEMA_INVALID {task_id}")
                verifying.remove(task_id)
                return None
            result = load_json(result_path)
            result_hash = sha256_file(result_path)
            identity_pairs = {
                "package_id": manifest["package_id"],
                "package_fingerprint": package_fingerprint,
                "task_id": task_id,
                "baseline_commit": expected_start,
            }
            for field, expected in identity_pairs.items():
                if result.get(field) != expected:
                    reasons.append(f"DEPENDENCY_{field.upper()}_MISMATCH {task_id}")
            if result.get("dependency_result_sha256") != expected_dependency_hashes:
                reasons.append(f"DEPENDENCY_TRANSITIVE_HASHES_MISMATCH {task_id}")
            if result.get("implementer_status") != "PASS":
                reasons.append(f"DEPENDENCY_IMPLEMENTER_NOT_PASS {task_id}")
            if result.get("reviewer_status") != "ACCEPTED":
                reasons.append(f"DEPENDENCY_NOT_ACCEPTED {task_id}")
            implementer = result.get("implementer_identity")
            reviewer = result.get("reviewer_identity")
            if not isinstance(implementer, str) or not implementer.strip():
                reasons.append(f"DEPENDENCY_IMPLEMENTER_IDENTITY_INVALID {task_id}")
            if not isinstance(reviewer, str) or not reviewer.strip() or NON_HUMAN_IDENTITY.search(reviewer):
                reasons.append(f"DEPENDENCY_REVIEWER_IDENTITY_INVALID {task_id}")
            if reviewer == implementer:
                reasons.append(f"DEPENDENCY_REVIEWER_NOT_INDEPENDENT {task_id}")
            if any(item.get("severity") in {"P0", "P1"} for item in result.get("residual_risks", [])):
                reasons.append(f"DEPENDENCY_HIGH_RESIDUAL_RISK {task_id}")

            actual_head = result.get("actual_head")
            if not isinstance(actual_head, str) or HEX_40.fullmatch(actual_head) is None:
                reasons.append(f"DEPENDENCY_ACTUAL_HEAD_INVALID {task_id}")
            elif not git_ok(repo, "cat-file", "-e", f"{actual_head}^{{commit}}"):
                reasons.append(f"DEPENDENCY_ACTUAL_HEAD_UNAVAILABLE {task_id}")
            elif not git_ok(repo, "merge-base", "--is-ancestor", expected_start, actual_head):
                reasons.append(f"DEPENDENCY_HEAD_NOT_DESCENDANT {task_id}")
            else:
                actual_paths = sorted(
                    line
                    for line in git(repo, "diff", "--name-only", f"{expected_start}..{actual_head}").splitlines()
                    if line
                )
                if result.get("changed_paths") != actual_paths:
                    reasons.append(f"DEPENDENCY_CHANGED_PATHS_MISMATCH {task_id}")
                if any(not allowed_path(path, dependency_task["allowed_path_prefixes"]) for path in actual_paths):
                    reasons.append(f"DEPENDENCY_CHANGED_PATHS_OUTSIDE_SCOPE {task_id}")
                if not actual_paths and task_id != "BD-AF-P0-T00":
                    reasons.append(f"DEPENDENCY_CHANGESET_EMPTY {task_id}")

            approval_path: Path | None = None
            approval_signature_path: Path | None = None
            try:
                approval_path = safe_result_file(result_root, result["approval_relative_path"])
                approval_signature_path = safe_result_file(result_root, result["approval_signature_relative_path"])
            except (KeyError, OSError, ValueError):
                reasons.append(f"DEPENDENCY_APPROVAL_CUSTODY_INVALID {task_id}")
            if approval_path is not None and approval_signature_path is not None:
                if sha256_file(approval_path) != result.get("approval_sha256"):
                    reasons.append(f"DEPENDENCY_APPROVAL_HASH_MISMATCH {task_id}")
                if sha256_file(approval_signature_path) != result.get("approval_signature_sha256"):
                    reasons.append(f"DEPENDENCY_APPROVAL_SIGNATURE_HASH_MISMATCH {task_id}")
                if not validate_document(package_root, "--validate-approval", approval_path):
                    reasons.append(f"DEPENDENCY_APPROVAL_SCHEMA_INVALID {task_id}")
                else:
                    dependency_approval = load_json(approval_path)
                    reasons.extend(
                        f"{reason} {task_id}"
                        for reason in validate_approval_semantics(
                            dependency_approval,
                            manifest=manifest,
                            acceptance=acceptances[task_id],
                            task_id=task_id,
                            repo=repo,
                            task_start_head=expected_start,
                            package_fingerprint=package_fingerprint,
                            dependency_hashes=expected_dependency_hashes,
                        )
                    )
                    approver = dependency_approval.get("approver")
                    if (
                        trust_root is None
                        or not isinstance(approver, str)
                        or not verify_ssh_signature(
                            approval_path,
                            approval_signature_path,
                            trust_root,
                            approver,
                            APPROVAL_NAMESPACE,
                        )
                    ):
                        reasons.append(f"DEPENDENCY_APPROVAL_SIGNATURE_INVALID {task_id}")

            evidence_items = result.get("evidence_manifests", [])
            expected_ids = expected_criterion_ids(acceptances[task_id], task_id)
            actual_ids = [item.get("criterion_id") for item in evidence_items if isinstance(item, dict)]
            if set(actual_ids) != expected_ids or len(actual_ids) != len(set(actual_ids)):
                reasons.append(f"DEPENDENCY_EVIDENCE_SET_MISMATCH {task_id}")
            evidence_hashes: dict[str, str] = {}
            for item in evidence_items:
                criterion_id = item.get("criterion_id", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
                try:
                    evidence_path = safe_result_file(result_root, item["relative_path"])
                except (KeyError, OSError, TypeError, ValueError):
                    reasons.append(f"DEPENDENCY_EVIDENCE_CUSTODY_INVALID {task_id} {criterion_id}")
                    continue
                evidence_hash = sha256_file(evidence_path)
                evidence_hashes[criterion_id] = evidence_hash
                if evidence_hash != item.get("sha256") or item.get("status") != "PASS":
                    reasons.append(f"DEPENDENCY_EVIDENCE_HASH_OR_STATUS_INVALID {task_id} {criterion_id}")
                if not validate_document(package_root, "--validate-evidence", evidence_path):
                    reasons.append(f"DEPENDENCY_EVIDENCE_SCHEMA_INVALID {task_id} {criterion_id}")
                    continue
                evidence = load_json(evidence_path)
                expected_evidence = {
                    "package_id": manifest["package_id"],
                    "package_sha256": package_fingerprint,
                    "task_id": task_id,
                    "criterion_id": criterion_id,
                    "baseline_commit": expected_start,
                    "actual_head": actual_head,
                    "worktree_clean": True,
                    "status": "PASS",
                    "cwd": str(repo),
                    "network_policy": "DENIED",
                }
                if any(evidence.get(field) != expected for field, expected in expected_evidence.items()):
                    reasons.append(f"DEPENDENCY_EVIDENCE_IDENTITY_MISMATCH {task_id} {criterion_id}")
                if evidence.get("network_enforcer") == "UNAVAILABLE_FAIL_CLOSED":
                    reasons.append(f"DEPENDENCY_EVIDENCE_NETWORK_UNVERIFIED {task_id} {criterion_id}")
                artifact_hashes = evidence.get("artifact_sha256")
                if not isinstance(artifact_hashes, dict):
                    reasons.append(f"DEPENDENCY_EVIDENCE_ARTIFACT_MAP_INVALID {task_id} {criterion_id}")
                    continue
                evidence_relative = Path(item["relative_path"])
                for artifact_relative, expected_hash in artifact_hashes.items():
                    combined = (evidence_relative.parent / artifact_relative).as_posix()
                    try:
                        artifact_path = safe_result_file(result_root, combined)
                    except (OSError, TypeError, ValueError):
                        reasons.append(f"DEPENDENCY_EVIDENCE_ARTIFACT_MISSING {task_id} {criterion_id}")
                        continue
                    if not isinstance(expected_hash, str) or sha256_file(artifact_path) != expected_hash:
                        reasons.append(f"DEPENDENCY_EVIDENCE_ARTIFACT_HASH_MISMATCH {task_id} {criterion_id}")
                stdout_name = f"{criterion_id}.stdout.txt"
                stderr_name = f"{criterion_id}.stderr.txt"
                if evidence.get("stdout_sha256") != artifact_hashes.get(stdout_name):
                    reasons.append(f"DEPENDENCY_STDOUT_HASH_MISMATCH {task_id} {criterion_id}")
                if evidence.get("stderr_sha256") != artifact_hashes.get(stderr_name):
                    reasons.append(f"DEPENDENCY_STDERR_HASH_MISMATCH {task_id} {criterion_id}")

            reviewer_record_path: Path | None = None
            reviewer_signature_path: Path | None = None
            try:
                reviewer_record_path = safe_result_file(result_root, result["reviewer_record_relative_path"])
                reviewer_signature_path = safe_result_file(result_root, result["reviewer_signature_relative_path"])
            except (KeyError, OSError, TypeError, ValueError):
                reasons.append(f"DEPENDENCY_REVIEW_CUSTODY_INVALID {task_id}")
            if reviewer_record_path is not None and reviewer_signature_path is not None:
                if sha256_file(reviewer_record_path) != result.get("reviewer_record_sha256"):
                    reasons.append(f"DEPENDENCY_REVIEW_RECORD_HASH_MISMATCH {task_id}")
                if sha256_file(reviewer_signature_path) != result.get("reviewer_signature_sha256"):
                    reasons.append(f"DEPENDENCY_REVIEW_SIGNATURE_HASH_MISMATCH {task_id}")
                if not validate_document(package_root, "--validate-reviewer-record", reviewer_record_path):
                    reasons.append(f"DEPENDENCY_REVIEW_RECORD_SCHEMA_INVALID {task_id}")
                else:
                    review = load_json(reviewer_record_path)
                    expected_review = {
                        "schema_version": "1.0.0",
                        "package_id": manifest["package_id"],
                        "package_fingerprint": package_fingerprint,
                        "signature_namespace": REVIEW_NAMESPACE,
                        "task_id": task_id,
                        "baseline_commit": expected_start,
                        "actual_head": actual_head,
                        "dependency_result_sha256": expected_dependency_hashes,
                        "approval_sha256": result.get("approval_sha256"),
                        "approval_signature_sha256": result.get("approval_signature_sha256"),
                        "evidence_manifest_sha256": evidence_hashes,
                        "changed_paths": result.get("changed_paths"),
                        "implementer_identity": implementer,
                        "implementer_status": "PASS",
                        "decision": "ACCEPTED",
                        "reviewer_identity": reviewer,
                        "reviewed_at": result.get("reviewed_at"),
                        "residual_risks": result.get("residual_risks"),
                    }
                    if review != expected_review:
                        reasons.append(f"DEPENDENCY_REVIEW_RECORD_CONTENT_MISMATCH {task_id}")
                    if (
                        trust_root is None
                        or not isinstance(reviewer, str)
                        or not verify_ssh_signature(
                            reviewer_record_path,
                            reviewer_signature_path,
                            trust_root,
                            reviewer,
                            REVIEW_NAMESPACE,
                        )
                    ):
                        reasons.append(f"DEPENDENCY_REVIEW_SIGNATURE_INVALID {task_id}")

            verifying.remove(task_id)
            verified_results[task_id] = result
            verified_result_hashes[task_id] = result_hash
            return result

        direct_results: dict[str, dict[str, Any]] = {}
        if task["dependencies"]:
            if result_root is None:
                reasons.append("DEPENDENCY_RESULTS_DIR_MISSING")
            else:
                for dependency in task["dependencies"]:
                    result = verify_dependency_result(dependency)
                    if result is not None:
                        direct_results[dependency] = result
        direct_dependency_hashes = {dependency: verified_result_hashes[dependency] for dependency in direct_results}
        if set(direct_results) != set(task["dependencies"]):
            reasons.append("DIRECT_DEPENDENCY_RESULTS_INCOMPLETE")
        if task["dependencies"]:
            dependency_heads = {result["actual_head"] for result in direct_results.values()}
            if len(dependency_heads) == 1:
                task_start_head = next(iter(dependency_heads))
            else:
                reasons.append("DEPENDENCY_HEADS_DIVERGE")
                task_start_head = head
        else:
            task_start_head = root_baseline if isinstance(root_baseline, str) else head

        if args.mode == "start":
            if head != task_start_head:
                reasons.append("TASK_START_HEAD_MISMATCH")
        else:
            if not git_ok(repo, "merge-base", "--is-ancestor", task_start_head, head):
                reasons.append("IMPLEMENTATION_HEAD_NOT_DESCENDANT_OF_TASK_START")
            changed_paths = sorted(
                line for line in git(repo, "diff", "--name-only", f"{task_start_head}..{head}").splitlines() if line
            )
            outside = [path for path in changed_paths if not allowed_path(path, task["allowed_path_prefixes"])]
            if outside:
                reasons.append(f"CHANGED_PATHS_OUTSIDE_TASK_SCOPE count={len(outside)}")
            if not changed_paths and task["id"] != "BD-AF-P0-T00":
                reasons.append("NO_IMPLEMENTATION_CHANGESET")

        approval_path: Path | None = None
        approval_signature_path: Path | None = None
        if args.approval is None:
            reasons.append("HUMAN_APPROVAL_ENVELOPE_MISSING")
        else:
            candidate = args.approval.resolve()
            if not require_external_regular_file(candidate, repo, package_root):
                reasons.append("HUMAN_APPROVAL_ENVELOPE_NOT_EXTERNAL_REGULAR_FILE")
            else:
                approval_path = candidate
        if args.approval_signature is None:
            reasons.append("HUMAN_APPROVAL_SIGNATURE_MISSING")
        else:
            candidate = args.approval_signature.resolve()
            if not require_external_regular_file(candidate, repo, package_root):
                reasons.append("HUMAN_APPROVAL_SIGNATURE_NOT_EXTERNAL_REGULAR_FILE")
            else:
                approval_signature_path = candidate
        if approval_path is not None:
            if not validate_document(package_root, "--validate-approval", approval_path):
                reasons.append("HUMAN_APPROVAL_ENVELOPE_SCHEMA_INVALID")
            else:
                approval = load_json(approval_path)
                reasons.extend(
                    validate_approval_semantics(
                        approval,
                        manifest=manifest,
                        acceptance=acceptance,
                        task_id=args.task,
                        repo=repo,
                        task_start_head=task_start_head,
                        package_fingerprint=package_fingerprint,
                        dependency_hashes=direct_dependency_hashes,
                    )
                )
                approver = approval.get("approver")
                if (
                    approval_signature_path is not None
                    and trust_root is not None
                    and isinstance(approver, str)
                    and not verify_ssh_signature(
                        approval_path,
                        approval_signature_path,
                        trust_root,
                        approver,
                        APPROVAL_NAMESPACE,
                    )
                ):
                    reasons.append("HUMAN_APPROVAL_SIGNATURE_INVALID")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "PREFLIGHT_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    if reasons:
        print(json.dumps({"status": "HOLD", "task_id": args.task, "reasons": sorted(set(reasons))}, sort_keys=True))
        return 3
    print(
        json.dumps(
            {
                "status": "READY",
                "mode": args.mode,
                "task_id": args.task,
                "task_start_head": task_start_head,
                "head": head,
                "package_fingerprint": package_fingerprint,
                "dependency_result_sha256": direct_dependency_hashes,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
