"""Composition helpers shared by the CLI and the live loop: stores -> Panel, config -> model/costs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.backtest import CostModel
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import Registry, parse_registry
from beidou_data.store import FundingStore, KlineStore, funding_per_bar
from beidou_shared.config import load_yaml

UNIVERSE_STATE = "universe.json"


def load_panel(
    store: KlineStore,
    symbols: Sequence[str],
    interval: str,
    *,
    funding_store: FundingStore | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Panel:
    start_ms = None if start is None else int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = None if end is None else int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
    frames = {symbol: store.load(symbol, interval, start_ms, end_ms) for symbol in symbols}
    frames = {symbol: frame for symbol, frame in frames.items() if not frame.empty}
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
