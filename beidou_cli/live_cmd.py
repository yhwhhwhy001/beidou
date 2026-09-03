"""``beidou live ...`` and ``beidou report ...`` commands."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import click

from beidou_cli import live, report
from beidou_live.alerts import WebhookAlerts
from beidou_live.config import (
    build_market_data,
    build_model_from_profile,
    build_store,
    build_venue,
    live_config,
    load_profile,
    registry_evidence_problems,
    resolve_universe,
)
from beidou_live.engine import LiveEngine
from beidou_live.reports import daily_markdown, daily_payload
from beidou_live.scheduler import SystemClock


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


@live.command("run")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--dry-run", is_flag=True, help="compute targets and planned orders; never write to the venue")
@click.option("--cycles", default=None, type=int, help="stop after N bar cycles (default: run forever)")
@click.option(
    "--immediate", is_flag=True, help="run one cycle on the last closed bar right away, then follow the schedule"
)
@click.option("--symbols", default="", help="comma-separated universe override")
@click.option("--allow-unvalidated", is_flag=True, help="start even if enabled strategies lack validation evidence")
@click.option("--data-root", default=".beidou/data", show_default=True)
@click.option("--verbose", is_flag=True)
def live_run(
    profile: str,
    dry_run: bool,
    cycles: int | None,
    immediate: bool,
    symbols: str,
    allow_unvalidated: bool,
    data_root: str,
    verbose: bool,
) -> None:
    """Run the bar-driven live loop against the configured (demo) venue."""
    _logging(verbose)
    payload = load_profile(profile)
    model, registry = build_model_from_profile(payload)
    problems = registry_evidence_problems(registry)
    if problems:
        for problem in problems:
            click.echo(f"evidence: {problem}")
        if not allow_unvalidated and not dry_run:
            raise click.ClickException(
                "enabled strategies lack validation evidence; run `beidou research validate` or pass --allow-unvalidated"
            )
    universe = resolve_universe(payload, [s for s in symbols.split(",") if s.strip()] or None, data_root)
    config = live_config(payload, universe, registry, dry_run=dry_run)
    store = build_store(payload)
    venue = build_venue(payload, config.kill_switch_path)
    market = build_market_data(payload)
    alerts = WebhookAlerts(str((payload.get("alerts", {}) or {}).get("webhook_url", "")))
    engine = LiveEngine(
        config, model=model, market=market, venue=venue, clock=SystemClock(), store=store, alerts=alerts
    )
    click.echo(
        f"universe={universe} interval={config.interval} leverage={config.leverage} dry_run={dry_run} kill_switch={config.kill_switch_path}"
    )

    async def main() -> int:
        try:
            return await engine.run(cycles, immediate=immediate)
        finally:
            await venue.aclose()
            await market.aclose()

    done = asyncio.run(main())
    click.echo(f"completed {done} cycle(s)")


@live.command("status")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
def live_status(profile: str) -> None:
    """Show heartbeat and state written by the running loop."""
    store = build_store(load_profile(profile))
    heartbeat = store.read_heartbeat()
    state = store.load()
    click.echo(json.dumps({"heartbeat": heartbeat, "state": state.to_dict()}, indent=2, sort_keys=True, default=str))


@live.command("flatten")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--yes", is_flag=True, help="confirm closing every position with reduce-only market orders")
@click.option("--data-root", default=".beidou/data", show_default=True)
def live_flatten(profile: str, yes: bool, data_root: str) -> None:
    """Close all positions now (reduce-only market orders); works while the kill switch is engaged."""
    if not yes:
        raise click.ClickException("pass --yes to confirm flattening every position")
    _logging(False)
    payload = load_profile(profile)
    model, registry = build_model_from_profile(payload)
    universe = resolve_universe(payload, None, data_root)
    config = live_config(payload, universe, registry, dry_run=False)
    venue = build_venue(payload, config.kill_switch_path)
    engine = LiveEngine(
        config,
        model=model,
        market=build_market_data(payload),
        venue=venue,
        clock=SystemClock(),
        store=build_store(payload),
    )

    async def main() -> None:
        try:
            for item in await engine.flatten():
                click.echo(json.dumps(item.to_dict(), default=str))
        finally:
            await venue.aclose()

    asyncio.run(main())


@live.command("kill-switch")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--engage/--release", default=True, help="engage (default) or release the durable kill switch")
def live_kill_switch(profile: str, engage: bool) -> None:
    """Engage/release the kill-switch file: while engaged, only risk-reducing orders are sent."""
    payload = load_profile(profile)
    path = Path((payload.get("guards", {}) or {}).get("kill_switch_path", ".beidou/live/KILL_SWITCH"))
    if engage:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"engaged {datetime.now(UTC).isoformat()}\n", encoding="utf-8")
        click.echo(f"kill switch engaged: {path}")
    else:
        if path.exists():
            path.unlink()
        click.echo(f"kill switch released: {path}")


@report.command("daily")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--date", "day", default=None, help="YYYY-MM-DD (default: today UTC)")
@click.option(
    "--out", default=None, help="directory for the markdown/json report (default: profile paths.reports_dir/daily)"
)
def report_daily(profile: str, day: str | None, out: str | None) -> None:
    """Render the daily attribution report from the live state files."""
    payload = load_profile(profile)
    store = build_store(payload)
    chosen = day or datetime.now(UTC).strftime("%Y-%m-%d")
    data = daily_payload(store, chosen)
    markdown = daily_markdown(data)
    directory = Path(out or Path((payload.get("paths", {}) or {}).get("reports_dir", "reports")) / "daily")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chosen}.md").write_text(markdown, encoding="utf-8")
    (directory / f"{chosen}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    click.echo(markdown)
    click.echo(f"written {directory / f'{chosen}.md'}")


__all__ = ["live_flatten", "live_kill_switch", "live_run", "live_status", "report_daily"]
