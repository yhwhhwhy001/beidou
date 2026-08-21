"""Supervisor evidence, snapshot and lifecycle boundary coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_core.health import HealthState
from beidou_launcher import supervisor as supervisor_module
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
from beidou_launcher.supervisor import BeidouSupervisor
from beidou_observability.monitoring.contracts import AccountPositionMode


class _Health:
    def __init__(self) -> None:
        self.metrics = None
        self.readiness = None
        self.trading = None
        self.liveness = None
        self.exit = None
        self.status = None
        self.factors = None
        self._metrics_collector = lambda: {"engine_metric": 1}

    def set_metrics_collector(self, fn):
        self.metrics = fn

    def set_readiness_check(self, fn):
        self.readiness = fn

    def set_trading_readiness(self, fn):
        self.trading = fn

    def set_liveness_check(self, fn):
        self.liveness = fn

    def set_exit_readiness(self, fn):
        self.exit = fn

    def set_status_info(self, fn):
        self.status = fn

    def set_factor_provider(self, fn):
        self.factors = fn


def _supervisor(tmp_path: Path, *, mode: str = "testnet") -> BeidouSupervisor:
    return BeidouSupervisor(project_root=tmp_path, mode=mode, symbols=["BTCUSDT"], port=19099)


def _check(check_id: str, status: CheckStatus = CheckStatus.PASS, severity: CheckSeverity = CheckSeverity.INFO):
    return CheckResult(check_id, check_id, status, severity, status.value)


def test_exchange_snapshot_probes_accept_readback_and_use_cached_fallback(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    responses = [
        {"totalWalletBalance": "100", "positions": []},
        RuntimeError("timeout"),
        RuntimeError("timeout-no-cache"),
    ]

    async def api(_path, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    supervisor.engine = SimpleNamespace(_api_async=api, _last_account={"totalWalletBalance": "90"})
    supervisor._last_exchange_account_probe = -100.0
    asyncio.run(supervisor._refresh_exchange_account_snapshot())
    assert supervisor._exchange_account_snapshot["ok"] is True
    assert "account" in supervisor._exchange_account_snapshot

    supervisor._last_exchange_account_probe = -100.0
    asyncio.run(supervisor._refresh_exchange_account_snapshot())
    assert supervisor._exchange_account_snapshot["source"] == "cached_fallback"

    supervisor.engine._last_account = None
    supervisor._last_exchange_account_probe = -100.0
    asyncio.run(supervisor._refresh_exchange_account_snapshot())
    assert supervisor._exchange_account_snapshot["ok"] is False
    assert "RuntimeError" in supervisor._exchange_account_snapshot["error"]


def test_position_mode_and_algo_snapshots_preserve_unknown_and_record_changes(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    responses = [
        {"dualSidePosition": False},
        {"dualSidePosition": True},
        {"bad": True},
        [{"symbol": "BTCUSDT", "algoId": "a1"}, {"symbol": "", "algoId": "ignored"}],
        {"error": "unavailable"},
    ]

    async def api(path, **_kwargs):
        response = responses.pop(0)
        if path.endswith("positionSide/dual"):
            return response
        return response

    supervisor.engine = SimpleNamespace(_api_async=api)
    supervisor._last_position_mode_probe = -100.0
    asyncio.run(supervisor._refresh_position_mode())
    assert supervisor._position_mode_evidence is not None
    assert supervisor._position_mode_evidence.mode is AccountPositionMode.ONE_WAY

    supervisor._last_position_mode_probe = -100.0
    asyncio.run(supervisor._refresh_position_mode())
    assert supervisor._position_mode_evidence.mode is AccountPositionMode.HEDGE

    supervisor._last_position_mode_probe = -100.0
    asyncio.run(supervisor._refresh_position_mode())
    assert supervisor._position_mode_evidence.mode is AccountPositionMode.HEDGE

    supervisor._last_exchange_algo_probe = -100.0
    asyncio.run(supervisor._refresh_exchange_algo_snapshot(force=True))
    assert supervisor._exchange_algo_snapshot["ok"] is True
    assert supervisor._exchange_algo_snapshot["by_symbol"] == {"BTCUSDT": ["a1"]}
    supervisor._last_exchange_algo_probe = -100.0
    asyncio.run(supervisor._refresh_exchange_algo_snapshot(force=True))
    assert supervisor._exchange_algo_snapshot["ok"] is False


def test_health_callbacks_expose_factors_metrics_and_g7_state(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, mode="paper")
    health = _Health()
    supervisor.engine = SimpleNamespace(
        _health=health,
        _control=SimpleNamespace(get_status=lambda: SimpleNamespace(value="RESUME")),
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _active_order_ids=set(),
        _recon=None,
        _factor_registry=SimpleNamespace(
            _factors={"factor-a": SimpleNamespace(lifecycle=SimpleNamespace(value="ACTIVE"))}
        ),
        _get_status_info=lambda: {"engine": "ok"},
    )
    supervisor._resume_authorized = True
    supervisor.report.supervisor_state = "RUNNING"
    supervisor.report.checks = [_check("runtime.test", CheckStatus.UNKNOWN, CheckSeverity.P2)]
    supervisor._install_health_callbacks()

    assert health.factors() == [{"factor_id": "factor-a", "lifecycle": "ACTIVE"}]
    metrics = health.metrics()
    assert metrics["engine_metric"] == 1
    assert metrics["check_runtime_test"] == 3
    assert metrics["supervisor_state"] == 0
    assert "g7_overall_pass_rate" in metrics
    assert health.status()["supervisor"]["g7_certification"]["producer_status"] == "HARD_HOLD"
    assert health.liveness() is HealthState.HEALTHY

    supervisor._g7_certification = SimpleNamespace(
        list_windows=lambda: [
            {"window_id": "old", "status": "COMPLETED"},
            {"window_id": "run", "status": "RUNNING"},
            {"window_id": "created", "status": "CREATED"},
        ],
        state_load_errors=["old-state"],
    )
    summary = supervisor._g7_certification_summary()
    assert [item["window_id"] for item in summary["active_windows"]] == ["run", "created"]
    assert summary["state_load_errors"] == ["old-state"]
    supervisor._g7_certification = SimpleNamespace(list_windows=lambda: (_ for _ in ()).throw(RuntimeError("state")))
    assert supervisor._g7_certification_summary()["state_load_errors"] == ["RuntimeError"]


def test_runtime_and_monitoring_checks_preserve_external_fact_failures(tmp_path: Path, monkeypatch) -> None:
    supervisor = _supervisor(tmp_path)
    retry_calls: list[int] = []
    supervisor.engine = SimpleNamespace(
        _alerts=SimpleNamespace(retry_pending=lambda max_items: retry_calls.append(max_items))
    )
    supervisor._algorithm_probe = {"ok": True}
    captured = {}

    def runtime(**kwargs):
        captured.update(kwargs)
        return [_check("runtime.internal")], 4

    monkeypatch.setattr(supervisor_module, "collect_runtime_checks", runtime)
    checks = supervisor._runtime_checks()
    assert checks[0].check_id == "runtime.internal"
    assert supervisor._last_error_count == 4
    assert captured["algorithm_probe"] == {"ok": True}

    emitted: list[tuple[str, dict]] = []
    supervisor.writer = SimpleNamespace(write_event=lambda event, payload: emitted.append((event, payload)))
    external = _check("runtime.internal", CheckStatus.FAIL, CheckSeverity.P0)
    duplicate = _check("runtime.internal", CheckStatus.PASS)
    monkeypatch.setattr(supervisor_module, "collect_monitoring_checks", lambda **_kwargs: [external])
    merged = supervisor._merge_monitoring_checks([duplicate, _check("runtime.other")])
    assert [item.check_id for item in merged] == ["runtime.other", "runtime.internal"]
    assert retry_calls == [1]
    assert emitted and emitted[0][0] == "monitoring_check_fail"


def test_truth_snapshot_authorization_and_recovery_are_fail_closed(tmp_path: Path) -> None:
    for mode in ("paper", "shadow", "research", "safety_only"):
        supervisor = _supervisor(tmp_path / mode, mode=mode)
        assert supervisor._authorize_resume_via_truth_snapshot()[0] is True

    supervisor = _supervisor(tmp_path / "none", mode="testnet")
    assert supervisor._authorize_resume_via_truth_snapshot() == (False, "engine not initialized")
    supervisor.engine = SimpleNamespace(
        build_truth_snapshot=lambda: "snapshot",
        _control=SimpleNamespace(authorize_resume=lambda snap: (snap == "snapshot", "accepted")),
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="DEGRADED")),
        _control_state=lambda: "NO_NEW_RISK",
    )
    assert supervisor._authorize_resume_via_truth_snapshot() == (True, "accepted")

    transitions: list[str] = []

    class Lifecycle:
        state = SimpleNamespace(value="DEGRADED")

        def transition(self, target):
            transitions.append(target.value)
            self.state = SimpleNamespace(value=target.value)
            return SimpleNamespace(value="SUCCESS")

    supervisor.engine = SimpleNamespace(
        _lifecycle=Lifecycle(),
        _control=SimpleNamespace(
            get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"),
            execute_action=lambda action: transitions.append(getattr(action, "value", str(action))),
        ),
    )
    supervisor._resume_authorized = True
    assert asyncio.run(
        supervisor._recover_if_validated([_check("runtime.safety.user_stream", CheckStatus.FAIL, CheckSeverity.P0)])
    )
    assert transitions[:3] == ["RECOVERING", "VALIDATING", "ACTIVE"]
    assert "RESUME" in transitions


def test_testnet_active_auto_reauthorization_and_transition_failure(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    actions: list[str] = []
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _control=SimpleNamespace(
            get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"),
            execute_action=lambda action: actions.append(getattr(action, "value", str(action))),
        ),
    )
    assert supervisor._maybe_testnet_auto_reauthorize() is True
    assert supervisor._resume_authorized is True
    assert actions == ["RESUME"]

    class FailingLifecycle:
        state = SimpleNamespace(value="DEGRADED")

        def transition(self, _target):
            return SimpleNamespace(value="FAIL")

    supervisor2 = _supervisor(tmp_path / "failing")
    supervisor2.engine = SimpleNamespace(
        _lifecycle=FailingLifecycle(),
        _control=SimpleNamespace(
            get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"),
            execute_action=lambda _action: None,
        ),
    )
    assert supervisor2._maybe_testnet_auto_reauthorize() is False
    assert supervisor2._resume_authorized is False


def test_wait_for_startup_handles_engine_failure_starting_and_validated_paths(tmp_path: Path) -> None:
    async def failing():
        raise RuntimeError("engine boom")

    supervisor = _supervisor(tmp_path / "failure")
    supervisor.engine = SimpleNamespace()

    async def failure_scenario() -> bool:
        supervisor._engine_task = asyncio.create_task(failing())
        return await supervisor._wait_for_startup()

    assert asyncio.run(failure_scenario()) is False
    assert "RuntimeError" in supervisor._engine_failure

    async def pending():
        await asyncio.sleep(10)

    supervisor = _supervisor(tmp_path / "starting")
    supervisor.startup_timeout = 0.01
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="STARTING")), _health=None
    )

    async def starting_scenario() -> bool:
        supervisor._engine_task = asyncio.create_task(pending())
        try:
            return await supervisor._wait_for_startup()
        finally:
            supervisor._engine_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor._engine_task

    assert asyncio.run(starting_scenario()) is False

    class Thread:
        def is_alive(self):
            return True

    supervisor = _supervisor(tmp_path / "ready")
    supervisor.startup_timeout = 0.1
    supervisor.engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _health=SimpleNamespace(_thread=Thread()),
    )
    supervisor._algorithm_probe = {"ok": True}
    supervisor._runtime_checks = lambda: []
    supervisor._merge_monitoring_checks = lambda checks: checks
    supervisor._refresh_exchange_account_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_position_mode = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    supervisor._refresh_exchange_algo_snapshot = lambda: asyncio.sleep(0)  # type: ignore[method-assign]

    async def ready_scenario() -> bool:
        supervisor._engine_task = asyncio.create_task(pending())
        try:
            return await supervisor._wait_for_startup()
        finally:
            supervisor._engine_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor._engine_task

    assert asyncio.run(ready_scenario()) is True
