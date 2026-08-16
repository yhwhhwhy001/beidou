"""Compatibility shim for the canonical :mod:`beidou_launcher` CLI.

This module owns no runtime construction or write authority. All invocations
are routed through the single governed launcher entrypoint.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Delegate the legacy module invocation to ``beidou start``."""
    from beidou_launcher.cli import main as launcher_command

    launcher_command.main(
        args=["start", *sys.argv[1:]],
        prog_name="python -m apps.autopilot",
        standalone_mode=True,
    )


if __name__ == "__main__":
    main()
