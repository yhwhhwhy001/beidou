#!/usr/bin/env python3
# ruff: noqa: S603,S607,T201
"""Execute one accepted task contract and emit hash-bound evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


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
        raise ValueError(f"expected object in {path}")
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], cwd=repo, text=True, capture_output=True, check=False, timeout=30
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def parse_junit(path: Path) -> dict[str, int]:
    payload = path.read_bytes()
    if len(payload) > 50 * 1024 * 1024:
        raise ValueError("JUnit evidence exceeds 50 MiB safety limit")
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("JUnit evidence contains a forbidden DTD or entity declaration")
    root = ET.fromstring(payload)  # noqa: S314 - DTD/entities rejected above; stdlib-only package.
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    leaf_suites = [suite for suite in suites if not any(child.tag == "testsuite" for child in suite)] or suites
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0}
    for suite in leaf_suites:
        for key in ("tests", "failures", "errors", "skipped"):
            totals[key] += int(suite.attrib.get(key, "0"))
        for skipped in suite.iter("skipped"):
            marker = " ".join(
                [skipped.attrib.get("type", ""), skipped.attrib.get("message", ""), skipped.text or ""]
            ).lower()
            if "xfail" in marker:
                totals["xfailed"] += 1
    return totals


def substitute(argv: list[str], mapping: dict[str, str]) -> list[str]:
    result: list[str] = []
    for token in argv:
        for key, value in mapping.items():
            token = token.replace("{" + key + "}", value)
        if "{" in token or "}" in token:
            raise ValueError(f"unresolved command placeholder: {token}")
        result.append(token)
    result[0] = sys.executable if result[0] == "python" else result[0]
    return result


def candidate_paths(argv: list[str], repo: Path) -> list[Path]:
    paths: list[Path] = []
    for token in argv[1:]:
        if token.startswith("-") or token in {"pytest"} or token.startswith("--junitxml="):
            continue
        if token.endswith(".py") or token.startswith(("tests/", "scripts/", "apps/")):
            path = Path(token)
            paths.append(path if path.is_absolute() else repo / path)
    return paths


def enforce_denied_network(argv: list[str]) -> tuple[list[str] | None, str]:
    if sys.platform == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec:
            profile = "(version 1) (allow default) (deny network*)"
            return [sandbox_exec, "-p", profile, *argv], "MACOS_SANDBOX_EXEC_DENY_NETWORK"
    return None, "UNAVAILABLE_FAIL_CLOSED"


def validate_evidence(package_root: Path, evidence_path: Path) -> None:
    command = [
        sys.executable,
        str(package_root / "scripts/validate_package.py"),
        "--validate-evidence",
        str(evidence_path),
    ]
    result = subprocess.run(command, cwd=package_root, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode != 0:
        raise ValueError(f"evidence schema validation failed: {(result.stderr or result.stdout).strip()}")


def require_external_path(path: Path, *protected_roots: Path) -> None:
    for protected in protected_roots:
        try:
            path.resolve().relative_to(protected.resolve())
        except ValueError:
            continue
        raise ValueError(f"evidence and approval paths must be outside protected root: {protected}")


def execute_criterion(
    criterion: dict[str, Any],
    *,
    task_id: str,
    package_root: Path,
    package_fingerprint: str,
    repo: Path,
    evidence_dir: Path,
    approval: Path,
    original_repo: Path,
    task_start_head: str,
    approved_head: str,
) -> tuple[str, Path]:
    criterion_id = criterion["id"]
    oracle = criterion["oracle"]
    mapping = {
        "repo": str(repo),
        "approval": str(approval),
        "evidence_dir": str(evidence_dir),
        "package_root": str(package_root),
        "original_repo": str(original_repo),
    }
    argv = substitute(oracle["argv"], mapping)
    execution_argv, network_enforcer = enforce_denied_network(argv)
    missing_command_paths = [str(path) for path in candidate_paths(argv, repo) if not path.exists()]
    stdout_path = evidence_dir / f"{criterion_id}.stdout.txt"
    stderr_path = evidence_dir / f"{criterion_id}.stderr.txt"
    started_at = now()
    timed_out = False
    if execution_argv is None:
        stdout = b""
        stderr = b"no supported operating-system network denial enforcer"
        exit_code = 126
    elif missing_command_paths:
        stdout = b""
        stderr = ("missing command paths: " + ", ".join(missing_command_paths)).encode()
        exit_code = 127
    else:
        environment = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment.pop(key, None)
        environment.update(
            {
                "NO_PROXY": "",
                "no_proxy": "",
                "BEIDOU_NETWORK_POLICY": "DENIED",
                "PYTHONDONTWRITEBYTECODE": "1",
                "BEIDOU_EVIDENCE_DIR": str(evidence_dir),
                "BEIDOU_CRITERION_ID": criterion_id,
            }
        )
        try:
            result = subprocess.run(
                execution_argv,
                cwd=repo,
                env=environment,
                capture_output=True,
                check=False,
                timeout=oracle["timeout_seconds"],
            )
            stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            exit_code = 124
    finished_at = now()
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

    failure_reasons: list[str] = []
    status = "PASS"
    if execution_argv is None:
        status = "NOT_VERIFIABLE"
        failure_reasons.append("NETWORK_DENIAL_ENFORCER_UNAVAILABLE")
    elif missing_command_paths:
        status = "NOT_VERIFIABLE"
        failure_reasons.append("ORACLE_COMMAND_PATH_MISSING")
    elif timed_out:
        status = "FAIL"
        failure_reasons.append("ORACLE_TIMEOUT")
    elif exit_code not in oracle["expected_exit_codes"]:
        status = "FAIL"
        failure_reasons.append("UNEXPECTED_EXIT_CODE")

    junit_result: dict[str, int] | None = None
    expectation = oracle["test_expectation"]
    if expectation is not None:
        junit_path = Path(substitute([expectation["junit_path"]], mapping)[0])
        if not junit_path.is_absolute():
            junit_path = repo / junit_path
        if not junit_path.is_file():
            status = "NOT_VERIFIABLE" if status == "PASS" else status
            failure_reasons.append("JUNIT_MISSING")
        else:
            try:
                junit_result = parse_junit(junit_path)
            except (ET.ParseError, OSError, ValueError):
                status = "NOT_VERIFIABLE" if status == "PASS" else status
                failure_reasons.append("JUNIT_INVALID")
            else:
                if junit_result["tests"] < expectation["minimum_collected"]:
                    status = "FAIL"
                    failure_reasons.append("TEST_COUNT_BELOW_MINIMUM")
                if junit_result["skipped"] > expectation["maximum_skipped"]:
                    status = "FAIL"
                    failure_reasons.append("SKIP_COUNT_ABOVE_MAXIMUM")
                if junit_result["xfailed"] > expectation["maximum_xfailed"]:
                    status = "FAIL"
                    failure_reasons.append("XFAIL_COUNT_ABOVE_MAXIMUM")
                if junit_result["failures"] or junit_result["errors"]:
                    status = "FAIL"
                    failure_reasons.append("JUNIT_FAILURES_OR_ERRORS")

    artifact_hashes: dict[str, str] = {
        stdout_path.name: sha256_file(stdout_path),
        stderr_path.name: sha256_file(stderr_path),
    }
    for relative in criterion["evidence"]["artifacts"]:
        artifact = evidence_dir / relative
        if artifact.is_file() and not artifact.is_symlink():
            artifact_hashes[relative] = sha256_file(artifact)
        else:
            status = "NOT_VERIFIABLE" if status == "PASS" else status
            failure_reasons.append(f"EVIDENCE_ARTIFACT_MISSING:{relative}")

    actual_head = git(repo, "rev-parse", "HEAD")
    worktree_clean = not bool(git(repo, "status", "--porcelain=v1", "--untracked-files=all"))
    if actual_head != approved_head:
        status = "FAIL"
        failure_reasons.append("HEAD_CHANGED_DURING_ORACLE")
    if not worktree_clean:
        status = "FAIL"
        failure_reasons.append("WORKTREE_DIRTY_AFTER_ORACLE")

    evidence = {
        "schema_version": "1.0.0",
        "package_id": "BD-AF-P0P3-V1",
        "package_sha256": package_fingerprint,
        "task_id": task_id,
        "criterion_id": criterion_id,
        "baseline_commit": task_start_head,
        "actual_head": actual_head,
        "worktree_clean": worktree_clean,
        "argv": argv,
        "execution_argv": execution_argv or argv,
        "network_enforcer": network_enforcer,
        "cwd": str(repo),
        "network_policy": oracle["network_policy"],
        "timeout_seconds": oracle["timeout_seconds"],
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "junit": junit_result,
        "artifact_sha256": artifact_hashes,
        "status": status,
        "failure_reason": ";".join(sorted(set(failure_reasons))) if failure_reasons else None,
    }
    evidence_path = evidence_dir / f"{criterion_id}.evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    validate_evidence(package_root, evidence_path)
    return status, evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--approval-signature", type=Path, required=True)
    parser.add_argument("--approval-trust-root", type=Path, required=True)
    parser.add_argument("--dependency-results-dir", type=Path)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--implementer-identity", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    package_root = args.root.resolve()
    repo = args.repo.resolve()
    approval = args.approval.resolve()
    approval_signature = args.approval_signature.resolve()
    approval_trust_root = args.approval_trust_root.resolve()
    results_dir = args.results_dir.resolve()
    task_bundle_dir = results_dir / args.task
    evidence_dir = task_bundle_dir / "evidence"
    try:
        if not args.implementer_identity.strip():
            raise ValueError("implementer identity must be non-empty")
        if args.dependency_results_dir and args.dependency_results_dir.resolve() != results_dir:
            raise ValueError("dependency results directory must equal results directory")
        for external_path in (results_dir, approval, approval_signature, approval_trust_root):
            require_external_path(external_path, repo, package_root)
        if task_bundle_dir.exists():
            raise ValueError(f"task result bundle already exists: {task_bundle_dir}")
        preflight = [
            sys.executable,
            str(package_root / "scripts/preflight_execution.py"),
            "--repo",
            str(repo),
            "--task",
            args.task,
            "--approval",
            str(approval),
            "--approval-signature",
            str(approval_signature),
            "--approval-trust-root",
            str(approval_trust_root),
            "--mode",
            "acceptance",
        ]
        if args.dependency_results_dir:
            preflight.extend(["--dependency-results-dir", str(args.dependency_results_dir.resolve())])
        gate = subprocess.run(preflight, cwd=package_root, text=True, capture_output=True, check=False, timeout=90)
        if gate.returncode != 0:
            print(
                json.dumps(
                    {"status": "HOLD", "task_id": args.task, "preflight": (gate.stdout or gate.stderr).strip()},
                    sort_keys=True,
                )
            )
            return 3
        preflight_payload = json.loads(gate.stdout)
        results_dir.mkdir(parents=True, exist_ok=True)
        approval_dir = task_bundle_dir / "approvals"
        evidence_dir.mkdir(parents=True, exist_ok=False)
        approval_dir.mkdir(parents=True, exist_ok=False)
        approval_copy = approval_dir / f"{args.task}.approval.json"
        approval_signature_copy = approval_dir / f"{args.task}.approval.json.sig"
        shutil.copyfile(approval, approval_copy)
        shutil.copyfile(approval_signature, approval_signature_copy)
        delivery = load_json(package_root / "delivery.yaml")
        approval_payload = load_json(approval_copy)
        original_repo = Path(approval_payload["protected_source_worktree"]).resolve()
        task = next((item for item in delivery["tasks"] if item["id"] == args.task), None)
        if task is None:
            raise ValueError(f"unknown task: {args.task}")
        acceptance = load_json(package_root / task["path"] / "ACCEPTANCE.yaml")
        package_fingerprint = sha256_file(package_root / "CHECKSUMS.sha256")
        task_start_head = preflight_payload["task_start_head"]
        approved_head = git(repo, "rev-parse", "HEAD")
        statuses: list[str] = []
        manifests: list[str] = []
        for criterion in acceptance["criteria"]:
            status, evidence_path = execute_criterion(
                criterion,
                task_id=args.task,
                package_root=package_root,
                package_fingerprint=package_fingerprint,
                repo=repo,
                evidence_dir=evidence_dir,
                approval=approval_copy,
                original_repo=original_repo,
                task_start_head=task_start_head,
                approved_head=approved_head,
            )
            statuses.append(status)
            manifests.append(str(evidence_path))
        rollback = acceptance["rollback_rehearsal"]
        rollback_criterion = {
            "id": f"RB-{args.task}",
            "oracle": {
                "phase": "POST_IMPLEMENTATION",
                "argv": rollback["command"],
                "cwd": "REPO_ROOT",
                "timeout_seconds": rollback["timeout_seconds"],
                "network_policy": rollback["network_policy"],
                "expected_exit_codes": rollback["expected_exit_codes"],
                "test_expectation": rollback["test_expectation"],
            },
            "evidence": rollback["evidence"],
            "failure_action": rollback["failure_action"],
        }
        rollback_status, rollback_evidence_path = execute_criterion(
            rollback_criterion,
            task_id=args.task,
            package_root=package_root,
            package_fingerprint=package_fingerprint,
            repo=repo,
            evidence_dir=evidence_dir,
            approval=approval_copy,
            original_repo=original_repo,
            task_start_head=task_start_head,
            approved_head=approved_head,
        )
        manifests.append(str(rollback_evidence_path))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "ACCEPTANCE_INVALID", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    overall = (
        "PASS" if statuses and all(status == "PASS" for status in statuses) and rollback_status == "PASS" else "FAIL"
    )
    task_result_draft: str | None = None
    reviewer_record_draft: str | None = None
    if overall == "PASS":
        evidence_items = []
        for manifest_path in manifests:
            path = Path(manifest_path)
            payload = load_json(path)
            evidence_items.append(
                {
                    "criterion_id": payload["criterion_id"],
                    "relative_path": path.relative_to(results_dir).as_posix(),
                    "sha256": sha256_file(path),
                    "status": "PASS",
                }
            )
        evidence_items.sort(key=lambda item: item["criterion_id"])
        changed_paths = sorted(
            line
            for line in git(repo, "diff", "--name-only", f"{task_start_head}..{approved_head}").splitlines()
            if line
        )
        task_result = {
            "schema_version": "1.0.0",
            "package_id": "BD-AF-P0P3-V1",
            "package_fingerprint": package_fingerprint,
            "task_id": args.task,
            "baseline_commit": task_start_head,
            "actual_head": approved_head,
            "dependency_result_sha256": preflight_payload["dependency_result_sha256"],
            "approval_relative_path": approval_copy.relative_to(results_dir).as_posix(),
            "approval_sha256": sha256_file(approval_copy),
            "approval_signature_relative_path": approval_signature_copy.relative_to(results_dir).as_posix(),
            "approval_signature_sha256": sha256_file(approval_signature_copy),
            "evidence_manifests": evidence_items,
            "changed_paths": changed_paths,
            "implementer_identity": args.implementer_identity,
            "implementer_status": "PASS",
            "reviewer_status": None,
            "reviewer_identity": None,
            "reviewed_at": None,
            "reviewer_record_relative_path": None,
            "reviewer_record_sha256": None,
            "reviewer_signature_relative_path": None,
            "reviewer_signature_sha256": None,
            "residual_risks": [],
        }
        task_result_path = results_dir / f"{args.task}.task-result.draft.json"
        task_result_path.write_text(
            json.dumps(task_result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        validation = subprocess.run(
            [
                sys.executable,
                str(package_root / "scripts/validate_package.py"),
                "--validate-task-result",
                str(task_result_path),
            ],
            cwd=package_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if validation.returncode != 0:
            print(
                json.dumps(
                    {"status": "ACCEPTANCE_INVALID", "error": "generated task-result draft failed validation"},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 2
        reviewer_record = {
            "schema_version": "1.0.0",
            "package_id": "BD-AF-P0P3-V1",
            "package_fingerprint": package_fingerprint,
            "signature_namespace": "beidou-alpha-first-task-review-v1",
            "task_id": args.task,
            "baseline_commit": task_start_head,
            "actual_head": approved_head,
            "dependency_result_sha256": preflight_payload["dependency_result_sha256"],
            "approval_sha256": sha256_file(approval_copy),
            "approval_signature_sha256": sha256_file(approval_signature_copy),
            "evidence_manifest_sha256": {item["criterion_id"]: item["sha256"] for item in evidence_items},
            "changed_paths": changed_paths,
            "implementer_identity": args.implementer_identity,
            "implementer_status": "PASS",
            "decision": "ACCEPTED",
            "reviewer_identity": "REPLACE_WITH_ALLOWED_SIGNERS_IDENTITY",
            "reviewed_at": "REPLACE_WITH_RFC3339_TIMESTAMP",
            "residual_risks": [],
        }
        reviewer_record_path = task_bundle_dir / f"{args.task}.reviewer-record.draft.json"
        reviewer_record_path.write_text(
            json.dumps(reviewer_record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        task_result_draft = str(task_result_path)
        reviewer_record_draft = str(reviewer_record_path)
    print(
        json.dumps(
            {
                "status": overall,
                "task_id": args.task,
                "criterion_statuses": statuses,
                "rollback_status": rollback_status,
                "evidence_manifests": manifests,
                "task_result_draft": task_result_draft,
                "reviewer_record_draft": reviewer_record_draft,
            },
            sort_keys=True,
        )
    )
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
