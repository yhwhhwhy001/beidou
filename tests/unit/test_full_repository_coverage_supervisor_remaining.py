"""Behavioral coverage for supervisor safety and lifecycle boundaries."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlPlane
from beidou_launcher import supervisor as supervisor_module
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor


def _supervisor(tmp_path: Path, *, mode: str = "testnet") -> BeidouSupervisor:
    return BeidouSupervisor(project_root=tmp_path, mode=mode, symbols=["BTCUSDT"], port=19101)


@pytest.fixture(autouse=True)
def _restore_cwd():
    original = Path.cwd()
    yield
    os.chdir(original)


def _blocker(check_id: str = "runtime.safety.execution") -> CheckResult:
    return CheckResult(check_id, check_id, CheckStatus.FAIL, CheckSeverity.P0, "blocked")


def _check(
    check_id: str,
    status: CheckStatus = CheckStatus.PASS,
    severity: CheckSeverity = CheckSeverity.INFO,
    message: str | None = None,
) -> CheckResult:
    return CheckResult(check_id, check_id, status, severity, message or status.value)


class _Health:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self._metrics_collector = lambda: {"base": 1}

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            return lambda fn: self.callbacks.__setitem__(name, fn)
        raise AttributeError(name)


def test_write_interlock_allows_registered_testnet_unknown_only_path(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path)
    calls: list[tuple[str, str]] = []

    async def original_async(path, **kwargs):
        calls.append(("async", path))
        return {"async": kwargs["method"]}

    def original_sync(path, **kwargs):
        calls.append(("sync", path))
        return {"sync": kwargs["method"]}

    async def adapter_request(method, path, **kwargs):
        calls.append(("adapter", path))
        return {"adapter": method, "signed": kwargs["signed"]}

    supervisor.engine = SimpleNamespace(
        _api_async=original_async,
        _api=original_sync,
        _adapter=SimpleNamespace(request=adapter_request),
    )
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    supervisor._install_exchange_write_interlock()

    assert supervisor.engine._api("/futures/order", method="POST") == {"sync": "POST"}
    assert asyncio.run(supervisor.engine._api_async("/futures/order", method="POST")) == {"async": "POST"}
    assert asyncio.run(supervisor.engine._adapter.request("POST", "/futures/order", signed=True)) == {
        "adapter": "POST",
        "signed": True,
    }
    assert calls == [("sync", "/futures/order"), ("async", "/futures/order"), ("adapter", "/futures/order")]
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "hard")
    blocked_sync = supervisor.engine._api("/futures/order", method="POST")
    blocked_async = asyncio.run(supervisor.engine._api_async("/futures/order", method="POST"))
    blocked_adapter = asyncio.run(supervisor.engine._adapter.request("POST", "/futures/order"))
    assert blocked_sync["code"] == -3 and blocked_async["code"] == -3
    assert blocked_adapter.is_ok is False
    assert len(supervisor.engine._supervisor_blocked_writes) == 3


def test_control_incident_and_factor_callbacks_fail_closed_on_missing_or_broken_state(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, mode="paper")
    assert supervisor._control_state() == "UNKNOWN"
    assert supervisor._has_active_trading_incident() is False

    health = _Health()

    def broken_status():
        raise RuntimeError("control unavailable")

    def broken_incidents():
        raise RuntimeError("incident store unavailable")

    supervisor.engine = SimpleNamespace(
        _health=health,
        _control=SimpleNamespace(get_status=broken_status),
        _alerts=SimpleNamespace(get_active_incidents=broken_incidents),
        _factor_registry=None,
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _get_status_info=lambda: {},
    )
    assert supervisor._control_state() == "UNKNOWN"
    assert supervisor._has_active_trading_incident() is True
    supervisor._install_health_callbacks()
    assert health.callbacks["set_factor_provider"]() == []


def test_snapshot_refresh_throttles_and_records_invalid_position_evidence(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    assert asyncio.run(supervisor._refresh_exchange_account_snapshot()) is None
    assert asyncio.run(supervisor._refresh_position_mode()) is None
    assert asyncio.run(supervisor._refresh_exchange_algo_snapshot()) is None

    supervisor.engine = SimpleNamespace(_api_async=lambda *_args, **_kwargs: None)
    supervisor._last_exchange_account_probe = time.monotonic()
    supervisor._last_position_mode_probe = time.monotonic()
    supervisor._last_exchange_algo_probe = time.monotonic()
    supervisor._position_mode_evidence = SimpleNamespace(mode=SimpleNamespace(value="ONE_WAY"))
    assert asyncio.run(supervisor._refresh_exchange_account_snapshot()) is None
    assert asyncio.run(supervisor._refresh_position_mode()) is None
    assert asyncio.run(supervisor._refresh_exchange_algo_snapshot()) is None

    async def invalid_api(path, **_kwargs):
        if str(path).endswith("positionSide/dual"):
            return {"invalid": True}
        return {"invalid": True}

    supervisor.engine = SimpleNamespace(_api_async=invalid_api, _last_account=None)
    supervisor._last_exchange_account_probe = -100.0
    supervisor._last_position_mode_probe = -100.0
    supervisor._last_exchange_algo_probe = -100.0
    supervisor._position_mode_evidence = None
    asyncio.run(supervisor._refresh_exchange_account_snapshot())
    assert supervisor._exchange_account_snapshot["ok"] is False
    asyncio.run(supervisor._refresh_position_mode())
    assert supervisor._position_mode_evidence is not None
    assert supervisor._position_mode_evidence.mode.value == "UNKNOWN"
    asyncio.run(supervisor._refresh_exchange_algo_snapshot())
    assert supervisor._exchange_algo_snapshot["ok"] is False


def test_g7_producer_noop_and_monitor_scheduler_exception_are_safe(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor._record_g7_certification_evidence([])

    supervisor._g7_certification = SimpleNamespace(list_windows=lambda: [])
    supervisor._record_g7_certification_evidence([])
    supervisor._g7_certification = SimpleNamespace(
        list_windows=lambda: [{"window_id": "w1", "status": "RUNNING"}],
        get_window=lambda _window_id: None,
    )
    supervisor._record_g7_certification_evidence([])

    supervisor.engine = SimpleNamespace(
        _alerts=SimpleNamespace(retry_pending=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("alerts")))
    )
    supervisor.writer = SimpleNamespace(write_event=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        type(supervisor._monitoring_scheduler),
        "tick",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scheduler")),
    )
    monkeypatch.setattr(supervisor_module, "collect_monitoring_checks", lambda **_kwargs: [])
    assert supervisor._merge_monitoring_checks([]) == []
    assert supervisor_module._state_after_persistent_block("RUNNING", True) == "DEGRADED"
    assert supervisor_module._state_after_persistent_block("LOCKED", True) == "LOCKED"
    assert supervisor_module._state_after_persistent_block("RUNNING", False) == "RUNNING"

    async def broken_snapshot() -> None:
        raise RuntimeError("snapshot")

    asyncio.run(supervisor_module._refresh_snapshot_safe(broken_snapshot()))
    check = _blocker("preflight.g5_certificate")
    downgraded = supervisor_module._apply_g5_dev_exemption([check], "testnet", True)
    assert downgraded[0].severity is CheckSeverity.P2
    assert supervisor_module._apply_g5_dev_exemption([check], "paper", True)[0].severity is CheckSeverity.P0


def test_g7_producer_records_sli_batch_and_deduplicates_incidents(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    captured: dict[str, object] = {"batches": [], "incidents": [], "reports": []}
    window = SimpleNamespace(evidence_state_complete=True, total_recovery_count=0)
    supervisor._g7_certification = SimpleNamespace(
        list_windows=lambda: [
            {"window_id": "window-a", "status": "RUNNING"},
            {"window_id": "window-b", "status": "RUNNING"},
        ],
        get_window=lambda _window_id: window,
        record_batch_sli=lambda window_id, samples: captured["batches"].append((window_id, samples)),
        open_incident=lambda window_id, incident: captured["incidents"].append((window_id, incident.incident_id)),
        generate_daily_report=lambda window_id: captured["reports"].append(window_id),
    )
    checks = [
        _check("runtime.health.market_data"),
        _check("runtime.execution.order_trace", message="DUPLICATE observed"),
        _check("runtime.safety.protection_coverage"),
        _check("runtime.safety.reconciliation"),
        _check("runtime.health.incidents"),
        _check("runtime.safety.cost_and_pnl_reporting"),
        _blocker("runtime.g7.blocker"),
    ]
    supervisor._record_g7_certification_evidence(checks)
    supervisor._record_g7_certification_evidence(checks)
    assert captured["batches"]
    assert captured["batches"][0][0] == "window-b"
    assert len(captured["batches"][0][1]) == 7
    assert captured["incidents"] == [("window-b", "g7-window-b-runtime.g7.blocker")]
    assert captured["reports"] == ["window-b", "window-b"]
    assert window.total_recovery_count == supervisor._recovery_count


def test_merge_monitoring_checks_converts_monitoring_exception_to_p0(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.engine = SimpleNamespace(_alerts=SimpleNamespace())
    supervisor.writer = SimpleNamespace(write_event=lambda *_args, **_kwargs: None)

    def broken_checks(**_kwargs):
        raise RuntimeError("monitor unavailable")

    monkeypatch.setattr(supervisor_module, "collect_monitoring_checks", broken_checks)
    checks = supervisor._merge_monitoring_checks([])
    assert len(checks) == 1
    assert checks[0].check_id == "runtime.monitoring.execution"
    assert checks[0].severity is CheckSeverity.P0


def test_wait_for_startup_handles_done_cancelled_and_probe_or_check_errors(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _health=SimpleNamespace(_thread=SimpleNamespace(is_alive=lambda: True)),
    )

    async def cancelled_case() -> bool:
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        supervisor._engine_task = task
        return await supervisor._wait_for_startup()

    assert asyncio.run(cancelled_case()) is False
    assert supervisor._engine_failure == "engine task cancelled"

    async def raising_engine() -> None:
        raise RuntimeError("engine failed")

    async def failed_case() -> bool:
        task = asyncio.create_task(raising_engine())
        supervisor._engine_task = task
        return await supervisor._wait_for_startup()

    assert asyncio.run(failed_case()) is False
    assert "engine failed" in supervisor._engine_failure

    async def probe_error(*_args, **_kwargs):
        raise RuntimeError("probe")

    async def noop() -> None:
        return None

    supervisor._engine_failure = ""
    supervisor._algorithm_probe = {}
    supervisor._wait_for_startup = supervisor._wait_for_startup  # keep instance method explicit for coverage tools
    monkeypatch.setattr(supervisor_module, "run_read_only_algorithm_probe", probe_error)
    supervisor._refresh_exchange_account_snapshot = noop  # type: ignore[method-assign]
    supervisor._refresh_position_mode = noop  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = noop  # type: ignore[method-assign]
    supervisor._runtime_checks = lambda: []  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor.writer = SimpleNamespace(write=lambda _report: None)

    async def active_case() -> bool:
        task = asyncio.create_task(asyncio.sleep(0.05))
        supervisor._engine_task = task
        supervisor.startup_timeout = 1.0
        result = await supervisor._wait_for_startup()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return result

    assert asyncio.run(active_case()) is True

    supervisor._algorithm_probe = {"ok": True}
    supervisor._runtime_checks = lambda: (_ for _ in ()).throw(RuntimeError("checks"))  # type: ignore[method-assign]
    assert asyncio.run(active_case()) is True

    supervisor._shutdown_requested = True
    supervisor._algorithm_probe = {"ok": True}
    assert asyncio.run(active_case()) is False

    supervisor._shutdown_requested = False
    supervisor._algorithm_probe = {}
    supervisor._last_algorithm_probe_attempt = 0.0
    assert asyncio.run(active_case()) is True


def test_alert_resolution_and_debounce_state_transitions_are_behavioral(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    sent: list[str] = []
    supervisor.engine = SimpleNamespace(
        _alerts=SimpleNamespace(send_incident=lambda **kwargs: sent.append(kwargs["title"])),
    )
    supervisor._send_supervisor_alert("DEGRADED", [_blocker()])
    assert sent == ["Supervisor DEGRADED"]
    supervisor.engine._alerts.send_incident = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("alert"))
    supervisor._send_supervisor_alert("LOCKED", [_blocker()])

    resolved: list[str] = []
    supervisor.engine._alerts = SimpleNamespace(
        _active_incidents={
            "supervisor-1": SimpleNamespace(root_cause_category="supervisor"),
            "other": SimpleNamespace(root_cause_category="execution"),
        },
        resolve_incident=lambda incident_id: resolved.append(incident_id),
    )
    supervisor._resolve_supervisor_incidents()

    class BrokenActiveIncidents:
        @property
        def _active_incidents(self):
            raise RuntimeError("active incident read")

    supervisor.engine._alerts = BrokenActiveIncidents()
    supervisor._resolve_supervisor_incidents()
    assert resolved == ["supervisor-1"]

    supervisor.engine._alerts = SimpleNamespace(
        _active_incidents={"supervisor-2": SimpleNamespace(root_cause_category="supervisor")},
        resolve_incident=lambda _incident_id: (_ for _ in ()).throw(RuntimeError("resolve")),
    )
    supervisor._resolve_supervisor_incidents()
    supervisor.engine._alerts = None
    supervisor._resolve_supervisor_incidents()
    supervisor.engine = None
    supervisor._resolve_supervisor_incidents()
    supervisor.engine = SimpleNamespace(_control=SimpleNamespace(execute_action=lambda _action: None))

    failed_closed: list[tuple[str, bool]] = []
    alerts: list[str] = []

    async def fail_closed(reason, fatal=False):
        failed_closed.append((reason, fatal))

    supervisor._fail_closed = fail_closed  # type: ignore[method-assign]
    supervisor._send_supervisor_alert = lambda state, _blockers: alerts.append(state)  # type: ignore[method-assign]
    supervisor.report.supervisor_state = "RUNNING"
    asyncio.run(supervisor._apply_debounce_action("LOCKED", [_blocker()], True))
    assert supervisor.report.supervisor_state == "LOCKED"
    assert failed_closed and failed_closed[-1][1] is True

    supervisor.report.supervisor_state = "RUNNING"
    asyncio.run(supervisor._apply_debounce_action("DEGRADED", [_blocker("one")], True))
    assert supervisor.report.supervisor_state == "DEGRADED"
    supervisor._resume_authorized = False
    supervisor._control_state = lambda: "RESUME"  # type: ignore[method-assign]
    supervisor.engine._control = SimpleNamespace(
        execute_action=lambda action: alerts.append(getattr(action, "value", str(action)))
    )
    asyncio.run(supervisor._apply_debounce_action("DEGRADED", [_blocker("two")], True))
    assert "DEGRADED" in alerts

    supervisor._control_state = lambda: "NO_NEW_RISK"  # type: ignore[method-assign]
    supervisor._resolve_supervisor_incidents = lambda: None  # type: ignore[method-assign]
    supervisor.mode = "paper"
    asyncio.run(supervisor._apply_debounce_action("RUNNING", [], False))
    assert supervisor.report.supervisor_state == "PAUSED"
    supervisor._control_state = lambda: "RESUME"  # type: ignore[method-assign]
    asyncio.run(supervisor._apply_debounce_action("RUNNING", [], False))
    assert supervisor.report.supervisor_state == "RUNNING"
    asyncio.run(supervisor._apply_debounce_action("UNCHANGED", [_blocker()], True))
    assert failed_closed[-1][1] is False


def test_stale_supervisor_incident_is_cleared_before_debounce_when_it_is_the_only_blocker(
    tmp_path: Path,
) -> None:
    """A recovered runtime must not self-lock on its own stale alert."""
    supervisor = _supervisor(tmp_path)
    resolved: list[str] = []
    incident = SimpleNamespace(root_cause_category="supervisor", incident_id="supervisor-degraded")
    supervisor.engine = SimpleNamespace(
        _alerts=SimpleNamespace(
            get_active_incidents=lambda: [
                {
                    "incident_id": incident.incident_id,
                    "severity": "HIGH",
                    "title": "Supervisor DEGRADED",
                    "status": "DETECTED",
                }
            ],
            _active_incidents={incident.incident_id: incident},
            resolve_incident=lambda incident_id: resolved.append(incident_id),
        )
    )
    supervisor.report.supervisor_state = "DEGRADED"

    checks = [_blocker("runtime.health.incidents")]

    assert supervisor._resolve_stale_supervisor_incidents_before_debounce(checks) is True
    assert resolved == ["supervisor-degraded"]

    real_incident = SimpleNamespace(root_cause_category="reconciliation", incident_id="reconciliation-blocked")
    supervisor.engine._alerts.get_active_incidents = lambda: [real_incident]
    supervisor.engine._alerts._active_incidents = {real_incident.incident_id: real_incident}
    assert supervisor._resolve_stale_supervisor_incidents_before_debounce(checks) is False
    assert resolved == ["supervisor-degraded"]


def test_fail_closed_and_recovery_invalid_lifecycle_edges(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    assert asyncio.run(supervisor._fail_closed("no engine", fatal=False)) is None
    supervisor.engine = SimpleNamespace(
        _control=SimpleNamespace(
            get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"),
            execute_action=lambda _action: None,
        ),
        _lifecycle=SimpleNamespace(
            state=SimpleNamespace(value="ACTIVE"),
            transition=lambda target: SimpleNamespace(value="SUCCESS"),
        ),
        _running=True,
    )
    asyncio.run(supervisor._fail_closed("degrade", fatal=False))
    assert supervisor.engine._lifecycle.state.value == "ACTIVE"
    supervisor._resume_authorized = True
    supervisor.engine._lifecycle.state = SimpleNamespace(value="FAILED")
    assert asyncio.run(supervisor._recover_if_validated([])) is False


def test_recovery_rejects_missing_authority_limits_and_bad_transitions(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    assert asyncio.run(supervisor._recover_if_validated([])) is False
    supervisor.engine = SimpleNamespace(_lifecycle=SimpleNamespace(state=SimpleNamespace(value="DEGRADED")))
    assert asyncio.run(supervisor._recover_if_validated([])) is False
    supervisor._resume_authorized = True
    supervisor._recovery_timestamps = [time.monotonic()] * supervisor.max_restarts
    assert asyncio.run(supervisor._recover_if_validated([])) is False
    supervisor._recovery_timestamps = []
    assert asyncio.run(supervisor._recover_if_validated([_blocker()])) is False

    class BadLifecycle:
        state = SimpleNamespace(value="DEGRADED")

        def transition(self, _target):
            return SimpleNamespace(value="FAIL")

    supervisor.engine = SimpleNamespace(
        _lifecycle=BadLifecycle(),
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK")),
    )
    assert asyncio.run(supervisor._recover_if_validated([])) is False


def test_monitor_records_g7_persistence_failure_and_engine_terminal_states(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.monitor_interval = 0.001
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="LOCKED")),
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="NO_NEW_RISK")),
    )
    supervisor.writer = SimpleNamespace(write=lambda _report: None)
    supervisor._runtime_checks = lambda: []  # type: ignore[method-assign]
    supervisor._merge_monitoring_checks = lambda checks: checks  # type: ignore[method-assign]
    supervisor._refresh_exchange_account_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_position_mode = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._record_g7_certification_evidence = lambda _checks: (_ for _ in ()).throw(RuntimeError("persist"))  # type: ignore[method-assign]
    supervisor._apply_debounce_action = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._health_debounce.feed = lambda _value, **_kwargs: None  # A/B 分流后带 repairable= 关键字
    supervisor._is_trading_ready = lambda: False  # type: ignore[method-assign]
    supervisor._g7_tracker = SimpleNamespace(
        set_durable_window_state=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("tracker")),
        feed=lambda _checks: None,
        feed_recovery_context=lambda *_args: None,
    )

    async def locked_case() -> int:
        supervisor._engine_task = asyncio.create_task(asyncio.sleep(0.01))
        return await supervisor._monitor()

    assert asyncio.run(locked_case()) == 5

    async def cancelled_case() -> int:
        task = asyncio.create_task(asyncio.sleep(10))
        task.cancel()
        supervisor.engine._lifecycle.state = SimpleNamespace(value="ACTIVE")
        supervisor._engine_task = task
        return await supervisor._monitor()

    assert asyncio.run(cancelled_case()) == 6

    async def failing_engine() -> None:
        raise RuntimeError("engine crash")

    async def failed_case() -> int:
        supervisor.engine._lifecycle.state = SimpleNamespace(value="ACTIVE")
        supervisor._engine_task = asyncio.create_task(failing_engine())
        return await supervisor._monitor()

    assert asyncio.run(failed_case()) == 6


class _RunHealth:
    def __init__(self) -> None:
        self._port = 0
        self._metrics_collector = lambda: {}

    def __getattr__(self, name: str):
        if name.startswith("set_"):
            return lambda _fn: None
        raise AttributeError(name)


class _RunEngine:
    def __init__(self, *, symbols, mode) -> None:
        self.symbols = symbols
        self.mode = mode
        self._health = _RunHealth()
        self._control = ControlPlane()
        self._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={})
        self._api = lambda *_args, **_kwargs: {}
        self._adapter = SimpleNamespace(request=lambda *_args, **_kwargs: asyncio.sleep(0, result=None))
        self._factor_registry = None
        self._alerts = SimpleNamespace()
        self._lifecycle = SimpleNamespace(state=SimpleNamespace(value="ACTIVE"))
        self._running = True

    async def run(self) -> None:
        return None


def _prepare_run_supervisor(tmp_path: Path, *, mode: str = "paper") -> BeidouSupervisor:
    tmp_path.mkdir(parents=True, exist_ok=True)
    supervisor = _supervisor(tmp_path, mode=mode)
    supervisor.writer = SimpleNamespace(write=lambda _report: None, state_path=tmp_path / "state.json")
    supervisor.lock = SimpleNamespace(acquire=lambda: (True, "locked"), release=lambda: None)
    return supervisor


def test_run_covers_lock_block_construction_block_and_safe_shutdown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])
    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", _RunEngine)

    locked = _prepare_run_supervisor(tmp_path / "locked")
    locked.lock = SimpleNamespace(acquire=lambda: (False, "already running"), release=lambda: None)
    assert asyncio.run(locked.run()) == 3

    construction_block = _prepare_run_supervisor(tmp_path / "construction")
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [_blocker("wiring")])
    assert asyncio.run(construction_block.run()) == 2

    shutdown = _prepare_run_supervisor(tmp_path / "shutdown")
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])
    shutdown._wait_for_startup = lambda: asyncio.sleep(0, result=False)  # type: ignore[method-assign]
    shutdown._shutdown_requested = True
    assert asyncio.run(shutdown.run()) == 0


def test_run_preflight_block_is_observable_and_does_not_start(tmp_path: Path, monkeypatch) -> None:
    supervisor = _prepare_run_supervisor(tmp_path / "preflight")
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([_blocker("preflight")], None))
    assert asyncio.run(supervisor.run()) == 2
    assert supervisor.report.supervisor_state == "BLOCKED"


def test_run_covers_paper_bootstrap_failure_authorization_and_monitor_return(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])
    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", _RunEngine)
    original_sleep = asyncio.sleep
    monkeypatch.setattr(supervisor_module.asyncio, "sleep", lambda *_args: original_sleep(0))
    monkeypatch.setattr(
        "beidou_bootstrap.dev.patch_engine_for_dev",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("dev patch")),
    )
    monkeypatch.setattr(
        "beidou_bootstrap.dev.bootstrap_universe",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("universe")),
    )

    supervisor = _prepare_run_supervisor(tmp_path / "paper-success")

    async def ready() -> bool:
        return True

    async def monitored() -> int:
        return 7

    supervisor._wait_for_startup = ready  # type: ignore[method-assign]
    supervisor._authorize_resume_via_truth_snapshot = lambda: (True, "test authorization")  # type: ignore[method-assign]
    supervisor._control_state = lambda: "NO_NEW_RISK"  # type: ignore[method-assign]
    supervisor._is_trading_ready = lambda: True  # type: ignore[method-assign]
    supervisor._monitor = monitored  # type: ignore[method-assign]
    assert asyncio.run(supervisor.run()) == 7
    assert supervisor._resume_authorized is True
    assert supervisor.report.supervisor_state == "RUNNING"


def test_run_invokes_signal_shutdown_and_defers_resume_authorization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])
    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", _RunEngine)
    callbacks: list[object] = []

    class Loop:
        def add_signal_handler(self, _sig, callback):
            callbacks.append(callback)
            callback()

    monkeypatch.setattr(supervisor_module.asyncio, "get_running_loop", lambda: Loop())
    supervisor = _prepare_run_supervisor(tmp_path / "deferred", mode="testnet")

    async def ready() -> bool:
        return True

    async def monitored() -> int:
        return 0

    supervisor._wait_for_startup = ready  # type: ignore[method-assign]
    supervisor._authorize_resume_via_truth_snapshot = lambda: (False, "snapshot unavailable")  # type: ignore[method-assign]
    supervisor._monitor = monitored  # type: ignore[method-assign]
    assert asyncio.run(supervisor.run()) == 0
    assert len(callbacks) == 2
    assert supervisor._shutdown_requested is True
    assert supervisor._resume_authorized is False


def test_run_cancels_pending_engine_in_finally_after_wait_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])

    class PendingEngine(_RunEngine):
        async def run(self) -> None:
            await asyncio.sleep(100)

    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", PendingEngine)
    supervisor = _prepare_run_supervisor(tmp_path / "cancel-pending", mode="testnet")

    async def ready() -> bool:
        return True

    async def monitored() -> int:
        return 0

    async def immediate_timeout(*_args, **_kwargs):
        raise TimeoutError("test timeout")

    supervisor._wait_for_startup = ready  # type: ignore[method-assign]
    supervisor._authorize_resume_via_truth_snapshot = lambda: (False, "not authorized")  # type: ignore[method-assign]
    supervisor._monitor = monitored  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor_module.asyncio, "wait_for", immediate_timeout)
    assert asyncio.run(supervisor.run()) == 0


def test_run_covers_deep_startup_failure_and_fail_closed_return(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor_module, "run_preflight", lambda *_args: ([], None))
    monkeypatch.setattr(supervisor_module, "inspect_engine_wiring", lambda *_args: [])
    monkeypatch.setattr("beidou_core.engine.AutonomousEngine", _RunEngine)
    supervisor = _prepare_run_supervisor(tmp_path / "startup-failed", mode="testnet")
    supervisor._wait_for_startup = lambda: asyncio.sleep(0, result=False)  # type: ignore[method-assign]

    async def fail_closed(reason: str, fatal: bool = False) -> None:
        supervisor.engine._running = False
        supervisor._engine_failure = reason
        assert fatal is True

    supervisor._fail_closed = fail_closed  # type: ignore[method-assign]
    assert asyncio.run(supervisor.run()) == 4
    assert supervisor.report.supervisor_state == "FAILED"
