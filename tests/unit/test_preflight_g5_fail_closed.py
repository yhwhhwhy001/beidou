"""Regression tests for the frozen, opt-in G5 preflight gate (V4 PKG-08-M03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from beidou_launcher import preflight
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from scripts.testnet.run_g5 import blocking_preflight_checks, require_exchange_symbol, validate_probe_symbol


def test_missing_g5_certificate_is_rejected_without_creating_evidence(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "g5-testnet-plan.yaml").write_text("scenarios: [S1]\nmax_test_notional_usdt: 20\n")

    passed, message, evidence = preflight._g5_certificate_probe(tmp_path, "commit-under-test")

    certificate_path = tmp_path / "artifacts" / "evidence" / "testnet" / "g5-certificate.json"
    assert passed is False
    assert message == "G5 certificate is missing"
    assert evidence["commit"] == "commit-under-test"
    assert not certificate_path.exists()


def test_preflight_has_no_synthetic_g5_pass_generator() -> None:
    source = Path(preflight.__file__).read_text(encoding="utf-8")

    assert "AUTO_GENERATED" not in source
    assert "_auto_generate_g5" not in source


def test_g5_gate_is_opt_in_and_absent_from_default_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """BD-FIX (V4 PKG-08-M03): 默认 preflight 不再包含 G5 证书门禁;
    显式 require_g5_certificate=True 的冻结认证路径仍 fail-closed。"""

    monkeypatch.setenv("BEIDOU_DEV_FAST_START", "1")
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)

    default_checks, _settings = preflight.run_preflight(tmp_path, "testnet", 0)
    assert all(check.check_id != "preflight.g5_certificate" for check in default_checks)

    explicit_checks, _settings = preflight._run_preflight(tmp_path, "testnet", 0, require_g5_certificate=True)
    g5_checks = [check for check in explicit_checks if check.check_id == "preflight.g5_certificate"]
    assert len(g5_checks) == 1
    assert g5_checks[0].status is CheckStatus.FAIL
    assert g5_checks[0].severity is CheckSeverity.P0


def test_g5_producer_preflight_matches_default_launcher_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """默认 launcher 与 producer preflight 现在都不包含证书门禁(G5 冻结)。"""

    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)

    launcher_checks, _ = preflight.run_preflight(tmp_path, "testnet", 0)
    producer_checks, _ = preflight.run_g5_producer_preflight(tmp_path, 0)

    assert all(check.check_id != "preflight.g5_certificate" for check in launcher_checks)
    assert all(check.check_id != "preflight.g5_certificate" for check in producer_checks)
    launcher = {check.check_id: (check.status, check.severity) for check in launcher_checks}
    producer = {check.check_id: (check.status, check.severity) for check in producer_checks}
    assert producer == launcher


def test_g5_runner_uses_dedicated_producer_preflight() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts" / "testnet" / "run_g5.py").read_text(encoding="utf-8")

    assert "run_g5_producer_preflight" in source
    assert "run_preflight(project_root" not in source


@pytest.mark.parametrize(
    "symbol",
    ["BTCUSDT,ETHUSDT", "BTC/USDT", "BTC USDT", "ALL", "DEFAULT", "BTC", "12345", "BTCUSDTETHUSDT"],
)
def test_g5_probe_symbol_rejects_ambiguous_or_non_market_values(symbol: str) -> None:
    with pytest.raises(ValueError, match="one explicit"):
        validate_probe_symbol(symbol)


def test_g5_probe_symbol_normalizes_one_explicit_market() -> None:
    assert validate_probe_symbol(" btcusdt ") == "BTCUSDT"


def test_g5_exchange_info_requires_exact_requested_symbol() -> None:
    assert require_exchange_symbol("BTCUSDT", [{"symbol": "BTCUSDT", "status": "TRADING"}]) == {
        "symbol": "BTCUSDT",
        "status": "TRADING",
    }
    with pytest.raises(ValueError, match="not returned"):
        require_exchange_symbol("BTCUSDT", [{"symbol": "ETHUSDT", "status": "TRADING"}])


def test_g5_producer_blocks_p0_and_p1_fail_or_unknown() -> None:
    checks = [
        CheckResult("p0-fail", "p0", CheckStatus.FAIL, CheckSeverity.P0, "blocked"),
        CheckResult("p1-fail", "p1", CheckStatus.FAIL, CheckSeverity.P1, "blocked"),
        CheckResult("p1-unknown", "p1 unknown", CheckStatus.UNKNOWN, CheckSeverity.P1, "blocked"),
        CheckResult("p2-unknown", "p2 unknown", CheckStatus.UNKNOWN, CheckSeverity.P2, "diagnostic"),
        CheckResult("p1-pass", "p1 pass", CheckStatus.PASS, CheckSeverity.P1, "ok"),
    ]

    assert [check.check_id for check in blocking_preflight_checks(checks)] == [
        "p0-fail",
        "p1-fail",
        "p1-unknown",
    ]
