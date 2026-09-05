"""``beidou live ...`` and ``beidou report ...`` commands."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from beidou_alpha.panel import interval_seconds
from beidou_alpha.registry import Registry
from beidou_cli import live, report
from beidou_data.binance_public import DEFAULT_BASE_URL, PublicClient
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
    registry_dataset_problems,
    registry_evidence_problems,
    resolve_universe,
    universe_sink,
)
from beidou_live.engine import LiveEngine
from beidou_live.health import cycle_health
from beidou_live.inputs import required_history
from beidou_live.paper import PaperVenue
from beidou_live.probe import probes_from_registry
from beidou_live.reports import (
    daily_markdown,
    daily_payload,
    expectations_from_evidence,
    weekly_markdown,
    weekly_payload,
)
from beidou_live.risk_budget import RiskBudgetParams
from beidou_live.scheduler import SystemClock
from beidou_live.state import StateStore
from beidou_live.verify import cycle_clock, last_cycle, last_recorded_as_of_ms, verify_live_targets


def clock_skew_seconds(rest_url: str) -> float | None:
    """Venue server time minus this host's clock, in seconds; ``None`` when the venue cannot be reached.

    A drifting host clock does not stop the loop (the REST client measures its own offset for signing, and
    the staleness guard is one-directional), but every timestamp the loop writes - the cycle `bar`, the
    heartbeat `at`, the UTC-day boundary that triggers the pool refresh - comes from this clock.
    """
    try:
        with PublicClient(rest_url, timeout=10.0, max_retries=1) as public:
            before = time.time()
            server = public.server_time_ms()
            after = time.time()
    except Exception:
        return None
    return (server - (before + after) / 2 * 1000) / 1000.0


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
    problems = registry_evidence_problems(registry, payload)
    # D-041: the dataset manifest, read rather than only written.  Advisory lines are printed and do not
    # stop anything - the daily sync moves klines/funding, and the loop rewrites universe.json itself.
    dataset = registry_dataset_problems(registry, data_root, _interval(payload))
    for message in (*dataset.blocking, *dataset.advisory):
        click.echo(f"dataset: {message}")
    if problems or dataset.blocking:
        for problem in problems:
            click.echo(f"evidence: {problem}")
        if not allow_unvalidated and not dry_run and not paper:
            raise click.ClickException(
                "enabled strategies lack validation evidence, or cite data that has since changed; "
                "run `beidou research validate` or pass --allow-unvalidated"
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
@click.option(
    "--max-skew-seconds",
    default=60.0,
    show_default=True,
    help="tolerated distance between the wake-up and a real bar boundary (a whole-bar offset is fine)",
)
@click.option(
    "--min-success-rate",
    default=0.95,
    show_default=True,
    help="M-001: fail the check when fewer than this share of recent cycles completed",
)
@click.option("--min-success-window", default=24, show_default=True, help="hours of cycles the rate covers")
def live_status(
    profile: str,
    paper: bool,
    check: bool,
    max_age_seconds: float | None,
    max_skew_seconds: float,
    min_success_rate: float,
    min_success_window: int,
) -> None:
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
    skew = clock_skew_seconds(str((payload.get("market_data", {}) or {}).get("rest_url", DEFAULT_BASE_URL)))
    if skew is None:
        click.echo("clock: venue time unavailable (skipped)")
    else:
        span = float(interval_seconds(interval))
        alignment = ((skew + span / 2) % span) - span / 2
        click.echo(f"clock: venue is {skew:+.1f}s from this host ({alignment:+.1f}s from a bar boundary)")
        # D-025: the host clock is the reference, so a whole-bar offset is accepted; the remainder is what
        # decides whether the loop wakes on a bar the venue has already closed.
        if abs(alignment) > max_skew_seconds:
            problems.append(
                f"the wake-up sits {alignment:+.1f}s from a bar boundary (> {max_skew_seconds:.0f}s): the loop "
                "may act on a bar that has not closed at the venue"
            )
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
    # M-001: what share of the loop's own cycles completed, from the append-only log rather than the heartbeat
    health = cycle_health(
        store.read_jsonl(store.cycles_path),
        now=datetime.now(UTC),
        window_hours=float(min_success_window),
        restarted_at=state.restarted_at,
    )
    if health.success_rate is None:
        click.echo(f"cycles: none in the last {health.window_hours:.0f}h")
    else:
        click.echo(
            f"cycles: {health.success_rate:.1%} of {health.attempts} completed in the last "
            f"{health.window_hours:.0f}h ({health.failures} failed); {health.clean_days} clean day(s); "
            f"{health.restarts_note()}"
        )
        if health.success_rate < min_success_rate:
            problems.append(
                f"cycle success rate {health.success_rate:.1%} < {min_success_rate:.0%} "
                f"({health.failures} of {health.attempts} failed, last at {health.last_failure})"
            )
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
    """M-011: recompute the last cycle's model output from public data + state.json and diff it (KILL-027 monitor).

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
                model,
                market,
                universe,
                config.interval,
                required_history(model, config.history_bars),
                state,
                tolerance,
                recorded_as_of_ms=last_recorded_as_of_ms(store),
            )
        finally:
            await market.aclose()

    result = asyncio.run(main())
    result["last_cycle_clock"] = cycle_clock(last_cycle(store))
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


def _interval(profile: dict[str, Any]) -> str:
    return str((profile.get("market_data", {}) or {}).get("interval", "1h"))


def _evidence_reports(registry: Registry) -> dict[str, Any]:
    """The reports the enabled strategies cite.  `report daily` and `report weekly` had byte-identical
    copies of this loop; one copy is what let the dataset check be added to both at once."""
    evidence: dict[str, Any] = {}
    for entry in registry.enabled:
        report_path = Path(str((entry.evidence or {}).get("report", "")))
        if report_path.exists():
            evidence[entry.id] = json.loads(report_path.read_text(encoding="utf-8"))
    return evidence


@report.command("daily")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--paper", is_flag=True, help="report on the paper-mode state directory")
@click.option("--date", "day", default=None, help="YYYY-MM-DD (default: today UTC)")
@click.option(
    "--out", default=None, help="directory for the markdown/json report (default: profile paths.reports_dir/daily)"
)
@click.option("--check", is_flag=True, help="exit non-zero when the report is in ALERT (for the hourly monitor)")
@click.option("--data-root", default=".beidou/data", show_default=True)
def report_daily(profile: str, paper: bool, day: str | None, out: str | None, check: bool, data_root: str) -> None:
    """Render the daily attribution report (with drift vs validation expectations) from the live state files."""
    payload = load_profile(profile)
    store = _store_for(payload, paper)
    chosen = day or datetime.now(UTC).strftime("%Y-%m-%d")
    registry = load_registry(payload.get("registry", "config/alpha_registry.yaml"))
    data = daily_payload(
        store,
        chosen,
        expectations_from_evidence(_evidence_reports(registry)),
        probes_from_registry(registry),
        RiskBudgetParams.from_mapping(payload.get("risk_budget", {}) or {}),
        dataset=asdict(registry_dataset_problems(registry, data_root, _interval(payload))),
    )
    markdown = daily_markdown(data)
    directory = Path(out or Path((payload.get("paths", {}) or {}).get("reports_dir", "reports")) / "daily")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chosen}.md").write_text(markdown, encoding="utf-8")
    (directory / f"{chosen}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    click.echo(markdown)
    click.echo(f"written {directory / f'{chosen}.md'}")
    # The drift status was computed and then thrown away: nothing ever sent it anywhere.
    alerts: list[str] = []
    for name, block in (("equity", data.get("drift") or {}), ("income", data.get("income_drift") or {})):
        if str(block.get("status")) == "ALERT":
            detail = block.get("reasons") or [
                f"{strategy}: z={row.get('z'):.1f}"
                for strategy, row in (block.get("by_strategy") or {}).items()
                if row.get("z") is not None and row["z"] < -2.0
            ]
            alerts.append(f"{name} drift ALERT: {'; '.join(str(d) for d in detail)}")
    budget = data.get("risk_budget") or {}
    if str(budget.get("status")) == "ALERT":
        # P13's ladder: the thresholds were fixed before the change went live, so this says what to do
        # rather than that something looks off.  It alerts; a human still runs the one-line change.
        alerts.append("risk budget ALERT: " + "; ".join(str(r) for r in budget.get("reasons") or []))
    adaptation = data.get("risk_adaptation") or {}
    if str(adaptation.get("status")) == "ALERT":
        # M-015: the weights stopped taking each symbol's volatility back out.  Loud rather than
        # quiet because this is the layer D-037 pointed at when it ruled the leverage layer inert.
        alerts.append(
            f"risk adaptation ALERT: compression {adaptation.get('compression'):.2f} > "
            f"{adaptation.get('limit'):.2f}; per-symbol sizing is no longer vol-scaled"
        )
    window = data.get("evidence_window") or {}
    if int(window.get("changes_7d") or 0) > 1:
        # the plan allowed one promotion per week and nothing ever counted them
        alerts.append(
            f"{window['changes_7d']} construction changes in the last 7 days; the plan allows one promotion per week"
        )
    if alerts:
        message = f"beidou {chosen}: " + " | ".join(alerts)
        click.echo(message, err=True)
        webhook = str((payload.get("alerts", {}) or {}).get("webhook_url", ""))
        if webhook:
            asyncio.run(WebhookAlerts(webhook).send(message))
    if check and alerts:
        raise SystemExit(1)


def _changed_lines(commits: int) -> dict[str, int] | None:
    """Lines added plus removed per path over the last N commits, or None outside a git checkout."""
    try:
        raw = subprocess.run(
            ["git", "log", "--numstat", "--format=", f"-{max(1, commits)}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    totals: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        count = (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
        totals[path] = totals.get(path, 0) + count
    return totals or None


@report.command("weekly")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--paper", is_flag=True, help="report on the paper-mode state directory")
@click.option("--date", "day", default=None, help="YYYY-MM-DD, the last day of the week (default: today UTC)")
@click.option("--out", default=None, help="directory for the report (default: profile paths.reports_dir/weekly)")
@click.option("--commits", default=40, show_default=True, help="commits to measure the alpha effort share over")
@click.option("--data-root", default=".beidou/data", show_default=True)
def report_weekly(profile: str, paper: bool, day: str | None, out: str | None, commits: int, data_root: str) -> None:
    """The plan's weekly research report: the week's decisions next to the week's evidence."""
    payload = load_profile(profile)
    store = _store_for(payload, paper)
    chosen = day or datetime.now(UTC).strftime("%Y-%m-%d")
    registry = load_registry(payload.get("registry", "config/alpha_registry.yaml"))
    data = weekly_payload(
        store,
        chosen,
        expectations=expectations_from_evidence(_evidence_reports(registry)),
        changed_lines=_changed_lines(commits),
        dataset=asdict(registry_dataset_problems(registry, data_root, _interval(payload))),
    )
    markdown = weekly_markdown(data)
    directory = Path(out or Path((payload.get("paths", {}) or {}).get("reports_dir", "reports")) / "weekly")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chosen}.md").write_text(markdown, encoding="utf-8")
    (directory / f"{chosen}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    click.echo(markdown)
    click.echo(f"written {directory / f'{chosen}.md'}")


__all__ = [
    "live_flatten",
    "live_kill_switch",
    "live_run",
    "live_status",
    "live_verify",
    "report_daily",
    "report_weekly",
]
