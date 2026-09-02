"""CLI for the finite execution-probe campaign."""

from __future__ import annotations

import asyncio
import json

import click

from .config import SoakConfig
from .runtime import build_runner


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--confirm-testnet", is_flag=True, help="Explicitly authorize Binance Demo/Testnet access.")
@click.option("--episodes", type=click.IntRange(1, 30), default=None, help="Execution-probe episodes (max 30).")
@click.option("--max-duration-seconds", type=click.FloatRange(min=0, max=3600, min_open=True), default=None)
@click.option("--cycle-interval-seconds", type=click.FloatRange(min=60), default=None)
@click.option("--target-notional", type=click.FloatRange(min=0, max=100, min_open=True), default=None)
@click.option("--absolute-notional-ceiling", type=click.FloatRange(min=0, max=500, min_open=True), default=None)
@click.option("--max-leverage", type=click.FloatRange(min=0, max=3, min_open=True), default=None)
@click.option("--symbol", type=str, default=None, help="One canonical Binance USD-M symbol.")
def main(
    *,
    confirm_testnet: bool,
    episodes: int | None,
    max_duration_seconds: float | None,
    cycle_interval_seconds: float | None,
    target_notional: float | None,
    absolute_notional_ceiling: float | None,
    max_leverage: float | None,
    symbol: str | None,
) -> None:
    """Run a bounded execution-probe; never treat its fills as Alpha evidence."""

    if not confirm_testnet:
        raise click.UsageError("--confirm-testnet is required before runtime or network construction")
    overrides = {
        name: value
        for name, value in (
            ("confirm_testnet", confirm_testnet),
            ("episodes", episodes),
            ("max_duration_seconds", max_duration_seconds),
            ("cycle_interval_seconds", cycle_interval_seconds),
            ("target_notional", target_notional),
            ("absolute_notional_ceiling", absolute_notional_ceiling),
            ("max_leverage", max_leverage),
            ("symbol", symbol),
        )
        if value is not None
    }
    try:
        config = SoakConfig.from_env(**overrides)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    if config.kill_switch_path.exists():
        raise click.UsageError(f"durable kill switch is engaged: {config.kill_switch_path}")
    summary = asyncio.run(build_runner(config).run())
    click.echo(json.dumps(summary.to_dict(), sort_keys=True, separators=(",", ":")))
    if summary.status != "COMPLETED":
        raise click.ClickException(f"campaign stopped: {summary.stop_reason}")


if __name__ == "__main__":  # pragma: no cover
    main()
