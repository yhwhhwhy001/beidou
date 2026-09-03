"""``beidou`` command line: data sync | research backtest/validate/report | live run/status/flatten | report daily."""

from __future__ import annotations

import click

from beidou_cli._version import VERSION


@click.group()
@click.version_option(VERSION, prog_name="beidou")
def main() -> None:
    """北斗 V5 — Alpha-First trading system."""


@main.group()
def data() -> None:
    """Historical and live market data."""


@main.group()
def research() -> None:
    """Backtests, validation and research reports."""


@main.group()
def live() -> None:
    """Live trading loop on the configured venue profile."""


@main.group()
def report() -> None:
    """Daily attribution reports."""


__all__ = ["main"]
