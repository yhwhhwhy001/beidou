from __future__ import annotations

from types import SimpleNamespace

from beidou_control.api import ControlPlaneAPI
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
