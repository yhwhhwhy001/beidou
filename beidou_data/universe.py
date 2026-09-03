"""Universe selection: top-N liquid USDT perpetuals with rank hysteresis."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from beidou_shared.types import InstrumentRules


@dataclass(frozen=True)
class UniverseConfig:
    quote_asset: str = "USDT"
    top_n: int = 15
    always_include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_min_notional_usdt: float = 20.0
    enter_rank: int = 15
    exit_rank: int = 20
    volume_lookback_days: int = 30
    interval: str = "1h"
    history_start: str = "2021-01"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> UniverseConfig:
        hysteresis = payload.get("hysteresis", {}) or {}
        return cls(
            quote_asset=str(payload.get("quote_asset", "USDT")),
            top_n=int(payload.get("top_n", 15)),
            always_include=tuple(str(s) for s in payload.get("always_include", []) or []),
            exclude=tuple(str(s) for s in payload.get("exclude", []) or []),
            max_min_notional_usdt=float(payload.get("max_min_notional_usdt", 20.0)),
            enter_rank=int(hysteresis.get("enter_rank", payload.get("top_n", 15))),
            exit_rank=int(hysteresis.get("exit_rank", int(payload.get("top_n", 15)) + 5)),
            volume_lookback_days=int(payload.get("volume_lookback_days", 30)),
            interval=str(payload.get("interval", "1h")),
            history_start=str(payload.get("history_start", "2021-01")),
        )


def eligible_symbols(rules: Mapping[str, InstrumentRules], config: UniverseConfig) -> list[str]:
    chosen: list[str] = []
    for symbol, rule in rules.items():
        if symbol in config.exclude:
            continue
        if not rule.tradable or rule.quote_asset != config.quote_asset:
            continue
        if float(rule.min_notional) > config.max_min_notional_usdt and symbol not in config.always_include:
            continue
        chosen.append(symbol)
    return sorted(chosen)


def select_universe(
    volume_by_symbol: Mapping[str, float],
    rules: Mapping[str, InstrumentRules],
    config: UniverseConfig,
    previous: Iterable[str] = (),
) -> list[str]:
    """Rank eligible symbols by volume; enter at ``enter_rank``, leave only beyond ``exit_rank``."""
    eligible = set(eligible_symbols(rules, config))
    ranked = sorted((s for s in eligible if s in volume_by_symbol), key=lambda s: -float(volume_by_symbol[s]))
    rank = {symbol: position + 1 for position, symbol in enumerate(ranked)}
    keep: set[str] = {s for s in config.always_include if s in rules}
    previous_set = set(previous)
    for symbol, position in rank.items():
        if position <= config.enter_rank or (symbol in previous_set and position <= config.exit_rank):
            keep.add(symbol)
    ordered = sorted(keep, key=lambda s: rank.get(s, 10**6))
    limit = max(config.top_n, config.exit_rank) + len(config.always_include)
    return ordered[:limit]
