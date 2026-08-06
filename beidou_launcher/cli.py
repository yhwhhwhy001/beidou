"""Command-line entrypoint for `beidou`, `北斗`, and `bd`."""

from __future__ import annotations

import sys

import click

from .checks import PreflightChecker
from .manifest import DEFAULT_MODE, DEFAULT_SYMBOLS, SUPPORTED_MODES
from .supervisor import BeidouSupervisor


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "action",
    required=False,
    default="start",
    type=click.Choice(["start", "doctor", "status", "stop"], case_sensitive=False),
)
@click.option("--mode", type=click.Choice(SUPPORTED_MODES), default=DEFAULT_MODE, show_default=True)
@click.option("--symbols", default=",".join(DEFAULT_SYMBOLS), show_default=True, help="逗号分隔的交易对，或 ALL")
@click.option("--startup-timeout", type=click.IntRange(min=30, max=900), default=180, show_default=True)
@click.option("--poll-interval", type=click.IntRange(min=5, max=300), default=10, show_default=True)
@click.option("--self-heal/--no-self-heal", default=True, show_default=True)
@click.option("--max-restarts", type=click.IntRange(min=0, max=10), default=2, show_default=True)
def main(
    action: str,
    mode: str,
    symbols: str,
    startup_timeout: int,
    poll_interval: int,
    self_heal: bool,
    max_restarts: int,
) -> None:
    """一键启动、诊断、查看或停止北斗 Autopilot。"""

    normalized_symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not normalized_symbols:
        raise click.UsageError("--symbols 不能为空")

    action = action.lower()
    if action == "doctor":
        report = PreflightChecker(mode, normalized_symbols).run()
        BeidouSupervisor._print_report(report)
        raise SystemExit(0 if report.passed else 2)
    if action == "status":
        raise SystemExit(BeidouSupervisor.status())
    if action == "stop":
        raise SystemExit(BeidouSupervisor.stop())

    supervisor = BeidouSupervisor(
        mode=mode,
        symbols=normalized_symbols,
        startup_timeout=startup_timeout,
        poll_interval=poll_interval,
        self_heal=self_heal,
        max_restarts=max_restarts,
    )
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    sys.exit(main())
