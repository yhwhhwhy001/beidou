"""``beidou data ...`` commands."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import click
import pandas as pd

from beidou_cli import data
from beidou_data.archive import ArchiveClient, Month
from beidou_data.binance_public import AsyncPublicClient, PublicClient
from beidou_data.pool import (
    MEMBERSHIP_FILE,
    LivePool,
    daily_quote_volume,
    membership_summary,
    point_in_time_membership,
    sync_daily,
)
from beidou_data.store import FundingStore, KlineStore
from beidou_data.sync import sync_funding, sync_klines
from beidou_data.universe import UniverseConfig, eligible_symbols
from beidou_live.composition import read_universe, write_universe
from beidou_shared.binance_rules import parse_exchange_info
from beidou_shared.config import load_yaml


@data.command("sync")
@click.option("--universe", "universe_path", default="config/universe.yaml", show_default=True)
@click.option("--root", default=".beidou/data", show_default=True, help="parquet store root")
@click.option("--symbols", default="", help="comma-separated override of the candidate set")
@click.option("--start", default=None, help="first archive month YYYY-MM (default from universe.yaml)")
@click.option("--market-url", default="https://fapi.binance.com", show_default=True)
@click.option("--funding/--no-funding", default=True, show_default=True)
@click.option("--candidates", default=0, help="number of volume-ranked candidates to download (default 2*top_n)")
def data_sync(
    universe_path: str, root: str, symbols: str, start: str | None, market_url: str, funding: bool, candidates: int
) -> None:
    """Download/refresh mainnet public klines + funding for the universe candidates, then select the universe."""
    config = UniverseConfig.from_mapping(load_yaml(universe_path))
    history_start = Month.parse(start or config.history_start)
    store = KlineStore(root)
    funding_store = FundingStore(root)
    with PublicClient(market_url) as public, ArchiveClient() as archive:
        now_ms = public.server_time_ms()
        rules = parse_exchange_info(public.exchange_info())
        eligible = set(eligible_symbols(rules, config))
        volume_24h = {str(row["symbol"]): float(row.get("quoteVolume", 0.0)) for row in public.ticker_24h()}
        if symbols:
            candidate_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            ranked = sorted((s for s in eligible if s in volume_24h), key=lambda s: -volume_24h[s])
            limit = candidates or 2 * config.top_n
            candidate_list = list(dict.fromkeys([*config.always_include, *ranked[:limit]]))
        click.echo(f"candidates: {len(candidate_list)} symbols from {history_start} (root={root})")
        reports = []
        for index, symbol in enumerate(candidate_list, start=1):
            started = time.monotonic()
            report = sync_klines(
                symbol,
                config.interval,
                history_start=history_start,
                store=store,
                archive=archive,
                public=public,
                now_ms=now_ms,
            )
            if funding:
                sync_funding(
                    symbol,
                    history_start=history_start,
                    store=funding_store,
                    public=public,
                    now_ms=now_ms,
                    report=report,
                )
            reports.append(report)
            status = "OK" if not report.errors else f"ERRORS={len(report.errors)}"
            click.echo(
                f"[{index}/{len(candidate_list)}] {symbol}: rows={report.total_rows} archive_months={report.archive_months} "
                f"rest_rows={report.rest_rows} funding={report.funding_rows} {status} ({time.monotonic() - started:.1f}s)"
            )
            for error in report.errors:
                click.echo(f"    ! {error}")
    # Downloading is this command's whole job.  It used to rank the store's 1h volume and write
    # `universe.json` as well, which gave the file two meanings depending on which command ran last:
    # on 2026-09-04 it held PUMPUSDT from a manual sync while the loop traded CYSUSDT from its own
    # daily refresh, and research reads this file as "the universe".  One writer, one ranking rule.
    click.echo("selection unchanged: `beidou data pool refresh` re-ranks and writes universe.json (D-014)")


@data.command("status")
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--interval", default="1h", show_default=True)
def data_status(root: str, interval: str) -> None:
    """Show stored symbols, row counts and last bar."""
    store = KlineStore(root)
    universe = read_universe(root)
    holes = 0
    for symbol in store.symbols(interval):
        last = store.last_open_time(symbol, interval)
        stamp = "" if last is None else time.strftime("%Y-%m-%d %H:%M", time.gmtime(last / 1000))
        flag = "*" if symbol in universe else " "
        # T-D01: a monthly archive plus a REST tail can leave a hole that a backtest reads as a jump
        gaps = store.gaps(symbol, interval)
        holes += len(gaps)
        note = (
            "" if not gaps else f"  GAPS={len(gaps)} first={time.strftime('%Y-%m-%d', time.gmtime(gaps[0][0] / 1000))}"
        )
        click.echo(f"{flag} {symbol:<12} rows={store.count(symbol, interval):>7} last={stamp} UTC{note}")
    click.echo(f"universe: {', '.join(universe) if universe else '<not selected>'}")
    click.echo(f"gaps: {holes} missing stretch(es) across {len(store.symbols(interval))} symbols")


@data.group("pool")
def data_pool() -> None:
    """Trading pool: live refresh with hysteresis, and the point-in-time membership table for research."""


@data_pool.command("refresh")
@click.option("--universe", "universe_path", default="config/universe.yaml", show_default=True)
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--market-url", default="https://fapi.binance.com", show_default=True, help="volumes (mainnet)")
@click.option(
    "--venue-url", default="https://demo-fapi.binance.com", show_default=True, help="exchangeInfo for tradability"
)
@click.option("--candidates", default=45, show_default=True)
def pool_refresh(universe_path: str, root: str, market_url: str, venue_url: str, candidates: int) -> None:
    """Re-rank the live universe (30-day mainnet quote volume, enter/exit hysteresis) and write universe.json."""
    config = UniverseConfig.from_mapping(load_yaml(universe_path))
    with PublicClient(venue_url) as venue_public:
        rules = parse_exchange_info(venue_public.exchange_info())
    previous = read_universe(root)

    async def run() -> object:
        client = AsyncPublicClient(market_url)
        try:
            return await LivePool(client, config, candidates=candidates).select(previous, rules)
        finally:
            await client.aclose()

    update = asyncio.run(run())
    payload = update.to_dict()  # type: ignore[attr-defined]
    path = write_universe(
        root,
        payload["symbols"],
        {
            "selected_at_ms": payload["at_ms"],
            "entered": payload["entered"],
            "left": payload["left"],
            "volume_30d": payload["volumes"],
            "source": "pool-refresh",
        },
    )
    click.echo(f"universe ({len(payload['symbols'])}): {', '.join(payload['symbols'])}")
    click.echo(f"entered: {payload['entered'] or '-'}  left: {payload['left'] or '-'}")
    click.echo(f"written {path}")


@data_pool.command("history")
@click.option("--universe", "universe_path", default="config/universe.yaml", show_default=True)
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--market-url", default="https://fapi.binance.com", show_default=True)
@click.option("--start", default=None, help="first month YYYY-MM (default from universe.yaml)")
@click.option("--refresh", default="MS", show_default=True, help="pandas offset alias of the re-selection dates")
@click.option("--sync/--no-sync", default=True, show_default=True, help="refresh daily klines for every candidate")
@click.option(
    "--sync-members/--no-sync-members",
    default=False,
    show_default=True,
    help="also download hourly klines + funding for every symbol that was ever a member",
)
@click.option(
    "--always-include",
    default="BTCUSDT,ETHUSDT",
    show_default=True,
    help="pins for the historical selection (never import today's preferences into 2021)",
)
def pool_history(
    universe_path: str,
    root: str,
    market_url: str,
    start: str | None,
    refresh: str,
    sync: bool,
    sync_members: bool,
    always_include: str,
) -> None:
    """Rebuild point-in-time membership from daily quote volume of every USDT perpetual that ever had an archive."""
    base = UniverseConfig.from_mapping(load_yaml(universe_path))
    pins = tuple(s.strip().upper() for s in always_include.split(",") if s.strip())
    config = replace(base, always_include=pins)
    history_start = Month.parse(start or config.history_start)
    store = KlineStore(root)
    with PublicClient(market_url) as public, ArchiveClient() as archive:
        now_ms = public.server_time_ms()
        rules = parse_exchange_info(public.exchange_info())
        listed = {symbol for symbol, rule in rules.items() if rule.quote_asset == config.quote_asset}
        archived = {s for s in archive.list_symbols() if s.endswith(config.quote_asset)}
        candidates = sorted(listed | archived)
        click.echo(f"candidates: {len(candidates)} ({len(listed)} listed, {len(archived - listed)} archive-only)")
        if sync:
            started = time.monotonic()
            errors = 0
            for index, symbol in enumerate(candidates, start=1):
                report = sync_daily(
                    symbol,
                    store=store,
                    public=public,
                    archive=archive,
                    now_ms=now_ms,
                    start_ms=history_start.start_ms(),
                    listed=symbol in listed,
                )
                if report.error:
                    errors += 1
                    click.echo(f"    ! {symbol}: {report.error}")
                if index % 50 == 0:
                    click.echo(f"  daily sync {index}/{len(candidates)} ({time.monotonic() - started:.0f}s)")
            click.echo(f"daily sync done: {len(candidates)} symbols, {errors} errors")
        eligible = {
            s
            for s in candidates
            if s not in rules
            or (
                rules[s].contract_type == "PERPETUAL"
                and rules[s].quote_asset == config.quote_asset
                and (float(rules[s].min_notional) <= config.max_min_notional_usdt or s in pins)
            )
        }
        eligible -= set(config.exclude)
        volume = daily_quote_volume(store, candidates)
        if volume.empty:
            raise click.ClickException("no daily klines stored; run with --sync")
        volume = volume.loc[pd.Timestamp(history_start.start_ms(), unit="ms", tz="UTC") :]
        membership = point_in_time_membership(volume, config, eligible=eligible, refresh=refresh)
        table_path = Path(root) / MEMBERSHIP_FILE
        membership.to_parquet(table_path)
        summary = membership_summary(membership)
        summary["coverage"] = {
            "symbols_with_daily_data": int(volume.notna().any(axis=0).sum()),
            "symbol_days": int(volume.notna().sum().sum()),
            "eligible": len(eligible),
        }
        (Path(root) / "membership.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", "utf-8")
        click.echo(
            f"membership: {summary['refreshes']} refreshes {summary['first']} -> {summary['last']}, "
            f"mean size {summary['mean_size']:.1f}, {summary['changes_per_refresh']:.2f} changes/refresh, "
            f"union {len(summary['union'])} symbols"
        )
        by_year = membership.groupby(pd.DatetimeIndex(membership.index).year).apply(
            lambda block: sorted(block.columns[block.any()])
        )
        for year, members in by_year.items():
            click.echo(f"  {year}: {len(members)} distinct members")
        click.echo(f"written {table_path}")
        if sync_members:
            funding_store = FundingStore(root)
            members = list(summary["union"])
            for index, symbol in enumerate(members, start=1):
                member = sync_klines(
                    symbol,
                    config.interval,
                    history_start=history_start,
                    store=store,
                    archive=archive,
                    public=public,
                    now_ms=now_ms,
                )
                sync_funding(
                    symbol,
                    history_start=history_start,
                    store=funding_store,
                    public=public,
                    now_ms=now_ms,
                    report=member,
                )
                status = "OK" if not member.errors else f"ERRORS={len(member.errors)}"
                click.echo(
                    f"[{index}/{len(members)}] {symbol}: rows={member.total_rows} funding={member.funding_rows} {status}"
                )


__all__ = ["data_status", "data_sync", "pool_history", "pool_refresh"]
