"""Independent semantic checks for G5/G7 certificates.

These tests deliberately exercise certificate meaning rather than the runner's
own ``status`` field.  A certificate that claims PASS but omits its required
evidence must remain non-verifiable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_certification.gate_verifier import (
    _number,
    _timestamp,
    verify_evidence_integrity,
    verify_g5_certificate,
    verify_g7_certificate,
)

EXPECTED_SCENARIOS = {
    "create_query_cancel",
    "stable_client_order_id",
    "ack_loss",
    "timeout_unknown_recovery",
    "duplicate_request",
    "partial_fill",
    "cancel_fill_race",
    "user_stream_reconnect",
    "native_protection",
    "process_restart",
    "database_restart",
    "double_worker_fencing",
    "clock_skew",
    "rate_limit",
    "credential_failure",
    "reconciliation_mismatch",
}


def _g5(**overrides: object) -> dict[str, object]:
    started = "2026-08-08T00:00:00+00:00"
    ended = "2026-08-08T00:10:00+00:00"
    scenarios = {scenario: {"status": "PASS"} for scenario in EXPECTED_SCENARIOS}
    certificate: dict[str, object] = {
        "gate": "G5",
        "status": "PASS",
        "commit": "abc123",
        "environment": "BINANCE_USDM_TESTNET_ONLY",
        "testnet_url": "https://demo-fapi.binance.com",
        "mainnet_prohibited": True,
        "is_simulated": False,
        # M20-F02: 认证模式必须显式标注
        "certification_mode": "DEV_BYPASS",
        "started_at": started,
        "ended_at": ended,
        "evidence_hash": "e" * 64,
        "max_notional_usdt": 20.0,
        "scenarios": scenarios,
        "summary": {"total": 16, "pass": 16, "warn": 0, "fail": 0},
        "account_access": {"can_withdraw": False},
        "blockers": [],
    }
    certificate.update(overrides)
    return certificate


def _g7(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    certificate: dict[str, object] = {
        "gate": "G7",
        "status": "PASS",
        "window_id": "g7-real-1",
        "duration_days": 30,
        "started_at": (now - timedelta(days=31)).isoformat(),
        "ended_at": now.isoformat(),
        "commit": "abc123",
        "g5_certificate_hash": "g" * 64,
        "evidence_hash": "e" * 64,
        "mainnet_prohibited": True,
        "is_simulated": False,
        "summary": {
            "total_sli_samples": 300,
            "sli_pass_rate": "100.0%",
            "total_incidents": 0,
            "p0_incidents": 0,
            "active_incidents": 0,
            "total_recoveries": 0,
            "daily_reports": 31,
            "resets": 0,
        },
        "disclaimer": "Real unattended evidence",
    }
    certificate.update(overrides)
    return certificate


def test_legacy_g5_certificate_is_not_verifiable() -> None:
    legacy = {
        "gate": "G5",
        "status": "PASS",
        "commit": "6303c6cb45b8342e54927dd6e23d6368bb584164",
        "testnet_url": "https://demo-fapi.binance.com",
        "timestamp": "2026-08-08T16:49:00.566139+00:00",
        "scenarios": {"server_time": {"status": "PASS"}},
        "summary": {"total": 1, "pass": 1, "warn": 0, "fail": 0},
        "account_access": {"can_withdraw": True},
    }

    result = verify_g5_certificate(legacy, expected_commit="abc123", expected_scenarios=EXPECTED_SCENARIOS)

    assert result.status == "NOT_VERIFIABLE"
    assert not result.passed
    assert {"commit", "started_at", "ended_at", "scenario_set", "withdraw_permission"}.issubset(set(result.failures))


def test_g5_warn_withdraw_and_missing_scenario_block_pass() -> None:
    scenarios = {scenario: {"status": "PASS"} for scenario in EXPECTED_SCENARIOS}
    scenarios.pop("ack_loss")
    scenarios["rate_limit"] = {"status": "WARN"}
    certificate = _g5(
        scenarios=scenarios,
        summary={"total": 15, "pass": 14, "warn": 1, "fail": 0},
        account_access={"can_withdraw": True},
    )

    result = verify_g5_certificate(certificate, expected_commit="abc123", expected_scenarios=EXPECTED_SCENARIOS)

    assert result.status == "NOT_VERIFIABLE"
    assert {"scenario_set", "scenario_status:rate_limit", "withdraw_permission"}.issubset(set(result.failures))


def test_simulated_g7_certificate_is_not_verifiable() -> None:
    certificate = _g7(is_simulated=True, disclaimer="SIMULATED G7 certificate")

    result = verify_g7_certificate(
        certificate,
        expected_commit="abc123",
        expected_g5_hash="g" * 64,
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    assert result.status == "NOT_VERIFIABLE"
    assert {"simulation", "disclaimer"}.issubset(set(result.failures))


def test_real_g7_certificate_with_bound_evidence_passes() -> None:
    result = verify_g7_certificate(
        _g7(),
        expected_commit="abc123",
        expected_g5_hash="g" * 64,
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    assert result.status == "PASS"
    assert result.passed
    assert result.failures == []


def test_g5_certificate_without_certification_mode_is_not_verifiable() -> None:
    """M20-F02: 缺失认证模式标注的证书视为伪造拒绝。"""
    from beidou_certification.gate_verifier import verify_g5_certificate

    verification = verify_g5_certificate(
        _g5(certification_mode=None),
        expected_commit="abc123",
        expected_scenarios=EXPECTED_SCENARIOS,
    )
    assert verification.passed is False
    assert "certification_mode" in verification.failures


def test_g5_dev_bypass_mode_passes_with_explicit_label() -> None:
    """DEV_BYPASS 显式标注的证书可通过(妥协可审计,不再静默伪造)。"""
    from beidou_certification.gate_verifier import verify_g5_certificate

    verification = verify_g5_certificate(
        _g5(certification_mode="DEV_BYPASS"),
        expected_commit="abc123",
        expected_scenarios=EXPECTED_SCENARIOS,
    )
    assert verification.passed is True


# ---- Ruling-22:testnet 提现权限显式豁免(demo-fapi canWithdraw 恒 True) ----


def test_g5_withdraw_exempt_on_demo_fapi_host() -> None:
    """demo-fapi host + allow=True → 豁免生效:checks 记
    withdraw_permission_exempt=True,failures 不含 withdraw_permission。"""
    from beidou_certification.gate_verifier import verify_g5_certificate

    verification = verify_g5_certificate(
        _g5(account_access={"can_withdraw": True}),
        expected_commit="abc123",
        expected_scenarios=EXPECTED_SCENARIOS,
        allow_withdraw_permission=True,
    )
    assert verification.passed is True
    assert verification.checks["withdraw_permission_exempt"] is True
    assert "withdraw_permission" not in verification.failures


def test_g5_withdraw_exempt_invalid_on_mainnet_host() -> None:
    """mainnet host + allow=True → 豁免无效(提现权限硬性保留):仍 failed,
    failures 含 withdraw_permission,checks 无豁免标记。"""
    from beidou_certification.gate_verifier import verify_g5_certificate

    verification = verify_g5_certificate(
        _g5(
            testnet_url="https://fapi.binance.com",
            account_access={"can_withdraw": True},
        ),
        expected_commit="abc123",
        expected_scenarios=EXPECTED_SCENARIOS,
        allow_withdraw_permission=True,
    )
    assert verification.passed is False
    assert verification.status == "NOT_VERIFIABLE"
    assert "withdraw_permission" in verification.failures
    assert "withdraw_permission_exempt" not in verification.checks


def test_g5_withdraw_not_exempt_without_allow_flag() -> None:
    """allow=False(缺省)→ 豁免未生效,行为与现状一致:demo-fapi host +
    canWithdraw=True 仍 failed 且 failures 含 withdraw_permission。"""
    from beidou_certification.gate_verifier import verify_g5_certificate

    verification = verify_g5_certificate(
        _g5(account_access={"can_withdraw": True}),
        expected_commit="abc123",
        expected_scenarios=EXPECTED_SCENARIOS,
    )
    assert verification.passed is False
    assert "withdraw_permission" in verification.failures
    assert "withdraw_permission_exempt" not in verification.checks


def test_gate_verifier_parses_numbers_timestamps_and_evidence_hashes(tmp_path) -> None:
    assert _timestamp(datetime(2026, 1, 1, tzinfo=timezone.utc)) is not None
    assert _timestamp("2026-01-01T00:00:00Z") is not None
    assert _timestamp("bad") is None
    assert _timestamp(None) is None
    assert _number(True) is None
    assert _number("bad") is None
    assert _number("inf") is None
    assert _number("1.5") == 1.5

    evidence = tmp_path / "evidence.json"
    evidence.write_text("payload", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(b"payload").hexdigest()
    ok, failures = verify_evidence_integrity({"evidence_hashes": {evidence.name: digest}}, str(tmp_path))
    assert ok and failures == []
    bad, failures = verify_evidence_integrity(
        {"evidence_hashes": {"missing.json": digest, evidence.name: "wrong"}}, str(tmp_path)
    )
    assert not bad and any(item.startswith("MISSING_EVIDENCE") for item in failures)
    assert any(item.startswith("HASH_MISMATCH") for item in failures)

    manifest = {"name": "bundle", "evidence_hashes": {}}
    import json

    manifest["manifest_hash"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    assert verify_evidence_integrity(manifest)[0]
    manifest["manifest_hash"] = "wrong"
    assert not verify_evidence_integrity(manifest)[0]
