"""Retired strategy-engine entrypoint delegated to the governed launcher."""

from __future__ import annotations

import sys


def main() -> None:
    """Start only the non-writing paper mode and require explicit symbols."""
    if any(argument == "--mode" or argument.startswith("--mode=") for argument in sys.argv[1:]):
        raise SystemExit("apps.strategy_engine fixes mode=paper; use beidou directly for another mode")

    from beidou_launcher.cli import main as launcher_command

    launcher_command.main(
        args=["start", "--mode", "paper", *sys.argv[1:]],
        prog_name="python -m apps.strategy_engine",
        standalone_mode=True,
    )


if __name__ == "__main__":
    main()
