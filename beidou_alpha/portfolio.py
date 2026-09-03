"""Portfolio construction: conviction -> vol-targeted, capped weights (fraction of equity).

Stage 1  w1 = target * vol_target / asset_vol         (each position sized to the target vol standalone)
Stage 2  w2 = w1 * vol_target / portfolio_vol(w1)      (EWMA covariance, causal)      scalar clipped
Stage 3  |w| <= max_weight,  sum |w| <= max_gross
Optionally a no-trade band suppresses tiny rebalances (path dependent, applied last).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import ewm_vol


@dataclass(frozen=True)
class PortfolioParams:
    vol_target: float = 0.15
    vol_halflife: int = 48
    covariance_halflife: int = 96
    min_asset_vol: float = 0.10
    max_weight: float = 0.15
    max_gross: float = 2.0
    max_scalar: float = 3.0
    no_trade_band: float = 0.0
    no_trade_rel_band: float = 0.0

    def __post_init__(self) -> None:
        if self.vol_target <= 0 or self.min_asset_vol <= 0 or self.max_weight <= 0 or self.max_gross <= 0:
            raise ValueError("portfolio parameters must be positive")
        if self.no_trade_band < 0 or self.no_trade_rel_band < 0 or self.max_scalar <= 0:
            raise ValueError("no-trade bands must be >= 0 and max_scalar > 0")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> PortfolioParams:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)


def ewma_portfolio_vol(returns: pd.DataFrame, weights: pd.DataFrame, halflife: int, bars_per_year: float) -> pd.Series:
    """Annualised sqrt(w_t' Sigma_t w_t) with an EWMA covariance recursion; Sigma_t uses returns through t."""
    r = returns.fillna(0.0).to_numpy(dtype=float)
    w = weights.fillna(0.0).to_numpy(dtype=float)
    n_bars, n_assets = r.shape
    lam = math.exp(-math.log(2.0) / max(halflife, 1))
    cov = np.zeros((n_assets, n_assets))
    out = np.full(n_bars, np.nan)
    for t in range(n_bars):
        row = r[t]
        cov = lam * cov + (1.0 - lam) * np.outer(row, row)
        if t + 1 >= max(halflife, 2):
            variance = float(w[t] @ cov @ w[t])
            out[t] = math.sqrt(max(variance, 0.0) * bars_per_year)
    return pd.Series(out, index=weights.index)


def build_weights(
    targets: pd.DataFrame, close: pd.DataFrame, bars_per_year: float, params: PortfolioParams
) -> pd.DataFrame:
    aligned = targets.reindex(index=close.index, columns=close.columns)
    asset_vol = (ewm_vol(close, halflife=params.vol_halflife) * math.sqrt(bars_per_year)).clip(
        lower=params.min_asset_vol
    )
    stage1 = (aligned.fillna(0.0) * (params.vol_target / asset_vol)).fillna(0.0)
    returns = close.pct_change()
    portfolio_vol = ewma_portfolio_vol(returns, stage1, params.covariance_halflife, bars_per_year)
    scalar = (params.vol_target / portfolio_vol.where(portfolio_vol > 1e-12)).clip(upper=params.max_scalar).fillna(0.0)
    stage2 = stage1.mul(scalar, axis=0).clip(-params.max_weight, params.max_weight)
    gross = stage2.abs().sum(axis=1)
    factor = (params.max_gross / gross.where(gross > params.max_gross)).fillna(1.0).clip(upper=1.0)
    weights = stage2.mul(factor, axis=0)
    weights = weights.where(aligned.notna().any(axis=1).cummax(), other=np.nan)
    if params.no_trade_band > 0 or params.no_trade_rel_band > 0:
        weights = apply_no_trade_band(weights, params.no_trade_band, params.no_trade_rel_band)
    return weights


def apply_no_trade_band(weights: pd.DataFrame, band: float, relative: float = 0.0) -> pd.DataFrame:
    """Keep the previous weight when |Δw| < max(band, relative * |w_prev|).

    Path dependent.  Exits to exactly zero, entries from zero and sign flips
    are always executed; only same-direction resizing is suppressed.
    """
    values = weights.to_numpy(dtype=float)
    out = np.empty_like(values)
    previous = np.zeros(values.shape[1])
    for t in range(values.shape[0]):
        row = values[t]
        if np.all(np.isnan(row)):
            out[t] = np.nan
            continue
        current = np.where(np.isnan(row), 0.0, row)
        threshold = np.maximum(band, relative * np.abs(previous))
        same_direction = (np.sign(current) == np.sign(previous)) & (current != 0.0) & (previous != 0.0)
        small = (np.abs(current - previous) < threshold) & same_direction
        current = np.where(small, previous, current)
        out[t] = current
        previous = current
    return pd.DataFrame(out, index=weights.index, columns=weights.columns)
