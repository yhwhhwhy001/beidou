"""How much of the book is one or three names (09-29 checklist N3) - reported, never enforced.

The template asks that a strategy not depend on a single asset, and the evidence file had no reading for
it: `per_symbol_summary` exists, but only `research backtest` printed it.  The failure is not
hypothetical here - flow's edge came from names that later left the pool (`config/alpha_registry.yaml`).

Contributions are each symbol's net P&L column of the backtest, summed.  The leave-one-out Sharpe drops one
symbol's column from the book's return without re-pricing the rest: weights are NOT renormalised, so it
understates how much the book would change and is an approximation that says so.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from beidou_alpha.validation.metrics import sharpe


def _shares(net_by_symbol: pd.DataFrame) -> dict[str, Any]:
    per_symbol = net_by_symbol.sum(axis=0).sort_values(ascending=False)
    total = float(per_symbol.sum())
    top = per_symbol.head(3)
    return {
        "net_pnl_total": total,
        "top": [[str(name), float(value)] for name, value in top.items()],
        "top1_share": float(top.iloc[0] / total) if total > 0 and len(top) else None,
        "top3_share": float(top.sum() / total) if total > 0 and len(top) else None,
    }


def concentration(full: pd.DataFrame, oos: pd.DataFrame, bars_per_year: float) -> dict[str, Any]:
    """``full``: net P&L by symbol, full sample; ``oos``: the same for the walk-forward out-of-sample bars."""
    book = oos.sum(axis=1)
    worst: tuple[str, float] | None = None
    for name in oos.columns:
        if not oos[name].any():
            continue
        without = sharpe(book - oos[name].fillna(0.0), bars_per_year)
        if without is not None and (worst is None or without < worst[1]):
            worst = (str(name), without)
    return {
        "enforced": False,
        "full_sample": _shares(full),
        "oos": {
            **_shares(oos),
            "sharpe": sharpe(book, bars_per_year),
            "leave_one_out_min_sharpe": None if worst is None else worst[1],
            "leave_one_out_min_without": None if worst is None else worst[0],
        },
    }
