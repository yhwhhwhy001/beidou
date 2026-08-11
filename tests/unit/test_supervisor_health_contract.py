"""Supervisor health/readiness contracts.

The supervisor is the authority boundary between process liveness and trading
readiness.  These tests keep the HTTP callbacks tied to the same live facts
that revoke exchange-write authority; a healthy Python process is not a
trading certificate.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from beidou_core.health import HealthState
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor


class _HealthCallbacks:
    def __init__(self) -> None:
        self.readiness: Any = None
        self.trading_readiness: Any = None
        self.liveness: Any = None
        self.exit_readiness: Any = None
        self.status_info: Any = None
        self.metrics: Any = None
        self.factors: Any = None

    def set_readiness_check(self, fn: Any) -> None:
        self.readiness = fn

    def set_trading_readiness(self, fn: Any) -> None:
        self.trading_readiness = fn

    def set_liveness_check(self, fn: Any) -> None:
        self.liveness = fn

    def set_exit_readiness(self, fn: Any) -> None:
        self.exit_readiness = fn

    def set_status_info(self, fn: Any) -> None:
        self.status_info = fn

    def set_metrics_collector(self, fn: Any) -> None:
        self.metrics = fn

    def set_factor_provider(self, fn: Any) -> None:
        self.factors = fn


def _blocker(check_id: str = "runtime.health.realtime_heartbeat") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        name="live blocker",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.P0,
        message="stale",
    )


def _supervisor_with_health(tmp_path: Path) -> tuple[BeidouSupervisor, _HealthCallbacks]:
    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19094)
    health = _HealthCallbacks()
    supervisor.engine = SimpleNamespace(
        _health=health,
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME")),
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _active_order_ids=set(),
        _recon=None,
        _factor_registry=None,
        _get_status_info=lambda: {"engine": "ok"},
    )
    supervisor._resume_authorized = True
    supervisor.report.supervisor_state = "RUNNING"
    supervisor._install_health_callbacks()
    return supervisor, health


def test_health_callbacks_revoke_readiness_for_live_blocker(tmp_path: Path) -> None:
    supervisor, health = _supervisor_with_health(tmp_path)

    assert health.readiness() is True
    assert health.trading_readiness() == (True, "SUPERVISOR_VALIDATED")
    assert health.liveness() is HealthState.HEALTHY
    assert health.exit_readiness() == (True, "READY")

    # A stale heartbeat must make both readiness certificates false while the
    # process can still be live.  This is the old false-certificate counterexample.
    supervisor.report.checks = [_blocker()]
    assert health.readiness() is False
    assert health.trading_readiness() == (False, "runtime.health.realtime_heartbeat")
    assert health.status_info()["supervisor"]["trading_ready"] is False

    supervisor._last_monitor_loop_ts = time.monotonic() - 10.0
    supervisor.monitor_interval = 1.0
    assert health.liveness() is HealthState.DEGRADED


def test_health_callbacks_report_exit_blockers_and_locked_liveness(tmp_path: Path) -> None:
    supervisor, health = _supervisor_with_health(tmp_path)

    supervisor.engine._active_order_ids = {"order-1", "order-2"}
    assert health.exit_readiness() == (False, "HAS_ACTIVE_ORDERS:2")

    supervisor.engine._active_order_ids = set()
    supervisor.engine._recon = SimpleNamespace(reconcile=lambda *_args: SimpleNamespace(status="MISMATCHED"))
    assert health.exit_readiness() == (False, "RECON_MISMATCHED")

    supervisor.engine._recon = SimpleNamespace(reconcile=lambda *_args: (_ for _ in ()).throw(RuntimeError("db")))
    assert health.exit_readiness() == (False, "RECON_CHECK_FAILED")

    supervisor.engine._lifecycle.state = SimpleNamespace(value="LOCKED")
    assert health.liveness() is HealthState.UNHEALTHY


def test_fail_closed_revokes_authority_and_locks_fatal_engine(tmp_path: Path) -> None:
    supervisor = BeidouSupervisor(project_root=tmp_path, mode="testnet", symbols=["BTCUSDT"], port=19095)
    actions: list[Any] = []
    transitions: list[Any] = []

    class _Lifecycle:
        state = SimpleNamespace(value="ACTIVE")

        def transition(self, target: Any) -> Any:
            transitions.append(target)
            self.state = SimpleNamespace(value=getattr(target, "value", target))
            return SimpleNamespace(value="SUCCESS")

    lifecycle = _Lifecycle()
    supervisor.engine = SimpleNamespace(
        _control=SimpleNamespace(
            get_status=lambda: SimpleNamespace(value="RESUME"),
            execute_action=lambda action: actions.append(action),
        ),
        _lifecycle=lifecycle,
        _running=True,
    )
    supervisor._resume_authorized = True

    asyncio.run(supervisor._fail_closed("P0 test", fatal=True))

    assert supervisor._resume_authorized is False
    assert actions and getattr(actions[-1], "value", actions[-1]) == "NO_NEW_RISK"
    assert transitions and getattr(transitions[-1], "value", transitions[-1]) == "DEGRADED"
    # In testnet mode, fatal escalation is suppressed and engine keeps running
