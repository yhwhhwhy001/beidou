"""Side-effect-free public command facade.

Importing :mod:`beidou_cli` deliberately does not import the legacy launcher.
The execution composition is a separately named, authorization-gated route.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import click

_RUNTIME_MODES = ("research", "paper", "shadow", "testnet", "safety_only")
_CommandFunction = TypeVar("_CommandFunction", bound=Callable[..., Any])


def _inspect_runtime_status() -> dict[str, object] | None:
    """Read the governed supervisor snapshot and verify its process identity."""

    from beidou_launcher.checks import find_project_root
    from beidou_launcher.state import inspect_runtime_status

    return inspect_runtime_status(find_project_root())


def _run_read_only_doctor(mode: str, port: int) -> tuple[list[dict[str, object]], bool]:
    """Run preflight probes without constructing a runtime or mutating process state."""

    from beidou_launcher.checks import find_project_root
    from beidou_launcher.preflight import run_preflight

    checks, _settings = run_preflight(find_project_root(), mode, port)
    return [item.to_dict() for item in checks], any(item.is_blocking for item in checks)


def _stop_runtime() -> tuple[bool, str]:
    """Use the identity- and freshness-checked stop path."""

    from beidou_launcher.checks import find_project_root
    from beidou_launcher.state import stop_running_instance

    return stop_running_instance(find_project_root())


@click.group(invoke_without_command=True)
@click.pass_context
def main(context: click.Context) -> None:
    """北斗安全命令入口；运行时构建仅可通过显式 execution start。"""

    if context.invoked_subcommand is None:
        click.echo(context.get_help())


@main.command("status")
def status() -> None:
    """Report observed runtime state using fresh process-bound evidence."""

    observed = _inspect_runtime_status()
    runtime = "NOT_RUNNING" if observed is None else str(observed.get("effective_state", "UNKNOWN"))
    click.echo(
        json.dumps(
            {
                "execution": "EXPLICIT_ONLY",
                "mode": "SAFE",
                "runtime": runtime,
                "runtime_evidence": observed,
            },
            sort_keys=True,
        )
    )


@main.command("doctor")
@click.option("--mode", type=click.Choice(_RUNTIME_MODES), default="safety_only", show_default=True)
@click.option("--port", type=click.IntRange(1024, 65535), default=9090, show_default=True)
def doctor(mode: str, port: int) -> None:
    """Run read-only startup, dependency, policy, and deployment diagnostics."""

    checks, blocked = _run_read_only_doctor(mode, port)
    for check in checks:
        click.echo(json.dumps(check, ensure_ascii=False, sort_keys=True))
    if blocked:
        raise SystemExit(2)


@main.command("stop")
def stop() -> None:
    """Stop only a process with fresh, matching supervisor identity evidence."""

    ok, message = _stop_runtime()
    click.echo(message)
    if not ok:
        raise SystemExit(1)


def _execution_start_options(command: _CommandFunction) -> _CommandFunction:
    command = click.option("--max-restarts", type=click.IntRange(0, 20), default=None)(command)
    command = click.option("--self-heal/--no-self-heal", default=None)(command)
    command = click.option("--monitor-interval", type=click.FloatRange(1.0, 60.0), default=None)(command)
    command = click.option("--startup-timeout", type=click.FloatRange(30.0, 900.0), default=None)(command)
    command = click.option("--port", type=click.IntRange(1024, 65535), default=None)(command)
    command = click.option("--symbols", default="BTCUSDT", show_default=True)(command)
    command = click.option("--mode", type=click.Choice(_RUNTIME_MODES), default="safety_only", show_default=True)(
        command
    )
    return command


def _request_execution(
    *,
    mode: str,
    symbols: str,
    port: int | None,
    startup_timeout: float | None,
    monitor_interval: float | None,
    self_heal: bool | None,
    max_restarts: int | None,
) -> None:
    from beidou_launcher.alpha_first_adapter import start_authorized_execution

    try:
        start_authorized_execution(
            mode=mode,
            symbols=tuple(item.strip() for item in symbols.split(",") if item.strip()),
            port=port,
            startup_timeout=startup_timeout,
            monitor_interval=monitor_interval,
            self_heal=self_heal,
            max_restarts=max_restarts,
        )
    except (PermissionError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("start")
@_execution_start_options
def legacy_start(
    mode: str,
    symbols: str,
    port: int | None,
    startup_timeout: float | None,
    monitor_interval: float | None,
    self_heal: bool | None,
    max_restarts: int | None,
) -> None:
    """Compatibility alias for ``execution start`` with identical gates."""

    _request_execution(
        mode=mode,
        symbols=symbols,
        port=port,
        startup_timeout=startup_timeout,
        monitor_interval=monitor_interval,
        self_heal=self_heal,
        max_restarts=max_restarts,
    )


@main.group("alpha")
def alpha() -> None:
    """Explicit offline Alpha commands using caller-bound local data."""


@alpha.command("evaluate")
@click.option("--closes", required=True, help="Comma-separated positive local close values.")
@click.option("--dataset-id", default="cli-local-fixture", show_default=True)
@click.option("--content-hash", default="cli-local-content", show_default=True)
def alpha_evaluate(closes: str, dataset_id: str, content_hash: str) -> None:
    """Evaluate Alpha without network, credentials, or persistence."""

    try:
        values = tuple(float(value.strip()) for value in closes.split(",") if value.strip())
    except ValueError as exc:
        raise click.BadParameter("closes must be comma-separated numbers", param_hint="--closes") from exc
    if not values:
        raise click.BadParameter("at least 51 close values are required", param_hint="--closes")

    # Delayed imports keep the root facade free of concrete runtime domains.
    from apps.alpha_app import BoundLocalData, OfflineAlphaApp
    from beidou_shared.contracts.experiment import DatasetRef
    from beidou_shared.types import InstrumentId, SchemaVersion, VenueId

    data = BoundLocalData(
        dataset=DatasetRef(dataset_id, SchemaVersion("1"), content_hash),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("LOCAL"),
        closes=values,
        observed_at=datetime.now(timezone.utc),
    )
    result = OfflineAlphaApp().evaluate(data)
    click.echo(
        json.dumps(
            {
                "dataset_id": result.dataset.dataset_id,
                "dataset_source": result.dataset.source,
                "forecast_hash": result.target.forecast_hash,
                "row_count": result.row_count,
                "target_weight": result.target.target_weight,
            },
            sort_keys=True,
        )
    )


@main.group("execution")
def execution() -> None:
    """Explicit runtime route; command presence grants no authority."""


@execution.command("start")
@_execution_start_options
def execution_start(
    mode: str,
    symbols: str,
    port: int | None,
    startup_timeout: float | None,
    monitor_interval: float | None,
    self_heal: bool | None,
    max_restarts: int | None,
) -> None:
    """Request runtime construction after an explicit local authorization."""

    _request_execution(
        mode=mode,
        symbols=symbols,
        port=port,
        startup_timeout=startup_timeout,
        monitor_interval=monitor_interval,
        self_heal=self_heal,
        max_restarts=max_restarts,
    )


__all__ = ["main"]
