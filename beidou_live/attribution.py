"""Attribute realised venue income to strategies via the previous cycle's contributions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")


def summarize_income(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """{symbol: {REALIZED_PNL: x, COMMISSION: y, FUNDING_FEE: z, total: t}}"""
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        kind = str(row.get("incomeType", ""))
        if kind not in INCOME_TYPES:
            continue
        symbol = str(row.get("symbol", "") or "ACCOUNT")
        bucket = out.setdefault(symbol, dict.fromkeys(INCOME_TYPES, 0.0) | {"total": 0.0})
        try:
            amount = float(row.get("income", 0.0))
        except (TypeError, ValueError):
            continue
        bucket[kind] += amount
        bucket["total"] += amount
    return out


def strategy_shares(
    contributions: Mapping[str, Mapping[str, float]], strategy_weights: Mapping[str, float], symbol: str
) -> dict[str, float]:
    """Share of a symbol's position owned by each strategy: w_k * T_k / sum_j w_j * T_j (signed)."""
    signed = {
        k: float(strategy_weights.get(k, 1.0)) * float(contrib.get(symbol, 0.0)) for k, contrib in contributions.items()
    }
    total = sum(signed.values())
    if abs(total) < 1e-12:
        active = [k for k, value in signed.items() if abs(value) > 1e-12]
        if not active:
            return {}
        return {k: 1.0 / len(active) for k in active}
    return {k: value / total for k, value in signed.items() if abs(value) > 1e-12}


def attribute(
    income_rows: list[dict[str, Any]],
    contributions: Mapping[str, Mapping[str, float]],
    strategy_weights: Mapping[str, float],
) -> dict[str, Any]:
    by_symbol = summarize_income(income_rows)
    by_strategy: dict[str, float] = {}
    unattributed = 0.0
    for symbol, bucket in by_symbol.items():
        shares = strategy_shares(contributions, strategy_weights, symbol)
        if not shares:
            unattributed += bucket["total"]
            continue
        for strategy, share in shares.items():
            by_strategy[strategy] = by_strategy.get(strategy, 0.0) + share * bucket["total"]
    return {
        "by_symbol": by_symbol,
        "by_strategy": by_strategy,
        "unattributed": unattributed,
        "total": sum(bucket["total"] for bucket in by_symbol.values()),
    }
