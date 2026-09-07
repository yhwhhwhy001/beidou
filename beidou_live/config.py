"""Profile YAML -> LiveConfig + runtime objects (the only place that knows about concrete classes)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from beidou_alpha.model import AlphaModel
from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import DrawdownThrottleParams
from beidou_alpha.panel import interval_seconds
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import Registry, evidence_problems
from beidou_alpha.signals import get_signal
from beidou_data.live_feed import PublicMarketData
from beidou_data.manifest import ManifestCheck, build_manifest, manifest_check
from beidou_data.pool import LivePool
from beidou_data.universe import UniverseConfig
from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.binance_usdm.venue import BinanceUsdmVenue
from beidou_exchange.guard import WriteGuard
from beidou_live.composition import build_model, load_registry, portfolio_params, read_universe, write_universe
from beidou_live.engine import LiveConfig
from beidou_live.guards import GuardParams
from beidou_live.lock import account_kill_switch_path
from beidou_live.ports import UniverseUpdate
from beidou_live.probe import probes_from_registry
from beidou_live.rebalancer import RebalanceParams
from beidou_live.state import StateStore
from beidou_shared.config import env_secret, load_yaml


def load_profile(path: str | Path) -> dict[str, Any]:
    return load_yaml(path)


def resolve_universe(
    profile: dict[str, Any], override: Sequence[str] | None = None, data_root: str | Path = ".beidou/data"
) -> list[str]:
    if override:
        return [symbol.upper() for symbol in override]
    selected = read_universe(data_root)
    if selected:
        return selected
    universe_cfg = load_yaml(profile.get("universe", "config/universe.yaml"))
    return [str(symbol) for symbol in universe_cfg.get("always_include", [])]


def account_kill_switches(profile: dict[str, Any]) -> tuple[Path, ...]:
    """The account-scoped kill switch, when the profile names an API key env var (L1-07).

    Empty when the credential is not available - paper and offline runs have no account to scope to,
    and an absent switch is correctly "not engaged" rather than an error.
    """
    venue = profile.get("venue", {}) or {}
    name = str(venue.get("api_key_env", ""))
    key = os.environ.get(name, "") if name else ""
    return (account_kill_switch_path(key),) if key else ()


def live_config(profile: dict[str, Any], universe: Sequence[str], registry: Registry, *, dry_run: bool) -> LiveConfig:
    portfolio = profile.get("portfolio", {}) or {}
    guards = profile.get("guards", {}) or {}
    market = profile.get("market_data", {}) or {}
    pool = profile.get("pool", {}) or {}
    interval = str(market.get("interval", "1h"))
    leverage_raw = portfolio.get("leverage", 2)
    leverage_mode = "auto" if str(leverage_raw).lower() == "auto" else "fixed"
    bars_per_day = max(1, 86_400 // interval_seconds(interval))
    exits = ExitParams.from_mapping({**(profile.get("exits", {}) or {}), "bars_per_day": bars_per_day})
    throttle = DrawdownThrottleParams.from_mapping(profile.get("drawdown_throttle", {}) or {})
    return LiveConfig(
        interval=interval,
        history_bars=int(market.get("history_bars", 400)),
        universe=tuple(universe),
        leverage=2 if leverage_mode == "auto" else int(leverage_raw),
        rebalance=RebalanceParams(
            no_trade_band=float(portfolio.get("no_trade_band", 0.005)),
            no_trade_rel_band=float(portfolio.get("no_trade_rel_band", 0.0)),
            max_order_notional=(
                float(portfolio["max_order_notional"]) if portfolio.get("max_order_notional") else None
            ),
            max_participation=float(portfolio.get("max_participation", 0.0)),
        ),
        guards=GuardParams(
            daily_loss_pause=float(guards.get("daily_loss_pause", -0.05)),
            stale_bars_max=int(guards.get("stale_bars_max", 2)),
            max_gross=float(portfolio.get("max_gross", 2.0)),
            max_weight=float(portfolio.get("max_weight", 0.15)),
        ),
        # L1-07: absolute, always.  A relative default resolves against the working directory, so a
        # CLI run from a worktree engaged a switch the loop could not see - the same two-processes,
        # two-directories, one-account shape as DL-L1.
        kill_switch_path=Path(guards.get("kill_switch_path", ".beidou/live/KILL_SWITCH")).resolve(),
        # L1-07: and the one that does not move when the working directory does.
        kill_switch_paths=account_kill_switches(profile),
        # a book's fraction enters attribution here; contributions stay unscaled targets (D-019)
        strategy_weights={entry.id: entry.weight * registry.fraction(entry.book) for entry in registry.enabled},
        dry_run=dry_run,
        exits=exits,
        throttle=throttle,
        leverage_mode=leverage_mode,
        margin_cap=float(portfolio.get("margin_cap", 0.40)),
        max_leverage=int(portfolio.get("max_leverage", 5)),
        margin_buffer=float(portfolio.get("margin_buffer", 0.10)),
        universe_refresh=str(pool.get("refresh", "never")).lower() != "never",
        liquidity_window=int(pool.get("liquidity_window", 24)),
        quarantine_after=int(pool.get("quarantine_after", 0)),
        probes=probes_from_registry(registry),
        max_bar_alignment_ms=int(float(guards.get("max_bar_alignment_seconds", 60.0)) * 1000),
        # the same mapping the model reads, so the digest describes the book that actually ran
        portfolio=PortfolioParams.from_mapping(portfolio),
        # DL-X1: the collateral mode the live record was produced under, asserted at startup.  Not part
        # of `construction_fingerprint` on purpose - it describes the ACCOUNT, not the construction, so
        # adding it must not reset M-010's evidence window the way a weight change would.
        expect_multi_assets=bool((profile.get("venue", {}) or {}).get("multi_assets_margin", True)),
        min_liq_distance=float((profile.get("risk_budget", {}) or {}).get("min_liq_distance", 10.0)),
    )


def build_pool(profile: dict[str, Any], market: PublicMarketData) -> LivePool | None:
    """Daily universe refresh over the same public client as the market data feed (None when disabled)."""
    pool = profile.get("pool", {}) or {}
    if str(pool.get("refresh", "never")).lower() == "never":
        return None
    config = UniverseConfig.from_mapping(load_yaml(profile.get("universe", "config/universe.yaml")))
    return LivePool(market.client, config, candidates=int(pool.get("candidates", 0)))


def universe_sink(data_root: str | Path) -> Callable[[UniverseUpdate], None]:
    """Persist a refreshed selection to ``universe.json`` so restarts and research see the same pool."""

    def write(update: UniverseUpdate) -> None:
        payload = update.to_dict()
        write_universe(
            data_root,
            list(update.symbols),
            {
                "selected_at_ms": update.at_ms,
                "entered": list(update.entered),
                "left": list(update.left),
                "volume_30d": dict(payload.get("volumes", {}) or {}),
                "source": "live-refresh",
            },
        )

    return write


def registry_evidence_problems(registry: Registry, profile: dict[str, Any] | None = None) -> list[str]:
    """KILL-015 at startup: report exists, digest matches, params match, and the construction matches too.

    ``profile`` supplies the live portfolio construction so a band or half-life change cannot detach the
    book from its evidence unnoticed (D-026 records it; this refuses it).  Reports written before
    ``validate`` recorded its construction have no ``portfolio`` block and are skipped, so nothing in
    flight today is blocked.  A probe book (D-019) must also cite an ACCEPTed book report at the
    registry's fraction.
    """

    def sha256_of(path: str) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def read_report(path: str) -> dict[str, Any]:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def canonical(strategy_id: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return get_signal(strategy_id).canonical_params(params)

    live_portfolio = portfolio_params(profile).__dict__ if profile is not None else None
    problems: list[str] = []
    for entry in registry.enabled:
        fraction = registry.books[entry.book].fraction if entry.book in registry.books else None
        problems.extend(
            evidence_problems(
                entry,
                exists=lambda p: Path(p).exists(),
                sha256_of=sha256_of,
                read_report=read_report,
                book_fraction=fraction,
                canonical_params=canonical,
                live_portfolio=live_portfolio,
            )
        )
    return problems


def registry_dataset_problems(
    registry: Registry, data_root: str | Path = ".beidou/data", interval: str = "1h"
) -> ManifestCheck:
    """D-041: check each enabled strategy's cited dataset manifest against the data on disk.

    ``validate`` has written a manifest into every report since D-024 and nothing ever read one back,
    so the stale-evidence pointer it exists to catch could not be caught.  This is the reader; the
    severity split it relies on is documented in :data:`beidou_data.manifest.BLOCKING_FIELDS`.

    A missing or unreadable report is left alone - ``registry_evidence_problems`` already reports it,
    and one fault should be named once.
    """
    current = build_manifest(data_root, interval)
    blocking: list[str] = []
    advisory: list[str] = []
    for entry in registry.enabled:
        path = Path(str((entry.evidence or {}).get("report", "")))
        if not str(path) or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        recorded = payload.get("dataset") if isinstance(payload, dict) else None
        check = manifest_check(recorded if isinstance(recorded, dict) else None, current)
        blocking.extend(f"{entry.id}: {message}" for message in check.blocking)
        advisory.extend(f"{entry.id}: {message}" for message in check.advisory)
    return ManifestCheck(blocking, advisory)


def build_model_from_profile(profile: dict[str, Any]) -> tuple[AlphaModel, Registry]:
    registry = load_registry(profile.get("registry", "config/alpha_registry.yaml"))
    return build_model(registry, profile), registry


def build_market_data(profile: dict[str, Any]) -> PublicMarketData:
    market = profile.get("market_data", {}) or {}
    return PublicMarketData(str(market.get("rest_url", "https://fapi.binance.com")))


def build_venue(profile: dict[str, Any], kill_switch_path: Path | Sequence[Path]) -> BinanceUsdmVenue:
    venue_cfg = profile.get("venue", {}) or {}
    rest_url = str(venue_cfg.get("rest_url", "https://demo-fapi.binance.com"))
    guard = WriteGuard(rest_url, kill_switch_path)
    client = BinanceRestClient(
        rest_url,
        env_secret(str(venue_cfg.get("api_key_env", "BEIDOU_DEMO_API_KEY"))),
        env_secret(str(venue_cfg.get("api_secret_env", "BEIDOU_DEMO_API_SECRET"))),
        guard=guard,
        recv_window_ms=int(venue_cfg.get("recv_window_ms", 10_000)),
    )
    return BinanceUsdmVenue(client)


def build_store(profile: dict[str, Any], *, dry_run: bool) -> StateStore:
    """The loop's state directory, with rehearsals sent somewhere else (L1-13).

    A dry run used to share `paths.state_dir` with the real loop, so rehearsing appended to the live
    `cycles.jsonl` and rewrote `heartbeat.json` and `state.json`.  `live status --check` reads that
    heartbeat to decide whether the loop is alive and the daily report counts those cycle rows, so a
    rehearsal could make a dead loop look fresh, or put bars into a metric meant to describe what the
    book actually did.

    The suffix is derived rather than configured: a separate key would have to be remembered exactly
    when someone is in a hurry, which is when rehearsals happen, and the live profile sets `state_dir`
    explicitly - so honouring an explicit setting would have kept the bug for the one profile anybody
    actually rehearses.
    """
    paths = profile.get("paths", {}) or {}
    directory = Path(paths.get("state_dir", ".beidou/live"))
    return StateStore(directory.with_name(directory.name + "-dry-run") if dry_run else directory)
