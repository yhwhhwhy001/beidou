"""Signed risk-policy completeness and identity contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_policy.loader import REQUIRED_RISK_PARAMETERS, PolicyEnvelope, PolicyLoader

SIGNING_KEY = "unit-test-signing-key-value"


def _parameters() -> dict[str, float | int]:
    return {
        "max_leverage": 3.0,
        "max_concentration_pct": 50.0,
        "max_position_notional": 500_000.0,
        "max_total_leverage": 3.0,
        "max_instruments": 10,
        "drift_threshold": 0.1,
        "max_drawdown_pct": 20.0,
        "max_daily_loss_pct": 5.0,
        "max_consecutive_losses": 5,
        "risk_per_trade_pct": 1.0,
        "min_sharpe_rolling": 0.0,
    }


def _write_policy(path: Path, *, policy_id: str = "risk_parameters", parameters: object | None = None) -> None:
    values = _parameters() if parameters is None else parameters
    payload = {
        "policy_id": policy_id,
        "version": "1.0.0",
        "parameters": values,
        "issued_at": "2026-08-09T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    canonical = json.dumps(
        {
            "policy_id": payload["policy_id"],
            "version": payload["version"],
            "parameters": payload["parameters"],
            "issued_at": payload["issued_at"],
            "expires_at": payload["expires_at"],
        },
        sort_keys=True,
    )
    payload["signature"] = hmac.new(SIGNING_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_complete_signed_risk_policy_loads_without_defaults(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    _write_policy(policy_dir / "risk_parameters.json")

    envelope = PolicyLoader(str(policy_dir), signing_key=SIGNING_KEY).load("risk_parameters")

    assert envelope is not None
    assert set(envelope.parameters) == REQUIRED_RISK_PARAMETERS
    assert envelope.validate_risk_parameters() == (True, "complete risk parameter contract")


def test_signed_policy_with_missing_parameter_is_invalid(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    values = _parameters()
    values.pop("max_leverage")
    _write_policy(policy_dir / "risk_parameters.json", parameters=values)

    envelope = PolicyLoader(str(policy_dir), signing_key=SIGNING_KEY).load("risk_parameters")

    assert envelope is not None
    valid, reason = envelope.validate_risk_parameters()
    assert not valid
    assert "max_leverage" in reason


def test_policy_loader_rejects_envelope_bound_to_different_id(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    _write_policy(policy_dir / "risk_parameters.json", policy_id="autopilot_risk")

    assert PolicyLoader(str(policy_dir), signing_key=SIGNING_KEY).load("risk_parameters") is None


def test_policy_parameter_bounds_are_fail_closed() -> None:
    envelope = PolicyEnvelope(
        policy_id="risk_parameters",
        version="1.0.0",
        parameters={**_parameters(), "max_drawdown_pct": 101.0},
        signature="signed",
        issued_at="2026-08-09T00:00:00+00:00",
    )

    valid, reason = envelope.validate_risk_parameters()

    assert not valid
    assert "max_drawdown_pct" in reason


def test_writable_engine_never_reads_unsigned_policy_default() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._can_write = True
    engine._policy_params = {}

    with pytest.raises(RuntimeError, match="SIGNED_POLICY_PARAMETER_MISSING:max_leverage"):
        engine._policy_float("max_leverage", 3.0)
