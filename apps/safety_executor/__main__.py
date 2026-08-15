"""Retired safety-executor entrypoint delegated to the governed launcher."""

from __future__ import annotations

import sys


def main() -> None:
    """Start only safety-only mode and require explicit symbols."""
    if "--mode" in sys.argv[1:]:
        raise SystemExit("apps.safety_executor fixes mode=safety_only; use beidou directly for another mode")

    from beidou_launcher.cli import main as launcher_command

    launcher_command.main(
        args=["start", "--mode", "safety_only", *sys.argv[1:]],
        prog_name="python -m apps.safety_executor",
        standalone_mode=True,
    )


if __name__ == "__main__":
    main()
