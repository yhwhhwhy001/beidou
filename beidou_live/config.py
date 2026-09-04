"""Profile YAML -> LiveConfig + runtime objects (the only place that knows about concrete classes)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from beidou_alpha.model import AlphaModel
from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import DrawdownThrottleParams
from beidou_alpha.panel import interval_seconds
from beidou_alpha.registry import Registry, evidence_problems
from beidou_alpha.signals import get_signal
from beidou_data.live_feed import PublicMarketData
from beidou_data.pool import LivePool
from beidou_data.universe import UniverseConfig
from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.binance_usdm.venue import BinanceUsdmVenue
from beidou_exchange.guard import WriteGuard
from beidou_live.composition import build_model, load_registry, portfolio_params, read_universe, write_universe
from beidou_live.engine import LiveConfig
from beidou_live.guards import GuardParams
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
        kill_switch_path=Path(guards.get("kill_switch_path", ".beidou/live/KILL_SWITCH")),
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


def build_model_from_profile(profile: dict[str, Any]) -> tuple[AlphaModel, Registry]:
    registry = load_registry(profile.get("registry", "config/alpha_registry.yaml"))
    return build_model(registry, profile), registry


def build_market_data(profile: dict[str, Any]) -> PublicMarketData:
    market = profile.get("market_data", {}) or {}
    return PublicMarketData(str(market.get("rest_url", "https://fapi.binance.com")))


def build_venue(profile: dict[str, Any], kill_switch_path: Path) -> BinanceUsdmVenue:
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


def build_store(profile: dict[str, Any]) -> StateStore:
    paths = profile.get("paths", {}) or {}
    return StateStore(paths.get("state_dir", ".beidou/live"))
