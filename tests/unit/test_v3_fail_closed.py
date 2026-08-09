"""V3 fail-closed regression tests.

These tests intentionally exercise the public safety seams instead of private
exchange calls.  They are the first proof slice for BD-V3-01/BD-V3-03.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from beidou_core.engine import AutonomousEngine
from beidou_core.health import HealthServer
from beidou_launcher.g7_tracker import G7LiveTracker
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.preflight import run_preflight


def test_testnet_without_signing_key_is_a_p0_startup_blocker(monkeypatch, tmp_path: Path) -> None:
    """A writable environment must never fall back to a built-in signing key."""

    monkeypatch.delenv("BEIDOU_SIGNING_KEY", raising=False)
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key-value")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret-value")

    checks, _settings = run_preflight(tmp_path, "testnet", 19090)

    signing = next(item for item in checks if item.check_id == "preflight.signing_key")
    assert signing.status == CheckStatus.FAIL
    assert signing.severity == CheckSeverity.P0
    assert "mock" not in signing.message.lower()


def test_supervisor_write_interlock_requires_live_resume_authority(tmp_path: Path) -> None:
    """Environment write capability is not sufficient while the supervisor is unsafe."""

    from beidou_launcher.supervisor import BeidouSupervisor

    calls: list[tuple[str, str]] = []

    async def original_async(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    def original_sync(
        path: str, method: str = "GET", signed: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        del signed, params
        calls.append((method, path))
        return {"ok": True}

    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19090)
    control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"))
    supervisor.engine = SimpleNamespace(
        _can_write=True,
        _api_async=original_async,
        _api=original_sync,
        _control=control,
    )
    supervisor._install_exchange_write_interlock()

    blocked = asyncio.run(supervisor.engine._api_async("/order", method="POST"))
    assert blocked["error"] == -3
    assert calls == []

    supervisor._resume_authorized = True
    supervisor.report.supervisor_state = "RUNNING"
    supervisor.engine._control = SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME"))
    allowed = asyncio.run(supervisor.engine._api_async("/order", method="POST"))
    assert allowed == {"ok": True}
    assert calls == [("POST", "/order")]


def test_health_server_defaults_to_loopback() -> None:
    assert HealthServer()._bind_host == "127.0.0.1"


def test_g7_is_not_eligible_without_real_elapsed_window_or_samples() -> None:
    tracker = G7LiveTracker()
    summary = tracker.summary()
    assert summary["g7_eligible"] is False
    assert summary["all_slis_passing"] is False


def test_g7_p0_invalidates_the_window() -> None:
    tracker = G7LiveTracker()
    from beidou_launcher.models import CheckResult

    tracker.feed(
        [
            CheckResult(
                "runtime.health.realtime_heartbeat",
                "heartbeat",
                CheckStatus.FAIL,
                CheckSeverity.P0,
                "stale",
            )
        ]
    )
    summary = tracker.summary()
    assert summary["g7_eligible"] is False
    assert summary["invalidated"] is True


def test_config_safe_defaults_do_not_offer_network_write() -> None:
    from beidou_shared.config import ConfigProvider, Environment

    settings = ConfigProvider().load(environment="unknown-v3-environment")
    assert settings.environment is Environment.SAFETY_ONLY
    assert settings.can_write_trades is False
    assert settings.infrastructure.health_host == "127.0.0.1"
    assert settings.exchange.rest_base_url == ""
    assert settings.exchange.ws_base_url == ""
    assert Environment.SHADOW.can_write_trades is False


def test_unsupported_state_backend_cannot_report_trading_ready() -> None:
    engine = object.__new__(AutonomousEngine)
    engine._state_backend_supported = False
    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "STATE_BACKEND_UNSUPPORTED")


def test_unknown_protection_config_freezes_new_risk_without_synthetic_defaults() -> None:
    from beidou_control.plane import ControlAction

    actions: list[ControlAction] = []
    incidents: list[tuple[object, ...]] = []
    engine = object.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        execute_action=lambda action: actions.append(action),
    )
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: incidents.append(args))
    config = SimpleNamespace(metadata={"blocked": True, "reason": "KLINE_UNKNOWN"}, stop_pct=0.0)

    with pytest.raises(RuntimeError, match="PROTECTION_CONFIG_UNKNOWN:BTCUSDT"):
        engine._require_protection_config("BTCUSDT", config)

    assert actions == [ControlAction.NO_NEW_RISK]
    assert engine._protection_config_unknown is True
    assert config.stop_pct == 0.0
    assert incidents


def test_factor_promotion_gate_never_allows_unverified_active() -> None:
    from beidou_research.factors.factor import FactorLifecycle, FactorPromotionGate

    gate = FactorPromotionGate(strict=False)
    decision = gate.validate_evidence(
        "factor-1",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        falsifier="test",
    )
    assert decision.approved is False
    assert "Missing required evidence" in decision.reason


def test_startup_wait_does_not_authorize_resume_with_runtime_blocker(tmp_path: Path) -> None:
    from beidou_launcher.supervisor import BeidouSupervisor

    supervisor = BeidouSupervisor(
        project_root=tmp_path,
        mode="testnet",
        symbols=["BTCUSDT"],
        port=19090,
        startup_timeout=0.02,
    )

    class _Thread:
        @staticmethod
        def is_alive() -> bool:
            return True

    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _health=SimpleNamespace(_thread=_Thread()),
    )
    blocker = CheckResult(
        "runtime.health.realtime_heartbeat",
        "实时循环心跳",
        CheckStatus.FAIL,
        CheckSeverity.P0,
        "stale",
    )
    supervisor._runtime_checks = lambda: [blocker]  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]

    async def _noop() -> None:
        return None

    supervisor._refresh_exchange_account_snapshot = _noop  # type: ignore[method-assign]
    supervisor._refresh_position_mode = _noop  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = _noop  # type: ignore[method-assign]

    async def _running_engine() -> None:
        await asyncio.sleep(1)

    async def _run() -> bool:
        supervisor._engine_task = asyncio.create_task(_running_engine())
        try:
            return await supervisor._wait_for_startup()
        finally:
            supervisor._engine_task.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor._engine_task

    assert asyncio.run(_run()) is False
    assert supervisor.report.blockers == [blocker]
    assert supervisor._resume_authorized is False


def test_persistent_blocker_cannot_leave_supervisor_running() -> None:
    from beidou_launcher.supervisor import _state_after_persistent_block

    assert _state_after_persistent_block("RUNNING", True) == "DEGRADED"
    assert _state_after_persistent_block("STARTING", True) == "DEGRADED"
    assert _state_after_persistent_block("DEGRADED", True) == "DEGRADED"
    assert _state_after_persistent_block("LOCKED", True) == "LOCKED"
    assert _state_after_persistent_block("RUNNING", False) == "RUNNING"
