"""The book against its own basket, in the evidence file (09-29 checklist N2) - reported, never enforced.

`research decompose` already prices a book against a constant long, and the live loop has D-045 and the
factor page (#140).  The evidence file the registry cites answered neither question: how much of the out-of-
sample return is the market's, and what the plain alternatives made over the same bars.

D-045's rules (`beidou_live/benchmark.py`) carry over where they can, and `BASIS` says where they do not:

* the basket is the caller's `benchmark_returns` over the run's membership, named by `benchmark_basket` in the
  block: point-in-time on a pit run, every panel symbol (hindsight) on a static one;
* t is Newey-West at 48 bars, clamped to a quarter of the sample; under 48 bars there is no fit;
* beta is read twice.  Live, the conditional fit (book on exposure x basket) takes an OPERATOR's exposure
  change - a new vol_target - out of the residual.  In a backtest the exposure is the signal's own choice, so
  the conditional fit books the signal's timing as beta, and "is this passive market exposure" is the
  constant fit's question;
* the signal state is printed beside them: over all-long bars a book is a constant long, and no regression
  finds signal alpha in bars where the signal made no distinction.

E-058 is why the correlation is not printed.  On 2026-09-04 a full-sample backtest correlation with the
equal-weight long read -0.18 and was written up as "not crypto beta"; the live book read +0.86 (RESEARCH_LOG
:379, :13640).  An unconditional correlation over a book whose sign changes answers the wrong question.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.validation.metrics import cagr, compound, max_drawdown, sharpe

NW_LAGS = 48
MAX_LAG_SHARE = 0.25
MIN_BARS = 48

#: Printed with every block, so a reader does not set these numbers beside the live D-045 page as like for like.
BASIS = {
    "returns": "simple per-bar returns; the live D-045 page regresses log returns",
    "basket": "equal weight, zero cost, rebalanced every bar: a volatile basket's compounded return is mostly drag, "
    "so compare Sharpe, not return",
    "conditional": "exposure is the book's own net exposure, so this fit counts the signal's timing as beta; whether "
    "the book is passive market exposure is the constant fit",
    "signal_state": "signs of the executed positions, flat bars included; the live page counts the signal's +/-1",
}


def nw_ols(y: np.ndarray, x: np.ndarray, lags: int = NW_LAGS) -> dict[str, Any] | None:
    """``y ~ 1 + x`` with Newey-West (Bartlett) t-statistics; None under `MIN_BARS` or on a degenerate design.

    `beidou_live.benchmark.nw_covariance`'s operations in its order, restated here because the research
    package may not import the live one; a test holds the two to the same numbers and the same constants.
    """
    design = np.column_stack([np.ones(len(x)), np.asarray(x, dtype=float)])
    observed = np.asarray(y, dtype=float)
    n, width = design.shape
    if n < MIN_BARS or n <= width or np.linalg.matrix_rank(design) < width:
        return None
    coefficients, *_ = np.linalg.lstsq(design, observed, rcond=None)
    resid = observed - design @ coefficients
    used = max(0, min(lags, int(n * MAX_LAG_SHARE), n - 1))
    inverse = np.linalg.inv(design.T @ design)
    scores = resid[:, None] * design
    meat = scores.T @ scores
    for lag in range(1, used + 1):
        gamma = scores[lag:].T @ scores[:-lag]
        meat = meat + (1.0 - lag / (used + 1)) * (gamma + gamma.T)
    se = np.sqrt(np.clip(np.diag(inverse @ meat @ inverse), 0.0, None))
    alpha, beta = float(coefficients[0]), float(coefficients[1])
    return {
        "beta": beta,
        "beta_t": beta / se[1] if se[1] > 0 else None,
        "alpha_bps_per_bar": alpha * 1e4,
        "alpha_t": alpha / se[0] if se[0] > 0 else None,
        "nw_lags": used,
        "nw_covers_intended_horizon": used >= lags,
    }


def _leg(returns: pd.Series, bars_per_year: float) -> dict[str, Any]:
    return {
        "return": compound(returns),
        "cagr": cagr(returns, bars_per_year),
        "sharpe": sharpe(returns, bars_per_year),
        "max_drawdown": max_drawdown(returns),
    }


def signal_state(weights: pd.DataFrame) -> dict[str, Any]:
    """Shares of bars by the sign pattern of the held book, and its mean net and gross exposure."""
    held = weights.fillna(0.0)
    longs, shorts = (held > 0).any(axis=1), (held < 0).any(axis=1)
    bars = max(len(held), 1)
    return {
        "all_long_share": float((longs & ~shorts).sum() / bars),
        "all_short_share": float((shorts & ~longs).sum() / bars),
        "two_sided_share": float((longs & shorts).sum() / bars),
        "flat_share": float((~longs & ~shorts).sum() / bars),
        "net_exposure_mean": float(held.sum(axis=1).mean()) if len(held) else None,
        "gross_exposure_mean": float(held.abs().sum(axis=1).mean()) if len(held) else None,
    }


def against_basket(
    book: pd.Series,
    weights: pd.DataFrame,
    basket: pd.Series,
    btc: pd.Series | None,
    bars_per_year: float,
    *,
    basket_name: str,
) -> dict[str, Any]:
    """``book``: net returns; ``weights``: the executed weights behind them, same bars; ``basket``: the equal-weight
    return under the run's execution, named ``basket_name``; ``btc``: BTCUSDT's asset return, or None."""
    held = weights.reindex(book.index).fillna(0.0)
    market = basket.reindex(book.index)
    ok = book.notna() & market.notna()
    y, m = book[ok].to_numpy(dtype=float), market[ok].to_numpy(dtype=float)
    exposure = held[ok].sum(axis=1).to_numpy(dtype=float)
    return {
        "enforced": False,
        "basket": basket_name,
        "bars": int(ok.sum()),
        "book": _leg(book[ok], bars_per_year),
        "basket_leg": _leg(market[ok], bars_per_year),
        "btc_buy_and_hold": None if btc is None else _leg(btc.reindex(book.index)[ok].fillna(0.0), bars_per_year),
        "constant": nw_ols(y, m),
        "conditional": nw_ols(y, exposure * m),
        "signal_state": signal_state(held[ok]),
        "basis": BASIS,
    }
