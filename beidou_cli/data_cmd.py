"""``beidou data ...`` commands."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import click
import pandas as pd

from beidou_cli import data
from beidou_data.alignment import verify_stamp_offset
from beidou_data.archive import ArchiveClient, Month
from beidou_data.binance_public import AsyncPublicClient, PublicClient, drop_unclosed
from beidou_data.index_price import IndexPriceClient, index_columns_for_live
from beidou_data.macro import (
    BY_COLUMN,
    MACRO_SERIES,
    REFERENCE_OPEN,
    AlfredClient,
    BlsWitnessClient,
    FredApiKeyMissing,
    align_releases_to_bars,
    api_key,
    revision_leak,
    verify_macro_contract,
)
from beidou_data.macro import admits_live_signal as macro_admits_live_signal
from beidou_data.metrics_archive import MetricsArchiveClient, sync_metrics
from beidou_data.onchain import (
    ONCHAIN,
    PANEL_COLUMNS,
    WITNESS_COLUMN,
    WITNESS_TOLERANCE,
    CommunityClient,
    WitnessClient,
    revision_evidence,
    store_kind,
    sync_onchain,
    to_contract_frame,
)
from beidou_data.onchain import admits_live_signal as onchain_admits_live_signal
from beidou_data.onchain import map_universe as map_onchain_universe
from beidou_data.pool import (
    MEMBERSHIP_FILE,
    LivePool,
    daily_quote_volume,
    membership_summary,
    point_in_time_membership,
    sync_daily,
)
from beidou_data.spot import (
    SPOT_MARKET,
    SpotClient,
    map_universe,
    measure_alignment,
    write_spot_map,
)
from beidou_data.store import SPOT_KLINE_KIND, FundingStore, KlineStore, MetricsStore, interval_ms
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


@data.command("metrics")
@click.option("--root", default=".beidou/data", show_default=True, help="parquet store root")
@click.option("--symbols", required=True, help="comma-separated symbols to ingest")
@click.option("--from", "start", required=True, help="first day YYYY-MM-DD")
@click.option("--to", "end", required=True, help="last day YYYY-MM-DD, exclusive")
@click.option(
    "--workers",
    default=8,
    show_default=True,
    help=(
        "Fetch this many SYMBOLS at once.  Measured per symbol-day: 0.06s of CPU against 0.58s of "
        "waiting, so this is the axis with the headroom.  Writes stay on one thread."
    ),
)
def data_metrics(root: str, symbols: str, start: str, end: str, workers: int) -> None:
    """Ingest the daily futures-metrics archive (DL-D2), resuming from what the store already holds.

    Research-side only, and deliberately so: live can read metrics from the 30-day REST window and
    nothing else, so a strategy that declares `needs_metrics` is refused at startup until a live
    source can answer for it (`metrics_refusal`).  Ingesting without that gate would be KILL-027 in
    its purest form - a research panel strictly larger than the live one, arriving silently.

    Measured 2026-09-07: 0.69s per symbol-day sequentially.  That number is a LOWER BOUND and was
    measured on a nearly empty store: until 2026-09-09 this function appended once per day, and
    ``MetricsStore.append`` rewrites the symbol's whole parquet, so the real cost grew with the days
    already held - a 2,077-day symbol wrote about 621 million rows to store 598 thousand.  It now
    gathers a symbol's days and appends once, and ``--workers`` fetches several symbols at a time.
    """
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    store = MetricsStore(root)
    before = {s: store.last_open_time(s) for s in wanted}

    def progress(symbol: str, done: int, total: int) -> None:
        click.echo(f"[{done}/{total}] {symbol}", err=True)

    with MetricsArchiveClient() as client:
        totals = sync_metrics(client, store, wanted, start=start, end=end, workers=workers, progress=progress)
    for symbol in wanted:
        after = store.last_open_time(symbol)
        rows = totals.get(symbol)
        if after is None:
            click.echo(f"{symbol}: nothing stored (the archive published no day in [{start}, {end}))")
            continue
        stamp = datetime.fromtimestamp(after / 1000, tz=UTC).isoformat()
        moved = "unchanged" if before[symbol] == after else f"advanced to {stamp}"
        click.echo(f"{symbol}: {rows if rows is not None else store.load(symbol).shape[0]} rows stored, {moved}")


@data.command("spot")
@click.option("--universe", "universe_path", default="config/universe.yaml", show_default=True)
@click.option("--root", default=".beidou/data", show_default=True, help="parquet store root")
@click.option("--symbols", default="", help="comma-separated perpetuals (default: every symbol the kline store holds)")
@click.option("--interval", default="1h", show_default=True)
@click.option("--start", default=None, help="first archive month YYYY-MM (default from universe.yaml)")
@click.option("--spot-url", default="https://api.binance.com", show_default=True)
def data_spot(universe_path: str, root: str, symbols: str, interval: str, start: str | None, spot_url: str) -> None:
    """Ingest SPOT klines for the perpetuals' spot legs and print the alignment contract (DL-D5).

    The mapping is resolved against the spot venue's own listing and written to ``spot_map.json``, so
    research and the live loop resolve a perpetual to the same spot symbol instead of each re-deriving
    it from the name - which gets 1000SATSUSDT and three others wrong, silently and by a factor of 1000.

    The alignment check runs every time rather than behind a flag.  T-D5-1 is a claim about two live
    series ("same grid, no offset, right multiplier"), and a claim that is only tested against fixtures
    is a claim about the fixtures; this is the one place both real series are on disk together.
    """
    config = UniverseConfig.from_mapping(load_yaml(universe_path))
    history_start = Month.parse(start or config.history_start)
    perp_store = KlineStore(root)
    spot_store = KlineStore(root, kind=SPOT_KLINE_KIND)
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or perp_store.symbols(interval)
    if not wanted:
        raise click.ClickException(f"no perpetuals to map: {perp_store.directory} holds no {interval} klines")
    with SpotClient(spot_url) as client, ArchiveClient() as archive:
        now_ms = client.server_time_ms()
        mappings = map_universe(wanted, client.listed_symbols())
        write_spot_map(root, mappings, {"measured_at_ms": now_ms, "interval": interval})
        absent = sorted(perp for perp, mapping in mappings.items() if not mapping.exists)
        click.echo(f"spot legs: {len(wanted) - len(absent)}/{len(wanted)} mapped, {len(absent)} with no spot listing")
        if absent:
            click.echo(f"  no spot leg: {', '.join(absent)}")
        for perp, mapping in sorted(mappings.items()):
            if mapping.spot is None:
                continue
            report = sync_klines(
                mapping.spot,
                interval,
                history_start=history_start,
                store=spot_store,
                archive=archive,
                public=client,
                now_ms=now_ms,
                market=SPOT_MARKET,
            )
            note = " NOT LISTED" if report.not_listed else ""
            scale = "" if mapping.multiplier == 1.0 else f" x{mapping.multiplier:g}"
            click.echo(f"{perp} -> {mapping.spot}{scale}: {report.total_rows} rows{note} {'; '.join(report.errors)}")
    for perp, mapping in sorted(mappings.items()):
        if mapping.spot is None:
            continue
        try:
            # Either leg can be absent - a perpetual outside this root, or a spot symbol whose archive
            # published nothing and whose tail failed.  Saying which is missing beats ending the run on
            # a FileNotFoundError after every download has already been paid for.
            frames = (perp_store.load(perp, interval), spot_store.load(mapping.spot, interval))
        except FileNotFoundError as exc:
            click.echo(f"{perp}/{mapping.spot}: alignment not measured ({exc})")
            continue
        evidence = measure_alignment(*frames, mapping=mapping, interval_ms=interval_ms(interval))
        verdict = "OK" if evidence.aligned else f"REFUSED ({evidence.reason})"
        click.echo(
            f"{perp}/{mapping.spot}: overlap={evidence.overlap_bars} lag={evidence.best_lag_bars:+d} "
            f"median|log ratio|={evidence.median_abs_log_ratio:.5f} p95={evidence.p95_abs_log_ratio:.5f} {verdict}"
        )


@data.command("onchain")
@click.option("--root", default=".beidou/data", show_default=True, help="parquet store root")
@click.option("--symbols", default="", help="comma-separated perpetuals (default: every symbol the kline store holds)")
@click.option("--from", "start", required=True, help="first day YYYY-MM-DD")
@click.option("--to", "end", required=True, help="last day YYYY-MM-DD, inclusive")
@click.option("--interval", default="1h", show_default=True, help="which kline store supplies the default symbols")
@click.option(
    "--witness/--no-witness",
    default=True,
    show_default=True,
    help="re-measure the day offset against blockchain.info before saying which columns may reach live",
)
def data_onchain(root: str, symbols: str, start: str, end: str, interval: str, witness: bool) -> None:
    """Ingest the Coin Metrics community dailies (#31) and print the RISK-G3 gate on every run.

    Keyed by ASSET, never by perpetual: `store_kind`'s note says why, and `sync_onchain` counts the
    same way, so a re-run resumes on the store's own watermark and two perpetuals sharing one chain are
    downloaded once.  Coverage is thin by nature - 49 of 528 perpetuals map to a community asset - so
    the mapped/unmapped split is printed rather than left to be inferred from a short list of results.

    The witness comparison runs every time rather than behind a flag, for `data spot`'s reason: what
    the contract claims is that Coin Metrics' day stamp means what it says, and a claim only ever
    checked against fixtures is a claim about the fixtures.  blockchain.info is the second, independent
    computation of BTC transaction counts that makes the check able to fail; it is never stored.  With
    no verification on record every column is REFUSED, which is the honest state and not a failure -
    ingesting a column and admitting it to live are different acts (KILL-027).
    """
    store = MetricsStore(root, kind=store_kind())
    perp_store = KlineStore(root)
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or perp_store.symbols(interval)
    if not wanted:
        raise click.ClickException(f"no perpetuals to map: {perp_store.directory} holds no {interval} klines")
    with CommunityClient() as client:
        covered = client.covered_assets()
        mappings = map_onchain_universe(wanted, covered)
        assets = sorted({m.asset for m in mappings.values() if m.asset is not None})
        click.echo(
            f"on-chain legs: {len(assets)} assets for {sum(1 for m in mappings.values() if m.exists)}/{len(wanted)} "
            f"perpetuals, against {len(covered)} community assets"
        )
        result = sync_onchain(client, store, mappings, start=start, end=end)
    for asset in assets:
        perps = ", ".join(sorted(perp for perp, m in mappings.items() if m.asset == asset))
        rows = result.stored.get(asset)
        last = store.last_open_time(asset)
        stamp = "-" if last is None else datetime.fromtimestamp(last / 1000, tz=UTC).date().isoformat()
        stored = "nothing published in the window" if rows is None else f"{rows} rows stored, last day {stamp}"
        click.echo(f"{asset} ({perps}): {stored}")
    for asset, why in sorted(result.failures.items()):
        click.echo(f"    ! {asset}: {why}")
    frames = {asset: store.load(asset) for asset in sorted(result.stored)}
    verification = None
    btc = frames.get("btc")
    if not witness:
        click.echo("witness: skipped, so no column can be admitted this run")
    elif btc is None or btc.empty:
        click.echo("witness: nothing stored for btc, and it is the only asset the free witness publishes")
    else:
        with WitnessClient() as second_opinion:
            counts = second_opinion.transactions_per_day()
        verification = verify_stamp_offset(
            ONCHAIN,
            to_contract_frame(btc, ONCHAIN.archive),
            counts,
            value_columns=[WITNESS_COLUMN],
            tolerance=WITNESS_TOLERANCE,
        )
        click.echo(f"witness: {verification.verdict} - {verification.reason}")
    for column in PANEL_COLUMNS:
        # The worst asset decides the column.  Revision evidence is per CELL (point 3), so a column is
        # only as clean as the latest-written cell any stored asset carries; taking btc's alone would
        # let a backfilled chain in behind a well-behaved one.
        evidence = max(
            (revision_evidence(frame, column) for frame in frames.values()), key=lambda e: e.late, default=None
        )
        admitted, reason = onchain_admits_live_signal(column, verification, evidence)
        click.echo(f"{'ADMITTED' if admitted else 'REFUSED '} {column}: {reason}")


def index_store_kind(interval: str) -> str:
    """The `kind` an index store is opened with, and the interval is part of it.

    `MetricsStore` is reused rather than copied for `onchain.store_kind`'s reason - one parquet per key,
    append-and-dedupe on `open_time`, which is already the right merge rule - but it keys on the SYMBOL
    alone, so two bar sizes sharing a directory would merge into one file on a key that cannot tell them
    apart and every second row would silently belong to the other grid.

    It lives here rather than in `beidou_data.index_price` because #29 shipped without a store
    deliberately; this command is the entry point that module was waiting for, not a redesign of it.
    """
    return f"index_klines_{interval}"


@data.command("index")
@click.option("--universe", "universe_path", default="config/universe.yaml", show_default=True)
@click.option("--root", default=".beidou/data", show_default=True, help="parquet store root")
@click.option("--symbols", default="", help="comma-separated perpetuals (default: every symbol the kline store holds)")
@click.option("--interval", default="1h", show_default=True)
@click.option("--start", default=None, help="first month YYYY-MM for a symbol the store has nothing for")
@click.option("--market-url", default="https://fapi.binance.com", show_default=True)
def data_index(universe_path: str, root: str, symbols: str, interval: str, start: str | None, market_url: str) -> None:
    """Ingest Binance's INDEX price beside each perpetual (#29), resuming from what the store holds.

    REST only.  The daily archive is a bulk convenience and either source covers 2019-12-23 onward
    (note 7), so the tail is one page per symbol per day and the first run backfills from
    `universe.yaml`'s `history_start`.  `drop_unclosed` is not optional here: the venue serves the
    bucket IN PROGRESS - measured at 2364 of an eventual 3600 trades - and `close_time` survives the
    parse precisely so that row can be told from a finished one.

    One symbol's failure is printed and skipped rather than raised, which `metrics_archive.sync_metrics`
    learned on a six-hour job: the store is the watermark, so ending the run throws away every other
    symbol's work.

    No verification is attempted and therefore no column is admitted.  Saying so on every run is the
    point: `verify_index_contract` needs the archive day BESIDE this REST page, two independent
    renderings, and comparing a source against itself would report a perfect score over a round trip
    (note 8).  A stored column and a live-admissible column are different things.
    """
    config = UniverseConfig.from_mapping(load_yaml(universe_path))
    history_start = Month.parse(start or config.history_start)
    perp_store = KlineStore(root)
    store = MetricsStore(root, kind=index_store_kind(interval))
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or perp_store.symbols(interval)
    if not wanted:
        raise click.ClickException(f"no perpetuals to price: {perp_store.directory} holds no {interval} klines")
    with IndexPriceClient(market_url) as client:
        now_ms = client.server_time_ms()
        for position, symbol in enumerate(wanted, start=1):
            last = store.last_open_time(symbol)
            start_ms = history_start.start_ms() if last is None else last + 1
            try:
                page = client.klines_range(symbol, interval, start_ms, now_ms)
            except Exception as exc:
                # Broad on purpose, and named rather than counted: a pair the venue no longer lists
                # answers HTTP 400 and a transport error answers nothing, and neither is a reason to
                # abandon the other eighty symbols' downloads.  `sync_onchain` records failures the
                # same way for the same reason.
                click.echo(f"[{position}/{len(wanted)}] {symbol}: ! {type(exc).__name__}: {exc}")
                continue
            fresh = drop_unclosed(page, now_ms)
            if fresh.empty:
                click.echo(f"[{position}/{len(wanted)}] {symbol}: no closed bar the store did not already hold")
                continue
            rows = store.append(symbol, fresh)
            newest = datetime.fromtimestamp(int(fresh["open_time"].iloc[-1]) / 1000, tz=UTC)
            click.echo(
                f"[{position}/{len(wanted)}] {symbol}: +{len(fresh)} bars, {rows} stored, "
                f"last={newest:%Y-%m-%d %H:%M} UTC"
            )
    admitted, refused = index_columns_for_live(None)
    click.echo(f"live gate: {len(admitted)}/{len(admitted) + len(refused)} index columns may reach live")
    for column, reason in sorted(refused.items()):
        click.echo(f"  REFUSED {column}: {reason}")


@data.command("macro")
@click.option("--from", "start", default="2019-01-01", show_default=True, help="first reference month YYYY-MM-DD")
@click.option("--to", "end", default=None, help="last reference month YYYY-MM-DD (default: today)")
@click.option("--columns", default="", help="comma-separated macro columns (default: the three pre-registered)")
def data_macro(start: str, end: str | None, columns: str) -> None:
    """Re-verify the #32 macro contract against both publishers, and report the point-in-time evidence.

    Nothing lands on disk, and that is #32's own scope call rather than an omission: a monthly release
    ledger is re-served in full on every request, so the store this feed would need is the panel's, not
    a fourth copy of append-and-dedupe.  What the command produces is the two REFUSALS a macro column
    has to clear - does FRED's reference-period stamp mean what BLS says it means, and was every value
    a bar reads already published when that bar closed.  The second is the one the whole module exists
    for: a naive frame leaks 67,200 of 67,200 bars.

    The bars are hourly and span the requested window because the leak count is a property of the GRID
    a signal would read on, not of the ledger; on any coarser bar the same ledger leaks differently.
    """
    # FRED authenticates by QUERY PARAMETER only - it offers no header auth - and httpx logs the full
    # request URL at INFO.  So anything that turns logging up (a launchd job's own config, a later
    # `-v`) would write the key into a log file, and `beidou_data.macro` can only redact its own
    # exception path.  The level belongs at the entry point, because the entry point is where it is set.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    last_day = end or datetime.now(tz=UTC).date().isoformat()
    names = [c.strip() for c in columns.split(",") if c.strip()]
    unknown = [name for name in names if name not in BY_COLUMN]
    if unknown:
        raise click.ClickException(f"no such macro column: {unknown}; known: {sorted(BY_COLUMN)}")
    wanted = [BY_COLUMN[name] for name in names] or list(MACRO_SERIES)
    try:
        key = api_key()
    except FredApiKeyMissing as exc:
        raise click.ClickException(str(exc)) from None
    bars = pd.date_range(start, last_day, freq="1h", tz="UTC")
    period_ms = interval_ms("1h")
    with AlfredClient(key) as alfred, BlsWitnessClient() as bls:
        for series in wanted:
            ledger = alfred.release_ledger(series, start=start, end=last_day)
            published = bls.series(series, start_year=int(start[:4]), end_year=int(last_day[:4]))
            verification = verify_macro_contract(ledger, published, series.column)
            aligned = align_releases_to_bars(ledger, bars, series.column, interval_ms=period_ms)
            leak = revision_leak(aligned, ledger, series.column, interval_ms=period_ms)
            admitted, reason = macro_admits_live_signal(series.column, verification, leak)
            click.echo(
                f"{series.column} ({series.fred_id} vs BLS {series.bls_id}): {len(ledger)} releases over "
                f"{ledger[REFERENCE_OPEN].nunique() if not ledger.empty else 0} reference months, "
                f"{len(published)} witness months"
            )
            click.echo(f"  contract: {verification.verdict} - {verification.reason}")
            click.echo(f"  {'ADMITTED' if admitted else 'REFUSED '}: {reason}")


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
        # Delisted names pass unconditionally (`s not in rules`), which is what keeps the point-in-time
        # universe survivorship-free.  The residual look-ahead, named because it is small rather than
        # absent (2026-09-08 audit): a symbol the venue still lists is filtered by TODAY's contract type
        # and min-notional over its WHOLE history, so one whose min-notional was raised recently is
        # excluded from years in which it qualified.  Binance changes these rarely and the direction is
        # not systematic; recorded rather than fixed, since a point-in-time exchangeInfo archive does
        # not exist to fix it with.
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
