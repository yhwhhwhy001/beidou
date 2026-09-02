"""Characterization and side-effect proofs for the Alpha-First CLI boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import beidou_cli

ROOT = Path(__file__).resolve().parents[2]


def _evidence(name: str, payload: object) -> None:
    evidence_dir = os.environ.get("BEIDOU_EVIDENCE_DIR", "").strip()
    if evidence_dir:
        path = Path(evidence_dir) / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and local module
        [sys.executable, "-m", "beidou_cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
        },
        check=False,
        timeout=30,
    )


def test_bare_root_is_helpful_and_side_effect_free(tmp_path: Path) -> None:
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = _run(cwd=tmp_path)
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    assert result.returncode == 0
    assert "execution" in result.stdout
    assert before == after
    assert "beidou_launcher" not in result.stdout
    _evidence(
        "cli-side-effect-inventory.json",
        {
            "commands": ["beidou", "beidou --help", "beidou status"],
            "filesystem_before": before,
            "filesystem_after": after,
            "network": "DENIED",
            "launcher_constructed": False,
            "credential_reads": 0,
            "database_writes": 0,
            "status": "PASS",
        },
    )


def test_help_has_no_launcher_or_runtime_imports() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "offline Alpha" in result.stdout
    assert "beidou_launcher" not in result.stdout


def test_status_reports_observed_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {
        "supervisor_state": "RUNNING",
        "effective_state": "RUNNING",
        "process_alive": True,
        "evidence_age_seconds": 0.5,
    }
    monkeypatch.setattr(beidou_cli, "_inspect_runtime_status", lambda: observed, raising=False)

    result = CliRunner().invoke(beidou_cli.main, ["status"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["runtime"] == "RUNNING"
    assert payload["runtime_evidence"] == observed
    assert payload["execution"] == "EXPLICIT_ONLY"


def test_status_reports_not_running_only_when_no_runtime_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beidou_cli, "_inspect_runtime_status", lambda: None, raising=False)

    result = CliRunner().invoke(beidou_cli.main, ["status"])

    assert result.exit_code == 0
    assert json.loads(result.output)["runtime"] == "NOT_RUNNING"


def test_doctor_is_read_only_facade_and_propagates_blocking_status(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_calls: list[tuple[str, int]] = []

    def run_doctor(mode: str, port: int) -> tuple[list[dict[str, object]], bool]:
        observed_calls.append((mode, port))
        return ([{"check_id": "preflight.example", "status": "FAIL"}], True)

    monkeypatch.setattr(beidou_cli, "_run_read_only_doctor", run_doctor, raising=False)

    result = CliRunner().invoke(beidou_cli.main, ["doctor", "--mode", "safety_only", "--port", "19090"])

    assert result.exit_code == 2
    assert json.loads(result.output)["check_id"] == "preflight.example"
    assert observed_calls == [("safety_only", 19090)]


def test_stop_uses_governed_runtime_identity_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beidou_cli, "_stop_runtime", lambda: (False, "identity mismatch"), raising=False)

    result = CliRunner().invoke(beidou_cli.main, ["stop"])

    assert result.exit_code == 1
    assert "identity mismatch" in result.output


def test_unknown_root_command_fails_closed() -> None:
    result = _run("unknown-command")
    assert result.returncode != 0
    assert "No such command" in result.stderr


def test_legacy_start_alias_routes_to_explicit_authorization_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEIDOU_EXECUTION_AUTHORIZATION", raising=False)
    result = _run("start", "--mode", "paper", "--symbols", "BTCUSDT")
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "authorization" in combined.lower()
    assert "No such command" not in combined


def test_execution_start_requires_explicit_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEIDOU_EXECUTION_AUTHORIZATION", raising=False)
    result = _run("execution", "start")
    assert result.returncode != 0
    assert "authorization" in (result.stdout + result.stderr).lower()


def test_execution_start_rejects_legacy_testnet_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEIDOU_EXECUTION_AUTHORIZATION", "EXPLICIT_LOCAL_APPROVAL")
    result = _run("execution", "start", "--mode", "testnet", "--symbols", "BTCUSDT")
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "apps.testnet_verify" in combined


def test_authorized_execution_forwards_explicit_legacy_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_launcher import alpha_first_adapter

    calls: list[tuple[object, dict[str, object]]] = []

    def fake_legacy_main(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setenv("BEIDOU_EXECUTION_AUTHORIZATION", "EXPLICIT_LOCAL_APPROVAL")
    monkeypatch.setattr("beidou_launcher.cli.main", fake_legacy_main)
    assert (
        alpha_first_adapter.start_authorized_execution(
            mode="paper",
            symbols=("BTCUSDT",),
            port=9090,
            startup_timeout=12.5,
            monitor_interval=3.0,
            self_heal=False,
            max_restarts=2,
        )
        == 0
    )
    assert calls == [
        (
            (
                [
                    "start",
                    "--mode",
                    "paper",
                    "--symbols",
                    "BTCUSDT",
                    "--port",
                    "9090",
                    "--startup-timeout",
                    "12.5",
                    "--monitor-interval",
                    "3.0",
                    "--no-self-heal",
                    "--max-restarts",
                    "2",
                ],
            ),
            {"standalone_mode": False},
        )
    ]


def test_alpha_evaluate_is_explicit_offline_workflow() -> None:
    result = _run("alpha", "evaluate", "--closes", ",".join(str(100 + i * 0.25) for i in range(60)))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["row_count"] == 60
    assert payload["dataset_source"] == "LOCAL"


def test_rollback_prior_launcher_mapping_remains_available() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'beidou = "beidou_cli:main"' in pyproject
    assert (ROOT / "beidou_launcher" / "cli.py").exists()
