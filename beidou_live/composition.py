"""Composition helpers shared by the CLI and the live loop: stores -> Panel, config -> model/costs."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.backtest import CostModel, ImpactModel
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import Registry, parse_registry
from beidou_data.store import FundingStore, KlineStore, funding_per_bar
from beidou_shared.config import load_yaml

UNIVERSE_STATE = "universe.json"
logger = logging.getLogger(__name__)


def load_panel(
    store: KlineStore,
    symbols: Sequence[str],
    interval: str,
    *,
    funding_store: FundingStore | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Panel:
    """Wide panel from the parquet store; symbols without stored klines are excluded (and logged), not fatal.

    The live pool can admit a symbol (e.g. a fresh listing) before ``beidou data
    sync`` has ever downloaded it; research on ``universe.json`` must not crash
    on such a name.
    """
    start_ms = None if start is None else int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = None if end is None else int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        try:
            frame = store.load(symbol, interval, start_ms, end_ms)
        except FileNotFoundError:
            missing.append(symbol)
            continue
        if not frame.empty:
            frames[symbol] = frame
    if missing:
        logger.warning("no %s klines stored for %s; excluded from the panel", interval, missing)
    if not frames:
        raise ValueError("no kline data for the requested symbols/range")
    panel = Panel.from_frames(frames, interval=interval)
    if funding_store is not None:
        funding = {symbol: funding_per_bar(funding_store.load(symbol), panel.index) for symbol in panel.symbols}
        panel = Panel.from_frames(frames, interval=interval, funding=funding)
    return panel


def load_registry(path: str | Path) -> Registry:
    return parse_registry(load_yaml(path))


def portfolio_params(profile: Mapping[str, Any]) -> PortfolioParams:
    return PortfolioParams.from_mapping(profile.get("portfolio", {}) or {})


def cost_model(costs: Mapping[str, Any], *, use_funding: bool | None = None) -> CostModel:
    turnover = float(costs.get("taker_fee_bps", 5.0)) + float(costs.get("slippage_bps", 2.0))
    funding = bool(costs.get("use_actual_funding", True)) if use_funding is None else use_funding
    return CostModel(turnover_bps=turnover, carry_bps_per_bar=0.0, use_funding=funding)


def impact_model(costs: Mapping[str, Any], *, capital: float = 0.0) -> ImpactModel:
    """DL-C1.  ``capital`` of 0 keeps the flat, scale-free model, which is this model's own limit."""
    block = costs.get("impact") or {}
    return ImpactModel(
        capital=float(capital),
        coefficient=float(block.get("coefficient", 1.0)),
        adv_window=int(block.get("adv_window_bars", 720)),
        vol_window=int(block.get("vol_window_bars", 720)),
    )


def build_model(registry: Registry, profile: Mapping[str, Any]) -> AlphaModel:
    interval = str((profile.get("market_data", {}) or {}).get("interval", "1h"))
    min_history = int((profile.get("portfolio", {}) or {}).get("min_history_bars", 720))
    return AlphaModel.from_registry(registry, portfolio_params(profile), interval, min_history_bars=min_history)


def read_universe(root: str | Path) -> list[str]:
    path = Path(root) / UNIVERSE_STATE
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [str(symbol) for symbol in payload.get("symbols", [])]


def write_universe(root: str | Path, symbols: Sequence[str], meta: Mapping[str, Any] | None = None) -> Path:
    path = Path(root) / UNIVERSE_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"symbols": list(symbols), **dict(meta or {})}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
