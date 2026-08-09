"""V3 fail-closed regression tests.

These tests intentionally exercise the public safety seams instead of private
exchange calls.  They are the first proof slice for BD-V3-01/BD-V3-03.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beidou_core.health import HealthServer
from beidou_launcher.g7_tracker import G7LiveTracker
from beidou_launcher.models import CheckSeverity, CheckStatus
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
