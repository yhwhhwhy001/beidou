"""Explicit, authorization-gated bridge from the safe CLI to legacy runtime.

This module has no launcher import at module load time.  The legacy runtime
composition is intentionally unreachable unless a caller supplies an
explicit local authorization marker; command discovery or help can therefore
never construct a supervisor or read runtime credentials.
"""

from __future__ import annotations

import os
from collections.abc import Sequence


def start_authorized_execution(
    *,
    mode: str,
    symbols: Sequence[str],
    port: int | None = None,
    startup_timeout: float | None = None,
    monitor_interval: float | None = None,
    self_heal: bool | None = None,
    max_restarts: int | None = None,
) -> int:
    """Construct the legacy supervisor only after explicit authorization."""

    if os.environ.get("BEIDOU_EXECUTION_AUTHORIZATION") != "EXPLICIT_LOCAL_APPROVAL":
        raise PermissionError(
            "execution authorization is required; set BEIDOU_EXECUTION_AUTHORIZATION="
            "EXPLICIT_LOCAL_APPROVAL only for an explicitly approved runtime task"
        )
    if mode == "testnet":
        raise PermissionError(
            "legacy Testnet runtime is prohibited; use python -m apps.testnet_verify "
            "with its bounded confirmation and safety gates"
        )
    if not symbols:
        raise ValueError("at least one explicit symbol is required")

    # This import is deliberately inside the authorized route.  The Alpha
    # task does not invoke this path and does not authorize runtime execution.
    from beidou_launcher.cli import main as legacy_main

    # Keep runtime activation explicit and delegated to the characterized
    # canonical entrypoint; no implicit fallback is provided by this facade.
    arguments = ["start", "--mode", mode, "--symbols", ",".join(symbols)]
    if port is not None:
        arguments.extend(("--port", str(port)))
    if startup_timeout is not None:
        arguments.extend(("--startup-timeout", str(startup_timeout)))
    if monitor_interval is not None:
        arguments.extend(("--monitor-interval", str(monitor_interval)))
    if self_heal is not None:
        arguments.append("--self-heal" if self_heal else "--no-self-heal")
    if max_restarts is not None:
        arguments.extend(("--max-restarts", str(max_restarts)))

    legacy_main(arguments, standalone_mode=False)
    return 0
