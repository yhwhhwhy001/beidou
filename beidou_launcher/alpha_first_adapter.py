"""Explicit, authorization-gated bridge from the safe CLI to legacy runtime.

This module has no launcher import at module load time.  The legacy runtime
composition is intentionally unreachable unless a caller supplies an
explicit local authorization marker; command discovery or help can therefore
never construct a supervisor or read runtime credentials.
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def start_authorized_execution(*, mode: str, symbols: Sequence[str]) -> int:
    """Construct the legacy supervisor only after explicit authorization."""

    if os.environ.get("BEIDOU_EXECUTION_AUTHORIZATION") != "EXPLICIT_LOCAL_APPROVAL":
        raise PermissionError(
            "execution authorization is required; set BEIDOU_EXECUTION_AUTHORIZATION="
            "EXPLICIT_LOCAL_APPROVAL only for an explicitly approved runtime task"
        )
    if not symbols:
        raise ValueError("at least one explicit symbol is required")

    # This import is deliberately inside the authorized route.  The Alpha
    # task does not invoke this path and does not authorize runtime execution.
    from beidou_launcher.cli import main as legacy_main

    # Keep runtime activation explicit and delegated to the characterized
    # canonical entrypoint; no implicit fallback is provided by this facade.
    legacy_main(
        ["start", "--mode", mode, "--symbols", ",".join(symbols)],
        standalone_mode=False,
    )
    return 0
