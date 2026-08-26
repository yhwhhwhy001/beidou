"""Side-effect-free public command facade.

Importing :mod:`beidou_cli` deliberately does not import the legacy launcher.
The execution composition is a separately named, authorization-gated route.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click


@click.group(invoke_without_command=True)
@click.pass_context
def main(context: click.Context) -> None:
    """北斗安全命令入口；运行时构建仅可通过显式 execution start。"""

    if context.invoked_subcommand is None:
        click.echo(context.get_help())


@main.command("status")
def status() -> None:
    """Report the read-only facade status without probing runtime state."""

    click.echo(json.dumps({"execution": "EXPLICIT_ONLY", "mode": "SAFE", "runtime": "NOT_STARTED"}, sort_keys=True))


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
@click.option("--mode", default="paper", show_default=True)
@click.option("--symbols", default="BTCUSDT", show_default=True)
def execution_start(mode: str, symbols: str) -> None:
    """Request runtime construction after an explicit local authorization."""

    from beidou_launcher.alpha_first_adapter import start_authorized_execution

    try:
        start_authorized_execution(
            mode=mode,
            symbols=tuple(item.strip() for item in symbols.split(",") if item.strip()),
        )
    except PermissionError as exc:
        raise click.ClickException(str(exc)) from exc


__all__ = ["main"]
