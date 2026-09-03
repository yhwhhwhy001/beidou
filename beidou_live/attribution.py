"""Attribute realised venue income to strategies via the previous cycle's contributions; flag external cash flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
# Money that arrives or leaves without a trade: deposits/withdrawals, a demo-account reset (E-044 showed up as
# two TRANSFER rows and vanished positions), collateral swaps.  They make equity non-comparable across cycles.
EXTERNAL_FLOW_TYPES = frozenset(
    {
        "TRANSFER",
        "INTERNAL_TRANSFER",
        "WELCOME_BONUS",
        "CROSS_COLLATERAL_TRANSFER",
        "COIN_SWAP_DEPOSIT",
        "COIN_SWAP_WITHDRAW",
        "AUTO_EXCHANGE",
    }
)


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


def external_flows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum of non-trading cash flows in ``rows``: ``{"total", "rows", "by_type"}`` (zero when there are none)."""
    total = 0.0
    count = 0
    by_type: dict[str, float] = {}
    for row in rows:
        kind = str(row.get("incomeType", ""))
        if kind not in EXTERNAL_FLOW_TYPES:
            continue
        try:
            amount = float(row.get("income", 0.0))
        except (TypeError, ValueError):
            continue
        total += amount
        count += 1
        by_type[kind] = by_type.get(kind, 0.0) + amount
    return {"total": total, "rows": count, "by_type": by_type}


def strategy_shares(
    contributions: Mapping[str, Mapping[str, float]], strategy_weights: Mapping[str, float], symbol: str
) -> dict[str, float]:
    """Share of a symbol's income owned by each strategy: |w_k T_k| / sum_j |w_j T_j|.

    Magnitudes, not signed values: with a signed denominator two strategies
    that disagree on a symbol net to ~0 and the shares explode to +50x / -49x
    (E-050).  Splitting by conviction magnitude keeps every share in [0, 1].
    """
    signed = {
        k: float(strategy_weights.get(k, 1.0)) * float(contrib.get(symbol, 0.0)) for k, contrib in contributions.items()
    }
    total = sum(abs(value) for value in signed.values())
    if total < 1e-12:
        return {}
    return {k: abs(value) / total for k, value in signed.items() if abs(value) > 1e-12}


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
        "external_flows": external_flows(income_rows),
    }
