from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from beidou_control.api import ControlPlaneAPI, create_app
from beidou_control.plane import ControlAction


def test_emergency_action_is_not_successful_when_control_plane_is_unwired() -> None:
    api = ControlPlaneAPI()

    result = api.emergency("NO_NEW_RISK", "operator-1", "corr-1")

    assert result.success is False
    assert "not wired" in result.reason
    assert api.get_emergency_history() == [result]


def test_emergency_action_records_success_only_after_control_plane_executes() -> None:
    actions: list[ControlAction] = []
    api = ControlPlaneAPI()
    api.wire_control_plane(
        SimpleNamespace(
            execute_action=lambda action: actions.append(action),
        )
    )

    result = api.emergency("NO_NEW_RISK", "operator-1", "corr-2")

    assert result.success is True
    assert actions == [ControlAction.NO_NEW_RISK]


def test_control_plane_health_readiness_eligibility_metrics_and_resume_boundaries() -> None:
    api = ControlPlaneAPI(host="localhost", port=9191)

    assert api.bind_address == "localhost:9191"
    assert api.is_remote_enabled is False
    assert api.health().status.value == "HEALTHY"
    assert api.readiness().ready is False
    assert api.readiness({"db": True, "exchange": True, "redis": True}).ready is True
    assert api.readiness({"db": True}).checks["exchange"] == "FAIL"

    rejected = api.trading_eligibility(
        account_facts=None,
        dq_tier="DEGRADED",
        reconciliation_clean=False,
        protection_coverage=99.9,
        gate_result="FAIL",
    )
    assert rejected.eligible is False
    assert "account_facts_fresh" in rejected.reason

    accepted = api.trading_eligibility(
        account_facts={"age": 1},
        dq_tier="PASS",
        reconciliation_clean=True,
        protection_coverage=100.0,
        gate_result="PASS",
    )
    assert accepted.eligible is True
    api.update_metrics({"queue_depth": 2.0})
    assert api.get_metrics()["queue_depth"] == 2.0
    assert api.get_metrics()["emergency_actions_count"] == 0

    assert api.resume_trading() == {
        "action": "RESUME",
        "success": False,
        "error": "control_plane not wired",
    }
    api.wire_control_plane(
        SimpleNamespace(
            execute_authorized_resume=lambda: (False, "incident-active"),
            get_status=lambda: SimpleNamespace(value="RESUME"),
        )
    )
    assert api.resume_trading()["success"] is False


def test_control_plane_factor_and_emergency_fail_closed_paths() -> None:
    from beidou_research.factors.factor import FactorLifecycle

    api = ControlPlaneAPI()
    invalid = api.emergency("INVALID", "operator", "corr")
    assert invalid.success is False

    class _Record:
        lifecycle = SimpleNamespace(value="IDEA")

        @staticmethod
        def _valid_transitions() -> list[FactorLifecycle]:
            return [FactorLifecycle.PAPER_TRADING]

    record = _Record()
    registry = SimpleNamespace(
        _factors={"factor-1": record}, get=lambda factor_id: record if factor_id == "factor-1" else None
    )
    api.wire_factor_registry(registry)
    assert api.list_factors() == [
        {"factor_id": "factor-1", "lifecycle": "IDEA", "can_transition_to": ["PAPER_TRADING"]}
    ]
    assert api.promote_factor("missing", "ACTIVE")["success"] is False
    assert api.promote_factor("factor-1", "not-a-state")["success"] is False
    assert api.promote_all_to_active()["error"] == "BULK_FACTOR_PROMOTION_FORBIDDEN"


def test_control_plane_reference_app_registers_and_invokes_fail_closed_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _HTTPException(Exception):
        def __init__(self, *, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _FakeApp:
        def __init__(self, **_kwargs: object) -> None:
            self.routes: dict[tuple[str, str], object] = {}

        def _decorator(self, method: str, path: str):
            def decorate(fn):
                self.routes[(method, path)] = fn
                return fn

            return decorate

        def get(self, path: str):
            return self._decorator("GET", path)

        def post(self, path: str):
            return self._decorator("POST", path)

    fastapi = ModuleType("fastapi")
    fastapi.FastAPI = _FakeApp  # type: ignore[attr-defined]
    fastapi.HTTPException = _HTTPException  # type: ignore[attr-defined]
    fastapi.Request = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)

    api = ControlPlaneAPI()
    app = create_app(api)
    assert app is not None
    assert ("GET", "/health") in app.routes
    assert asyncio.run(app.routes[("GET", "/health")]())["status"] == "HEALTHY"
    assert asyncio.run(app.routes[("GET", "/facts")]())["status"] == "NOT_VERIFIABLE"
    with pytest.raises(_HTTPException) as ready_error:
        asyncio.run(app.routes[("GET", "/ready")]())
    assert ready_error.value.status_code == 503
    with pytest.raises(_HTTPException) as emergency_error:
        asyncio.run(app.routes[("POST", "/emergency/{action}")]("NO_NEW_RISK", object()))
    assert emergency_error.value.status_code == 400
