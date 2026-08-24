"""Governed identifiers and readiness contract for the G5 producer runtime.

The producer is an evidence-collection runtime, not a second trading entry
point.  Keeping its launchd label and status contract in one small module
prevents the restart scenarios, CLI and launchd orchestration from drifting.
"""

from __future__ import annotations

import os
from typing import Any

PRODUCER_ACTION = "g5-producer"
PRODUCER_LABEL = "com.beidou.g5-producer"
PRODUCER_ENVIRONMENT_MARKER = "BEIDOU_G5_PRODUCER"


def launchd_target(label: str, *, uid: int | None = None) -> str:
    """Return a per-user launchd target for one governed service label."""

    effective_uid = os.getuid() if uid is None else uid
    if effective_uid < 0:
        raise ValueError("uid must be non-negative")
    if not label or any(char in label for char in "/\n\r"):
        raise ValueError("label must be a non-empty launchd label")
    return f"gui/{effective_uid}/{label}"


def producer_launchd_target(*, uid: int | None = None) -> str:
    """Return the fixed launchd target used only by the G5 producer."""

    return launchd_target(PRODUCER_LABEL, uid=uid)


def producer_process_pattern() -> str:
    """Return the fixed process pattern for the producer restart probe."""

    return "beidou g5-producer" if os.environ.get(PRODUCER_ENVIRONMENT_MARKER) == "1" else "beidou start"


def producer_status_verdict(payload: dict[str, Any]) -> tuple[bool, str]:
    """Validate the explicit producer-only status contract.

    A normal ``trading_ready`` response is deliberately insufficient.  The
    producer must identify itself, report that its writes are held, remain in
    ``NO_NEW_RISK`` and expose a fresh MATCHED reconciliation fact.
    """

    if payload.get("g5_producer_mode") is not True:
        return False, "PRODUCER_MODE_NOT_EXPLICIT"
    if payload.get("g5_producer_writes_held") is not True:
        return False, "PRODUCER_WRITES_NOT_HELD"
    if payload.get("control_action") != "NO_NEW_RISK":
        return False, "PRODUCER_CONTROL_NOT_HELD"
    recon = payload.get("last_reconciliation")
    if not isinstance(recon, dict) or recon.get("status") != "MATCHED":
        return False, "RECON_NOT_MATCHED"
    if payload.get("g5_producer_ready") is not True:
        return False, "PRODUCER_NOT_READY"
    return True, "G5_PRODUCER_READY"


__all__ = [
    "PRODUCER_ACTION",
    "PRODUCER_ENVIRONMENT_MARKER",
    "PRODUCER_LABEL",
    "launchd_target",
    "producer_launchd_target",
    "producer_process_pattern",
    "producer_status_verdict",
]
