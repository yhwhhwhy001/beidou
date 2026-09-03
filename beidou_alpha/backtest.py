"""Vectorised, causal backtest.

Weights are decided at bar ``t`` from information through bar ``t`` and
executed at bar ``t+1``.  Two return conventions:

* ``open_to_close`` (default, conservative, matches the frozen August 2026
  baseline): PnL_{t+1} = w_t * (close_{t+1} / open_{t+1} - 1).  The gap from
  close_t to open_{t+1} is excluded symmetrically for strategy and benchmark.
* ``close_to_close``: PnL_{t+1} = w_t * (close_{t+1} / close_t - 1), i.e. fills
  at the decision close.

Costs charged on the execution bar: ``turnover_bps`` per unit of |Δw|,
``carry_bps_per_bar`` per unit of |w| (flat adverse carry, legacy diagnostic)
and, when ``use_funding``, the actual settled funding ``w * rate`` (longs pay
positive funding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from beidou_alpha.panel import Panel
from beidou_alpha.validation.metrics import summarize_returns

Execution = Literal["open_to_close", "close_to_close"]


@dataclass(frozen=True)
class CostModel:
    turnover_bps: float = 7.0
    carry_bps_per_bar: float = 0.0
    use_funding: bool = False

    def __post_init__(self) -> None:
        if self.turnover_bps < 0 or self.carry_bps_per_bar < 0:
            raise ValueError("cost parameters must be non-negative")


@dataclass
class BacktestResult:
    execution: Execution
    weights: pd.DataFrame
    asset_returns: pd.DataFrame
    gross: pd.DataFrame
    costs: pd.DataFrame
    net: pd.DataFrame
    turnover: pd.Series
    bars_per_year: float

    @property
    def portfolio_gross(self) -> pd.Series:
        return self.gross.sum(axis=1)

    @property
    def portfolio_net(self) -> pd.Series:
        return self.net.sum(axis=1)

    @property
    def equity_curve(self) -> pd.Series:
        return (1.0 + self.portfolio_net).cumprod()

    def summary(self) -> dict[str, Any]:
        return summarize_returns(self.portfolio_gross, self.portfolio_net, self.weights, self.bars_per_year)

    def per_symbol_summary(self) -> dict[str, dict[str, Any]]:
        return {
            str(symbol): summarize_returns(
                self.gross[symbol], self.net[symbol], self.weights[symbol], self.bars_per_year
            )
            for symbol in self.weights.columns
        }


def asset_returns(panel: Panel, execution: Execution) -> pd.DataFrame:
    if execution == "open_to_close":
        return panel.close / panel.open - 1.0
    if execution == "close_to_close":
        return panel.close / panel.close.shift(1) - 1.0
    raise ValueError(f"unknown execution convention {execution!r}")


def run_backtest(
    panel: Panel,
    weights: pd.DataFrame,
    cost: CostModel | None = None,
    execution: Execution = "open_to_close",
) -> BacktestResult:
    """``weights``: decision-time target weights (fraction of equity), index = decision bars."""
    cost = cost or CostModel()
    columns = [symbol for symbol in weights.columns if symbol in panel.close.columns]
    decided = weights[columns].reindex(panel.close.index)
    first_valid = decided.dropna(how="all").index.min() if decided.notna().any().any() else None
    if first_valid is None:
        raise ValueError("weights contain no valid decisions")
    executed = decided.shift(1).loc[first_valid:].iloc[1:].fillna(0.0)
    if executed.empty:
        raise ValueError("no execution bars after the first decision")
    rets = asset_returns(panel, execution)[columns].reindex(executed.index).fillna(0.0)
    gross = executed * rets
    delta = executed.diff()
    delta.iloc[0] = executed.iloc[0]
    turnover = delta.abs()
    costs = turnover * (cost.turnover_bps / 10_000.0) + executed.abs() * (cost.carry_bps_per_bar / 10_000.0)
    if cost.use_funding and panel.funding is not None:
        funding = panel.funding[columns].reindex(executed.index).fillna(0.0)
        costs = costs + executed * funding
    net = gross - costs
    return BacktestResult(
        execution=execution,
        weights=executed,
        asset_returns=rets,
        gross=gross,
        costs=costs,
        net=net,
        turnover=turnover.sum(axis=1),
        bars_per_year=panel.bars_per_year,
    )


def benchmark_returns(
    panel: Panel, execution: Execution = "open_to_close", symbols: list[str] | None = None
) -> pd.Series:
    """Equal-weight, always-long, zero-cost comparator (an opportunity-cost benchmark, not investable)."""
    chosen = symbols or panel.symbols
    return asset_returns(panel, execution)[chosen].mean(axis=1)
