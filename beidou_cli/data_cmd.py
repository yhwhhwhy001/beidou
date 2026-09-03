"""``beidou data ...`` commands."""

from __future__ import annotations

import time

import click

from beidou_cli import data
from beidou_data.archive import ArchiveClient, Month
from beidou_data.binance_public import PublicClient
from beidou_data.store import FundingStore, KlineStore
from beidou_data.sync import sync_funding, sync_klines
from beidou_data.universe import UniverseConfig, eligible_symbols, select_universe
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
        volume_30d = _trailing_quote_volume(store, candidate_list, config, now_ms)
        selected = select_universe(volume_30d, rules, config, previous=read_universe(root))
        path = write_universe(
            root, selected, {"selected_at_ms": now_ms, "interval": config.interval, "volume_30d": volume_30d}
        )
        click.echo(f"universe ({len(selected)}): {', '.join(selected)}")
        click.echo(f"written {path}")


def _trailing_quote_volume(
    store: KlineStore, symbols: list[str], config: UniverseConfig, now_ms: int
) -> dict[str, float]:
    window_ms = config.volume_lookback_days * 86_400_000
    volumes: dict[str, float] = {}
    for symbol in symbols:
        if not store.exists(symbol, config.interval):
            continue
        frame = store.load(symbol, config.interval, start_ms=now_ms - window_ms)
        if frame.empty:
            continue
        volumes[symbol] = float(frame["quote_volume"].sum())
    return volumes


@data.command("status")
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--interval", default="1h", show_default=True)
def data_status(root: str, interval: str) -> None:
    """Show stored symbols, row counts and last bar."""
    store = KlineStore(root)
    universe = read_universe(root)
    for symbol in store.symbols(interval):
        last = store.last_open_time(symbol, interval)
        stamp = "" if last is None else time.strftime("%Y-%m-%d %H:%M", time.gmtime(last / 1000))
        flag = "*" if symbol in universe else " "
        click.echo(f"{flag} {symbol:<12} rows={store.count(symbol, interval):>7} last={stamp} UTC")
    click.echo(f"universe: {', '.join(universe) if universe else '<not selected>'}")


__all__ = ["data_status", "data_sync"]
