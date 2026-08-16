"""Regression tests for the fail-closed writable-Testnet G5 preflight gate."""

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


def test_dev_fast_start_cannot_remove_g5_preflight_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BEIDOU_DEV_FAST_START", "1")

    checks, _settings = preflight.run_preflight(tmp_path, "testnet", 0)

    g5_checks = [check for check in checks if check.check_id == "preflight.g5_certificate"]
    assert len(g5_checks) == 1
    assert g5_checks[0].status is CheckStatus.FAIL
    # M22-F05 (merge 回归修复): 豁免仅降级阻断语义(P2 不阻断),
    # 检查永不缺席、status 恒为真实判定。
    assert g5_checks[0].severity is CheckSeverity.P2
    assert g5_checks[0].is_blocking is False


def test_g5_producer_preflight_omits_only_existing_certificate_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)

    launcher_checks, _ = preflight.run_preflight(tmp_path, "testnet", 0)
    producer_checks, _ = preflight.run_g5_producer_preflight(tmp_path, 0)

    assert any(check.check_id == "preflight.g5_certificate" for check in launcher_checks)
    assert all(check.check_id != "preflight.g5_certificate" for check in producer_checks)
    launcher_without_g5 = {
        check.check_id: (check.status, check.severity)
        for check in launcher_checks
        if check.check_id != "preflight.g5_certificate"
    }
    producer = {check.check_id: (check.status, check.severity) for check in producer_checks}
    assert producer == launcher_without_g5


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
