"""``beidou live ...`` and ``beidou report ...`` commands."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from beidou_alpha.panel import interval_seconds
from beidou_cli import live, report
from beidou_data.binance_public import PublicClient
from beidou_live.alerts import WebhookAlerts
from beidou_live.composition import load_registry
from beidou_live.config import (
    build_market_data,
    build_model_from_profile,
    build_pool,
    build_store,
    build_venue,
    live_config,
    load_profile,
    registry_evidence_problems,
    resolve_universe,
    universe_sink,
)
from beidou_live.engine import LiveEngine
from beidou_live.inputs import required_history
from beidou_live.paper import PaperVenue
from beidou_live.probe import probes_from_registry
from beidou_live.reports import daily_markdown, daily_payload, expectations_from_evidence
from beidou_live.scheduler import SystemClock
from beidou_live.state import StateStore
from beidou_live.verify import verify_live_targets


def _paper_venue(market_url: str, balance: float, state_path: Path) -> PaperVenue:
    """Paper venue with mainnet trading rules (public exchangeInfo) and persistent simulated positions."""
    with PublicClient(market_url) as public:
        payload = public.exchange_info()
    return PaperVenue.from_exchange_info(payload, balance=balance, state_path=state_path)


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not verbose:
        # one line per request (14 kline pulls a cycle) is noise in an unattended log; errors still surface
        logging.getLogger("httpx").setLevel(logging.WARNING)


@live.command("run")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--dry-run", is_flag=True, help="compute targets and planned orders; never write to the venue")
@click.option(
    "--paper", is_flag=True, help="simulate fills in-process at mainnet marks; no credentials or exchange writes"
)
@click.option("--paper-balance", default=10_000.0, show_default=True)
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
    paper: bool,
    paper_balance: float,
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
        if not allow_unvalidated and not dry_run and not paper:
            raise click.ClickException(
                "enabled strategies lack validation evidence; run `beidou research validate` or pass --allow-unvalidated"
            )
    universe = resolve_universe(payload, [s for s in symbols.split(",") if s.strip()] or None, data_root)
    config = live_config(payload, universe, registry, dry_run=dry_run)
    if paper:
        store = StateStore(Path((payload.get("paths", {}) or {}).get("state_dir", ".beidou/live")).with_name("paper"))
    else:
        store = build_store(payload)
    market = build_market_data(payload)
    venue: Any
    if paper:
        venue = _paper_venue(market.base_url, paper_balance, store.directory / "paper_venue.json")
    else:
        venue = build_venue(payload, config.kill_switch_path)
    alerts = WebhookAlerts(str((payload.get("alerts", {}) or {}).get("webhook_url", "")))
    pool = build_pool(payload, market)
    engine = LiveEngine(
        config,
        model=model,
        market=market,
        venue=venue,
        clock=SystemClock(),
        store=store,
        alerts=alerts,
        pool=pool,
        universe_sink=universe_sink(data_root) if pool is not None else None,
    )
    leverage = "auto" if config.leverage_mode == "auto" else str(config.leverage)
    click.echo(
        f"universe={engine.universe} interval={config.interval} leverage={leverage} pool_refresh={pool is not None} "
        f"exits={config.exits.enabled} throttle={config.throttle.enabled} dry_run={dry_run} paper={paper} "
        f"kill_switch={config.kill_switch_path} state={store.directory}"
    )

    async def main() -> int:
        try:
            return await engine.run(cycles, immediate=immediate)
        finally:
            close = getattr(venue, "aclose", None)
            if callable(close):
                await close()
            await market.aclose()

    done = asyncio.run(main())
    click.echo(f"completed {done} cycle(s) without error")
    if cycles is not None and done < cycles:
        raise click.ClickException(f"{cycles - done} of {cycles} cycle(s) failed; see {store.heartbeat_path}")


@live.command("status")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--paper", is_flag=True, help="inspect the paper-mode state directory instead")
@click.option(
    "--check",
    is_flag=True,
    help="exit non-zero when the heartbeat is stale or the loop is erroring (cron/launchd alerts)",
)
@click.option("--max-age-seconds", default=None, type=float, help="staleness threshold (default: 2 x interval)")
def live_status(profile: str, paper: bool, check: bool, max_age_seconds: float | None) -> None:
    """Show heartbeat and state written by the running loop."""
    payload = load_profile(profile)
    store = _store_for(payload, paper)
    heartbeat = store.read_heartbeat()
    state = store.load()
    click.echo(json.dumps({"heartbeat": heartbeat, "state": state.to_dict()}, indent=2, sort_keys=True, default=str))
    if not check:
        return
    interval = str((payload.get("market_data", {}) or {}).get("interval", "1h"))
    threshold = max_age_seconds if max_age_seconds is not None else 2.0 * interval_seconds(interval)
    problems: list[str] = []
    if heartbeat is None:
        problems.append("no heartbeat")
    else:
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(str(heartbeat.get("at")))).total_seconds()
        except ValueError:
            age = float("inf")
        if age > threshold:
            problems.append(f"heartbeat is {age:.0f}s old (> {threshold:.0f}s)")
        if heartbeat.get("phase") == "ERROR" and int(heartbeat.get("consecutive_errors", 0)) >= 3:
            problems.append(f"loop erroring: {heartbeat.get('error')}")
    if problems:
        raise click.ClickException("; ".join(problems))


def _store_for(payload: dict[str, Any], paper: bool) -> StateStore:
    if paper:
        return StateStore(Path((payload.get("paths", {}) or {}).get("state_dir", ".beidou/live")).with_name("paper"))
    return build_store(payload)


@live.command("verify")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--paper", is_flag=True, help="verify the paper-mode state directory instead")
@click.option("--tolerance", default=1e-9, show_default=True, help="max |difference| per contribution")
@click.option("--check", is_flag=True, help="exit non-zero when the last cycle's contributions do not reproduce")
@click.option("--data-root", default=".beidou/data", show_default=True)
def live_verify(profile: str, paper: bool, tolerance: float, check: bool, data_root: str) -> None:
    """M-009: recompute the last cycle's model output from public data + state.json and diff it (KILL-027 monitor).

    Reads only.  Contributions must reproduce to the tolerance; a target difference is informational
    because state.json holds the post-throttle / post-exit / post-guard targets.
    """
    _logging(False)
    payload = load_profile(profile)
    model, registry = build_model_from_profile(payload)
    store = _store_for(payload, paper)
    state = store.load()
    if state.last_bar_ms is None:
        raise click.ClickException("no completed cycle in state.json yet")
    if state.stopped_books:
        model = model.without_books(list(state.stopped_books))
    universe = list(dict.fromkeys([*state.universe, *state.leaving])) or resolve_universe(payload, None, data_root)
    config = live_config(payload, universe, registry, dry_run=True)
    market = build_market_data(payload)

    async def main() -> dict[str, Any]:
        try:
            return await verify_live_targets(
                model, market, universe, config.interval, required_history(model, config.history_bars), state, tolerance
            )
        finally:
            await market.aclose()

    result = asyncio.run(main())
    click.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
    if check and not result.get("ok"):
        raise click.ClickException(str(result.get("note")))


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
@click.option("--paper", is_flag=True, help="report on the paper-mode state directory")
@click.option("--date", "day", default=None, help="YYYY-MM-DD (default: today UTC)")
@click.option(
    "--out", default=None, help="directory for the markdown/json report (default: profile paths.reports_dir/daily)"
)
def report_daily(profile: str, paper: bool, day: str | None, out: str | None) -> None:
    """Render the daily attribution report (with drift vs validation expectations) from the live state files."""
    payload = load_profile(profile)
    store = _store_for(payload, paper)
    chosen = day or datetime.now(UTC).strftime("%Y-%m-%d")
    registry = load_registry(payload.get("registry", "config/alpha_registry.yaml"))
    evidence: dict[str, Any] = {}
    for entry in registry.enabled:
        report_path = Path(str((entry.evidence or {}).get("report", "")))
        if report_path.exists():
            evidence[entry.id] = json.loads(report_path.read_text(encoding="utf-8"))
    data = daily_payload(store, chosen, expectations_from_evidence(evidence), probes_from_registry(registry))
    markdown = daily_markdown(data)
    directory = Path(out or Path((payload.get("paths", {}) or {}).get("reports_dir", "reports")) / "daily")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chosen}.md").write_text(markdown, encoding="utf-8")
    (directory / f"{chosen}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    click.echo(markdown)
    click.echo(f"written {directory / f'{chosen}.md'}")


__all__ = ["live_flatten", "live_kill_switch", "live_run", "live_status", "live_verify", "report_daily"]
