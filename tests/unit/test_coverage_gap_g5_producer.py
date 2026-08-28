"""Coverage-gap tests for beidou_launcher.g5_producer negative branches."""

from __future__ import annotations

import pytest

from beidou_launcher.g5_producer import launchd_target, producer_status_verdict


def test_launchd_target_rejects_negative_uid() -> None:
    with pytest.raises(ValueError, match="uid must be non-negative"):
        launchd_target("com.beidou.g5-producer", uid=-1)


@pytest.mark.parametrize("label", ["", "a/b", "a\nb", "a\rb"])
def test_launchd_target_rejects_invalid_label(label: str) -> None:
    with pytest.raises(ValueError, match="label must be a non-empty launchd label"):
        launchd_target(label)


def test_producer_status_verdict_rejects_unmatched_reconciliation() -> None:
    payload = {
        "g5_producer_mode": True,
        "g5_producer_writes_held": True,
        "control_action": "NO_NEW_RISK",
        "g5_producer_ready": True,
        "last_reconciliation": {"status": "MISMATCHED"},
    }
    assert producer_status_verdict(payload) == (False, "RECON_NOT_MATCHED")


def test_producer_status_verdict_rejects_non_dict_reconciliation() -> None:
    payload = {
        "g5_producer_mode": True,
        "g5_producer_writes_held": True,
        "control_action": "NO_NEW_RISK",
        "g5_producer_ready": True,
        "last_reconciliation": None,
    }
    assert producer_status_verdict(payload) == (False, "RECON_NOT_MATCHED")
