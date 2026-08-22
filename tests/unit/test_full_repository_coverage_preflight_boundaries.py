"""Read-only preflight coverage for certificate, source, and guard failures."""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_launcher import preflight


def _write_g5_plan(root: Path) -> None:
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "g5-testnet-plan.yaml").write_text("scenarios: [S1]\nmax_test_notional_usdt: 20\n", encoding="utf-8")


def test_g5_probe_reports_plan_malformed_verification_and_success(monkeypatch, tmp_path: Path) -> None:
    _write_g5_plan(tmp_path)
    certificate = tmp_path / "artifacts" / "evidence" / "testnet" / "g5-certificate.json"
    certificate.parent.mkdir(parents=True)
    certificate.write_text("{}", encoding="utf-8")
    plan = tmp_path / "config" / "g5-testnet-plan.yaml"

    plan.unlink()
    assert preflight._g5_certificate_probe(tmp_path, "a" * 40)[1] == "G5 plan is missing"
    _write_g5_plan(tmp_path)

    certificate.write_text("[]", encoding="utf-8")
    assert preflight._g5_certificate_probe(tmp_path, "a" * 40)[1] == "G5 certificate or scenario plan is malformed"

    certificate.write_text(json.dumps({"certificate": True}), encoding="utf-8")
    monkeypatch.setattr(
        "beidou_certification.gate_verifier.verify_g5_certificate",
        lambda *_args, **_kwargs: SimpleNamespace(passed=False, to_dict=lambda: {"passed": False}),
    )
    assert preflight._g5_certificate_probe(tmp_path, "a" * 40)[1] == "G5 certificate is not independently verifiable"

    monkeypatch.setattr(
        "beidou_certification.gate_verifier.verify_g5_certificate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("invalid certificate")),
    )
    passed, message, evidence = preflight._g5_certificate_probe(tmp_path, "a" * 40)
    assert passed is False and message == "G5 certificate verification failed"
    assert evidence["error_type"] == "RuntimeError"

    monkeypatch.setattr(
        "beidou_certification.gate_verifier.verify_g5_certificate",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True, to_dict=lambda: {"passed": True}),
    )
    passed, message, evidence = preflight._g5_certificate_probe(tmp_path, "a" * 40)
    assert passed is True and message == "G5 Testnet certificate verified"
    assert evidence["verification"] == {"passed": True}


class _ProbeCursor:
    def __init__(self, *, one: tuple[object, ...] | None = None, rows: list[tuple[object, ...]] | None = None) -> None:
        self.one = one
        self.rows = rows or []

    def fetchone(self) -> tuple[object, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _ProbeConnection:
    def __enter__(self) -> "_ProbeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> _ProbeCursor:
        if "to_regclass" in sql:
            return _ProbeCursor(one=(str(params[0]).removeprefix("public."),))
        return _ProbeCursor(rows=[])


def test_preflight_authority_rejects_unsupported_and_incomplete_migration(monkeypatch, tmp_path: Path) -> None:
    ok, message, evidence = preflight._postgres_authority_probe(tmp_path, "sqlite:///diagnostic.db")
    assert ok is False and message == "PostgreSQL DSN is not configured"
    assert evidence["backend"] == "unsupported"

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *_a, **_k: _ProbeConnection()))
    ok, message, evidence = preflight._postgres_authority_probe(
        Path(__file__).resolve().parents[2], "postgresql://user:secret@db/beidou"
    )
    assert ok is False and message == "PostgreSQL migration head is incomplete"
    assert evidence["missing_migrations"]
    assert "secret" not in message


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (RuntimeError("git unavailable"), (False, [], "RuntimeError: git unavailable")),
        (SimpleNamespace(returncode=1, stdout="", stderr="status failed"), (False, [], "status failed")),
        (SimpleNamespace(returncode=0, stdout=" M tracked.py\n", stderr=""), (True, [" M tracked.py"], "")),
        (SimpleNamespace(returncode=0, stdout="", stderr=""), (True, [], "")),
    ],
)
def test_git_worktree_state_reports_exception_failure_dirty_and_clean(
    monkeypatch, result: object, expected: tuple
) -> None:
    def run(*_args: object, **_kwargs: object) -> object:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight._git_worktree_state(Path(".")) == expected


def test_launchd_drift_detects_missing_proxy_argument(tmp_path: Path) -> None:
    template = tmp_path / "deploy"
    template.mkdir()
    with (template / "com.beidou.autopilot.plist").open("wb") as handle:
        plistlib.dump(
            {"KeepAlive": False, "ThrottleInterval": 30, "EnvironmentVariables": {"HTTPS_PROXY": "http://proxy"}},
            handle,
        )
    installed = tmp_path / "installed.plist"
    with installed.open("wb") as handle:
        plistlib.dump({"ProgramArguments": ["beidou", "start"]}, handle)
    drift, evidence = preflight._launchd_plist_drift(tmp_path, installed_path=installed)
    assert any("代理" in item for item in drift)
    assert evidence["installed"] is True


def test_run_preflight_covers_clean_storage_policy_guard_and_config_failures(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(preflight, "_git_worktree_state", lambda _root: (True, [], ""))
    monkeypatch.setattr(preflight, "_port_available", lambda _port: (True, "available"))
    monkeypatch.setattr(preflight, "check_package_imports", lambda: [])

    checks, settings = preflight._run_preflight(root, "paper", 19101, require_g5_certificate=False)
    assert settings is not None
    assert next(item for item in checks if item.check_id == "preflight.git_worktree").status.value == "PASS"
    assert next(item for item in checks if item.check_id == "preflight.runtime_storage").status.value == "PASS"

    from beidou_policy.loader import PolicyLoader

    monkeypatch.setattr(
        PolicyLoader,
        "load",
        lambda self, _policy_id: SimpleNamespace(validate_risk_parameters=lambda: (False, "incomplete")),
    )
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret")
    monkeypatch.setenv("BEIDOU_SIGNING_KEY", "testnet-signing-key")
    checks, _ = preflight._run_preflight(root, "testnet", 19102, require_g5_certificate=False)
    policy = next(item for item in checks if item.check_id == "preflight.signed_policy")
    assert policy.status.value == "FAIL"

    monkeypatch.setattr(
        "beidou_shared.config.ConfigProvider.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("config unavailable")),
    )
    checks, settings = preflight._run_preflight(root, "paper", 19103, require_g5_certificate=False)
    assert settings is None
    assert next(item for item in checks if item.check_id == "preflight.config").status.value == "FAIL"


def test_run_preflight_records_environment_guard_exception(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(preflight, "_git_worktree_state", lambda _root: (True, [], ""))
    monkeypatch.setattr(preflight, "_port_available", lambda _port: (True, "available"))
    monkeypatch.setattr(preflight, "check_package_imports", lambda: [])
    monkeypatch.setattr(
        "beidou_core.guard.EnvironmentGuard.run_all_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("guard unavailable")),
    )
    checks, _ = preflight._run_preflight(root, "paper", 19104, require_g5_certificate=False)
    guard = next(item for item in checks if item.check_id == "preflight.environment_guard")
    assert guard.status.value == "FAIL"
