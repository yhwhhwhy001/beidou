"""Command-line entrypoint for the bounded Testnet verification runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from .config import VerifierConfig
from .runtime import run


def _optional_overrides(
    *,
    rest_url: str | None,
    account_id: str | None,
    max_notional: float | None,
    max_leverage: float | None,
    max_instruments: int | None,
    interval: str | None,
    kline_limit: int | None,
    trace_path: str | None,
    pool_state_path: str | None,
    kill_switch_file: str | None,
    evidence_dir: str | None,
) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, value in (
        ("rest_url", rest_url),
        ("account_id", account_id),
        ("max_notional", max_notional),
        ("max_leverage", max_leverage),
        ("max_instruments", max_instruments),
        ("interval", interval),
        ("kline_limit", kline_limit),
        ("trace_path", Path(trace_path) if trace_path else None),
        ("pool_state_path", Path(pool_state_path) if pool_state_path else None),
        ("kill_switch_path", Path(kill_switch_file) if kill_switch_file else None),
        ("evidence_dir", Path(evidence_dir) if evidence_dir else None),
    ):
        if value is not None:
            values[name] = value
    return values


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--rest-url", default=None, help="HTTPS Binance Testnet/Demo REST base URL.")
@click.option("--account-id", default=None, help="Dedicated local Testnet account identity.")
@click.option(
    "--confirm-testnet",
    is_flag=True,
    help="Explicitly enable bounded Testnet risk-increasing writes for this process.",
)
@click.option("--max-notional", type=float, default=None, help="Absolute per-order Testnet notional cap.")
@click.option("--max-leverage", type=float, default=None, help="Absolute Testnet leverage cap.")
@click.option("--max-instruments", type=click.IntRange(1, 50), default=None, help="Maximum dynamic pool candidates.")
@click.option(
    "--interval",
    type=click.Choice(["1m", "5m", "15m", "30m", "1h", "4h", "1d"]),
    default=None,
    help="Closed-bar interval used by the decision cycle.",
)
@click.option("--kline-limit", type=click.IntRange(10, 1500), default=None, help="Closed bars requested per symbol.")
@click.option("--once", is_flag=True, help="Run one bounded startup/decision cycle and exit.")
@click.option(
    "--close-after-verify",
    is_flag=True,
    help="After a filled verification order, submit an owned reduce-only close.",
)
@click.option("--trace-path", default=None, help="Durable DecisionTrace JSONL path.")
@click.option("--pool-state-path", default=None, help="Durable adaptive-pool state path.")
@click.option("--kill-switch-file", default=None, help="Durable Testnet kill-switch file.")
@click.option(
    "--engage-kill-switch",
    is_flag=True,
    help="Engage the durable Testnet kill switch and exit without starting the runtime.",
)
@click.option(
    "--compact-traces",
    is_flag=True,
    help="Archive and compact the local DecisionTrace journal without network or runtime construction.",
)
@click.option("--evidence-dir", default=None, help="Evidence manifest output directory.")
def main(
    rest_url: str | None,
    account_id: str | None,
    confirm_testnet: bool,
    max_notional: float | None,
    max_leverage: float | None,
    max_instruments: int | None,
    interval: str | None,
    kline_limit: int | None,
    once: bool,
    close_after_verify: bool,
    trace_path: str | None,
    pool_state_path: str | None,
    kill_switch_file: str | None,
    engage_kill_switch: bool,
    compact_traces: bool,
    evidence_dir: str | None,
) -> None:
    """Run the sole Beidou Testnet Verification composition root."""

    try:
        config = VerifierConfig.from_env(
            **_optional_overrides(
                rest_url=rest_url,
                account_id=account_id,
                max_notional=max_notional,
                max_leverage=max_leverage,
                max_instruments=max_instruments,
                interval=interval,
                kline_limit=kline_limit,
                trace_path=trace_path,
                pool_state_path=pool_state_path,
                kill_switch_file=kill_switch_file,
                evidence_dir=evidence_dir,
            ),
            confirm_testnet=confirm_testnet,
            once=once,
            close_after_verify=close_after_verify,
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"invalid Testnet verifier configuration: {exc}") from exc

    if compact_traces:
        if engage_kill_switch or confirm_testnet:
            raise click.ClickException("--compact-traces cannot be combined with write-capable actions")
        from beidou_shared.decision_trace import DecisionTraceStore

        try:
            report = DecisionTraceStore(config.trace_path).compact()
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException(f"DecisionTrace compaction failed closed: {type(exc).__name__}: {exc}") from exc
        click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return

    if engage_kill_switch:
        try:
            config.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
            config.kill_switch_path.write_text("engaged\n", encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"could not engage Testnet kill switch: {exc}") from exc
        click.echo(f"[testnet_verify] durable kill switch engaged: {config.kill_switch_path}")
        return

    click.echo(
        "[testnet_verify] starting "
        f"(url={config.rest_url}, max_notional={config.max_notional}, max_leverage={config.max_leverage}, "
        f"confirm_testnet={config.confirm_testnet}, once={config.once})"
    )
    if config.confirm_testnet:
        click.echo(
            "[testnet_verify] bounded Testnet writes are enabled; Mainnet is never an allowed destination.",
            err=True,
        )

    try:
        summary = asyncio.run(run(config))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(f"Testnet verifier failed closed: {type(exc).__name__}: {exc}") from exc

    click.echo(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True))
    if summary.status == "NOT_VERIFIABLE":
        raise click.exceptions.Exit(2)


__all__ = ["main"]
