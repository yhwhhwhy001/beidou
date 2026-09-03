"""Profile YAML -> LiveConfig + runtime objects (the only place that knows about concrete classes)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from beidou_alpha.model import AlphaModel
from beidou_alpha.registry import Registry, evidence_problems
from beidou_data.live_feed import PublicMarketData
from beidou_exchange.binance_usdm.rest_client import BinanceRestClient
from beidou_exchange.binance_usdm.venue import BinanceUsdmVenue
from beidou_exchange.guard import WriteGuard
from beidou_live.composition import build_model, load_registry, read_universe
from beidou_live.engine import LiveConfig
from beidou_live.guards import GuardParams
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
    return LiveConfig(
        interval=str(market.get("interval", "1h")),
        history_bars=int(market.get("history_bars", 400)),
        universe=tuple(universe),
        leverage=int(portfolio.get("leverage", 2)),
        rebalance=RebalanceParams(
            no_trade_band=float(portfolio.get("no_trade_band", 0.005)),
            no_trade_rel_band=float(portfolio.get("no_trade_rel_band", 0.0)),
            max_order_notional=(
                float(portfolio["max_order_notional"]) if portfolio.get("max_order_notional") else None
            ),
        ),
        guards=GuardParams(
            daily_loss_pause=float(guards.get("daily_loss_pause", -0.05)),
            stale_bars_max=int(guards.get("stale_bars_max", 2)),
            max_gross=float(portfolio.get("max_gross", 2.0)),
            max_weight=float(portfolio.get("max_weight", 0.15)),
        ),
        kill_switch_path=Path(guards.get("kill_switch_path", ".beidou/live/KILL_SWITCH")),
        strategy_weights={entry.id: entry.weight for entry in registry.enabled},
        dry_run=dry_run,
    )


def registry_evidence_problems(registry: Registry) -> list[str]:
    def sha256_of(path: str) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    problems: list[str] = []
    for entry in registry.enabled:
        problems.extend(evidence_problems(entry, exists=lambda p: Path(p).exists(), sha256_of=sha256_of))
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
