"""Regression tests for the fail-closed writable-Testnet G5 preflight gate."""

from __future__ import annotations

from pathlib import Path

from beidou_launcher import preflight
from beidou_launcher.models import CheckStatus


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
