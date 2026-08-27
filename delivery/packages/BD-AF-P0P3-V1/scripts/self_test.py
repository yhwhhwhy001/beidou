#!/usr/bin/env python3
# ruff: noqa: S603,T201
"""Run isolated positive and negative tests for package tooling."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def run(command: list[str], *, cwd: Path, expected: set[int]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=120)
    if result.returncode not in expected:
        raise ValueError(
            f"unexpected exit {result.returncode} for {command}: {(result.stderr or result.stdout).strip()}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--repo", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    scripts = root / "scripts"
    checks: list[str] = []
    try:
        run([sys.executable, str(scripts / "verify_checksums.py")], cwd=root, expected={0})
        checks.append("checksums_positive")
        run([sys.executable, str(scripts / "validate_package.py"), "--negative-fixtures"], cwd=root, expected={0})
        checks.append("schema_semantic_negative_fixtures")
        if sys.platform == "darwin":
            sandbox_exec = shutil.which("sandbox-exec")
            if not sandbox_exec:
                raise ValueError("macOS network denial enforcer is unavailable")
            network_probe = run(
                [
                    sandbox_exec,
                    "-p",
                    "(version 1) (allow default) (deny network*)",
                    sys.executable,
                    "-c",
                    "import socket; socket.socket().connect(('127.0.0.1', 1))",
                ],
                cwd=root,
                expected={1},
            )
            if "Operation not permitted" not in network_probe.stderr and "PermissionError" not in network_probe.stderr:
                raise ValueError("network probe failed for a reason other than enforced denial")
            checks.append("os_network_denial_enforced")

        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            tampered = temporary / root.name
            shutil.copytree(root, tampered, symlinks=True)
            with (tampered / "PACKAGE_README.md").open("ab") as handle:
                handle.write(b"\nTAMPER\n")
            run(
                [sys.executable, str(tampered / "scripts/verify_checksums.py"), "--root", str(tampered)],
                cwd=tampered,
                expected={1},
            )
            checks.append("checksum_tamper_rejected")

            extra = temporary / f"{root.name}-extra"
            shutil.copytree(root, extra, symlinks=True)
            (extra / "UNLISTED.txt").write_text("invalid\n", encoding="utf-8")
            run(
                [sys.executable, str(extra / "scripts/verify_checksums.py"), "--root", str(extra)],
                cwd=extra,
                expected={1},
            )
            checks.append("unlisted_file_rejected")

            escaping = temporary / f"{root.name}-escape"
            shutil.copytree(root, escaping, symlinks=True)
            checksum_path = escaping / "CHECKSUMS.sha256"
            checksum_path.write_text(
                "0" * 64 + "  ../escape\n" + checksum_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            run(
                [sys.executable, str(escaping / "scripts/verify_checksums.py"), "--root", str(escaping)],
                cwd=escaping,
                expected={1},
            )
            checks.append("checksum_path_escape_rejected")

            symlinked = temporary / f"{root.name}-symlink"
            shutil.copytree(root, symlinked, symlinks=True)
            (symlinked / "UNLISTED-LINK").symlink_to("PACKAGE_README.md")
            run(
                [sys.executable, str(symlinked / "scripts/verify_checksums.py"), "--root", str(symlinked)],
                cwd=symlinked,
                expected={1},
            )
            checks.append("filesystem_symlink_rejected")

            first = temporary / "first.zip"
            second = temporary / "second.zip"
            run(
                [sys.executable, str(scripts / "build_package.py"), "--root", str(root), "--output", str(first)],
                cwd=root,
                expected={0},
            )
            run(
                [sys.executable, str(scripts / "build_package.py"), "--root", str(root), "--output", str(second)],
                cwd=root,
                expected={0},
            )
            if first.read_bytes() != second.read_bytes():
                raise ValueError("deterministic rebuild bytes differ")
            checks.append("deterministic_rebuild_equal")

            malicious = temporary / "malicious.zip"
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr(f"{root.name}/../escape", b"invalid")
            run(
                [sys.executable, str(scripts / "build_package.py"), "--root", str(root), "--verify", str(malicious)],
                cwd=root,
                expected={1},
            )
            checks.append("archive_path_escape_rejected")

            malicious_symlink = temporary / "malicious-symlink.zip"
            with zipfile.ZipFile(malicious_symlink, "w") as archive:
                info = zipfile.ZipInfo(f"{root.name}/PACKAGE_README.md")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"target")
            run(
                [
                    sys.executable,
                    str(scripts / "build_package.py"),
                    "--root",
                    str(root),
                    "--verify",
                    str(malicious_symlink),
                ],
                cwd=root,
                expected={1},
            )
            checks.append("archive_symlink_rejected")

            custody_tamper = temporary / f"{root.name}-custody-tamper"
            shutil.copytree(root, custody_tamper, symlinks=True)
            custody_path = custody_tamper / "baseline/source-custody-manifest.json"
            custody = json.loads(custody_path.read_text(encoding="utf-8"))
            custody["entries"][0]["sha256"] = "0" * 64
            custody_path.write_text(json.dumps(custody, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            run(
                [
                    sys.executable,
                    str(custody_tamper / "scripts/verify_checksums.py"),
                    "--root",
                    str(custody_tamper),
                    "--write",
                ],
                cwd=custody_tamper,
                expected={0},
            )
            run(
                [
                    sys.executable,
                    str(custody_tamper / "scripts/validate_package.py"),
                    "--root",
                    str(custody_tamper),
                ],
                cwd=custody_tamper,
                expected={1},
            )
            checks.append("custody_semantic_tamper_rejected")

            forged_result = temporary / "forged-accepted-result.json"
            forged_result.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "package_id": "BD-AF-P0P3-V1",
                        "package_fingerprint": "0" * 64,
                        "task_id": "BD-AF-P0-T00",
                        "baseline_commit": "0" * 40,
                        "actual_head": "0" * 40,
                        "dependency_result_sha256": {},
                        "approval_relative_path": "approval.json",
                        "approval_sha256": "0" * 64,
                        "approval_signature_relative_path": "approval.json.sig",
                        "approval_signature_sha256": "0" * 64,
                        "evidence_manifests": [
                            {
                                "criterion_id": "RB-BD-AF-P0-T00",
                                "relative_path": "evidence.json",
                                "sha256": "0" * 64,
                                "status": "PASS",
                            }
                        ],
                        "changed_paths": [],
                        "implementer_identity": "fixture-implementer",
                        "implementer_status": "FAIL",
                        "reviewer_status": "ACCEPTED",
                        "reviewer_identity": "fixture-human",
                        "reviewed_at": "2026-08-25T00:00:00+00:00",
                        "reviewer_record_relative_path": "review.json",
                        "reviewer_record_sha256": "0" * 64,
                        "reviewer_signature_relative_path": "review.json.sig",
                        "reviewer_signature_sha256": "0" * 64,
                        "residual_risks": [],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            run(
                [
                    sys.executable,
                    str(scripts / "validate_package.py"),
                    "--validate-task-result",
                    str(forged_result),
                ],
                cwd=root,
                expected={1},
            )
            checks.append("forged_accepted_dependency_result_rejected")

            custody_repo = temporary / "untracked-custody-repo"
            custody_repo.mkdir()
            (custody_repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            run(["git", "init", "-q"], cwd=custody_repo, expected={0})
            run(["git", "config", "user.email", "fixture@example.invalid"], cwd=custody_repo, expected={0})
            run(["git", "config", "user.name", "Fixture Human"], cwd=custody_repo, expected={0})
            run(["git", "add", "tracked.txt"], cwd=custody_repo, expected={0})
            run(["git", "commit", "-q", "-m", "fixture baseline"], cwd=custody_repo, expected={0})
            ignored_file = custody_repo / "delivery/packages/BD-AF-P0P3-V1/generated.txt"
            ignored_file.parent.mkdir(parents=True)
            ignored_file.write_text("generated\n", encoding="utf-8")
            near_prefix_file = custody_repo / "delivery/packages/BD-AF-P0P3-V1-near/user.txt"
            near_prefix_file.parent.mkdir(parents=True)
            near_prefix_file.write_text("user\n", encoding="utf-8")
            untracked_manifest = temporary / "untracked-custody.json"
            run(
                [
                    sys.executable,
                    str(scripts / "baseline_custody.py"),
                    "--root",
                    str(root),
                    "capture",
                    "--repo",
                    str(custody_repo),
                    "--output",
                    str(untracked_manifest),
                    "--ignore-untracked-prefix",
                    "delivery/packages/BD-AF-P0P3-V1/",
                ],
                cwd=root,
                expected={0},
            )
            untracked_payload = json.loads(untracked_manifest.read_text(encoding="utf-8"))
            if untracked_payload["untracked_change_count"] != 1 or untracked_payload["entries"] != [
                {
                    "kind": "REGULAR",
                    "path": "delivery/packages/BD-AF-P0P3-V1-near/user.txt",
                    "sha256": "6d9010b2b7a1483b256ae7477738dba7c530bd9ba53db1d6691441e74b83608a",
                    "status": "?",
                }
            ]:
                raise ValueError("untracked custody exclusion swallowed a near-prefix user path")
            checks.append("untracked_custody_near_prefix_preserved")

        if args.repo:
            with tempfile.TemporaryDirectory() as custody_temp_name:
                custody_output = Path(custody_temp_name) / "fresh-custody.json"
                expected_custody = json.loads(
                    (root / "baseline/source-custody-manifest.json").read_text(encoding="utf-8")
                )
                custody_command = [
                    sys.executable,
                    str(scripts / "baseline_custody.py"),
                    "--root",
                    str(root),
                    "capture",
                    "--repo",
                    str(args.repo.resolve()),
                    "--output",
                    str(custody_output),
                ]
                for prefix in expected_custody["ignored_untracked_prefixes"]:
                    custody_command.extend(["--ignore-untracked-prefix", prefix])
                run(
                    custody_command,
                    cwd=root,
                    expected={0},
                )
                actual_custody = json.loads(custody_output.read_text(encoding="utf-8"))
                if actual_custody != expected_custody:
                    raise ValueError("fresh source custody capture differs from the package-bound manifest")
                checks.append("protected_source_custody_unchanged")
            result = run(
                [
                    sys.executable,
                    str(scripts / "preflight_execution.py"),
                    "--root",
                    str(root),
                    "--repo",
                    str(args.repo.resolve()),
                    "--task",
                    "BD-AF-P0-T00",
                ],
                cwd=root,
                expected={3},
            )
            payload = json.loads(result.stdout)
            if payload.get("status") != "HOLD":
                raise ValueError("preflight exit 3 did not carry HOLD status")
            checks.append("implementation_preflight_holds")
            with tempfile.TemporaryDirectory() as acceptance_temp_name:
                acceptance_temp = Path(acceptance_temp_name)
                dummy_approval = acceptance_temp / "invalid-approval.json"
                dummy_approval.write_text("{}\n", encoding="utf-8")
                dummy_signature = acceptance_temp / "invalid-approval.json.sig"
                dummy_signature.write_text("invalid\n", encoding="utf-8")
                dummy_trust_root = acceptance_temp / "invalid.allowed_signers"
                dummy_trust_root.write_text("invalid\n", encoding="utf-8")
                results_dir = acceptance_temp / "results-must-not-exist"
                run(
                    [
                        sys.executable,
                        str(scripts / "run_acceptance.py"),
                        "--root",
                        str(root),
                        "--repo",
                        str(args.repo.resolve()),
                        "--task",
                        "BD-AF-P0-T00",
                        "--approval",
                        str(dummy_approval),
                        "--approval-signature",
                        str(dummy_signature),
                        "--approval-trust-root",
                        str(dummy_trust_root),
                        "--results-dir",
                        str(results_dir),
                        "--implementer-identity",
                        "self-test-implementer",
                    ],
                    cwd=root,
                    expected={3},
                )
                if results_dir.exists():
                    raise ValueError("acceptance runner created evidence before preflight became READY")
                checks.append("acceptance_runner_preflight_no_mutation")
            synthetic = run(
                [
                    sys.executable,
                    str(scripts / "synthetic_ready_path_test.py"),
                    "--root",
                    str(root),
                    "--original-repo",
                    str(args.repo.resolve()),
                ],
                cwd=root,
                expected={0},
            )
            synthetic_payload = json.loads(synthetic.stdout)
            expected_synthetic = {
                "status": "SYNTHETIC_READY_PATH_PASS",
                "t00_preflight": "READY",
                "t00_acceptance": "PASS",
                "t00_rollback": "PASS",
                "t00_finalized": "TASK_RESULT_FINALIZED",
                "t01_dependency_preflight": "READY",
                "forged_dependency_preflight": "HOLD",
                "tampered_evidence_preflight": "HOLD",
                "evidence_manifest_count": 3,
            }
            if synthetic_payload != expected_synthetic:
                raise ValueError(f"unexpected signed READY-path result: {synthetic_payload}")
            checks.append("signed_ready_dependency_and_tamper_paths")
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "SELF_TEST_FAILED", "checks_completed": checks, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "SELF_TEST_PASS", "checks": checks, "count": len(checks)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
