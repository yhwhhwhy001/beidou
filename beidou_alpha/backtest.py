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

import numpy as np
import pandas as pd

from beidou_alpha.overlays.exposure import BookGuardParams, clamp_book, hold_or_reduce
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
    guard_events: pd.DataFrame | None = None

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
        out = summarize_returns(self.portfolio_gross, self.portfolio_net, self.weights, self.bars_per_year)
        if self.guard_events is not None:
            out["guards"] = {
                "replayed": True,
                "gross_capped_bars": int(self.guard_events["gross_capped"].sum()),
                "daily_loss_pause_bars": int(self.guard_events["daily_loss_pause"].sum()),
                "bars": len(self.guard_events),
            }
        return out

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
    guards: BookGuardParams | None = None,
) -> BacktestResult:
    """``weights``: decision-time target weights (fraction of equity), index = decision bars.

    ``guards`` replays the two book-level guards the live loop applies after the model and before
    the rebalancer (D-004).  Leave it None for the historical path; pass it to score the book the
    loop would actually hold.  The replay is causal and path dependent - the daily-loss pause reads
    the equity this same replay produced - so it cannot be vectorised, and it is a no-op whenever
    neither guard binds (the shipped book at ``vol_target 0.15``: zero pauses in 5.6 years).
    """
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
    funding = (
        panel.funding[columns].reindex(executed.index).fillna(0.0)
        if cost.use_funding and panel.funding is not None
        else None
    )
    guard_events: pd.DataFrame | None = None
    if guards is not None:
        decision_times = pd.DatetimeIndex(panel.close.index[panel.close.index.get_indexer(executed.index) - 1])
        executed, guard_events = _replay_book_guards(executed, decision_times, rets, funding, cost, guards)
    gross = executed * rets
    delta = executed.diff()
    delta.iloc[0] = executed.iloc[0]
    turnover = delta.abs()
    costs = turnover * (cost.turnover_bps / 10_000.0) + executed.abs() * (cost.carry_bps_per_bar / 10_000.0)
    if funding is not None:
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
        guard_events=guard_events,
    )


def _replay_book_guards(
    executed: pd.DataFrame,
    decision_times: pd.DatetimeIndex,
    rets: pd.DataFrame,
    funding: pd.DataFrame | None,
    cost: CostModel,
    params: BookGuardParams,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bar-by-bar replay of the caps and the daily-loss pause on the equity path they themselves produce.

    ``decision_times`` are the bars the weights were decided on, not the bars they execute on: the live
    loop rolls ``day_start_equity`` on the decision bar, and at an hourly interval the two straddle UTC
    midnight once a day.  Equity entering iteration ``t`` is the equity through the decision bar, which
    is what ``Snapshot.equity`` reports to ``evaluate_guards``.  The one approximation against live: the
    held row here is the previous target rather than the venue position drifted by price, which is the
    continuous-rebalancing assumption the rest of this module already makes.
    """
    values = executed.to_numpy(dtype=float)
    returns = rets.to_numpy(dtype=float)
    fees = None if funding is None else funding.to_numpy(dtype=float)
    turnover_rate = cost.turnover_bps / 10_000.0
    carry_rate = cost.carry_bps_per_bar / 10_000.0
    out = np.empty_like(values)
    capped = np.zeros(len(values), dtype=bool)
    paused = np.zeros(len(values), dtype=bool)
    days = pd.DatetimeIndex(decision_times).normalize().to_numpy()
    held = np.zeros(values.shape[1])
    equity = 1.0
    day_start = 1.0
    day: object = None
    for t in range(len(values)):
        if days[t] != day:
            day = days[t]
            day_start = equity
        row, gross_capped = clamp_book(values[t], params.max_weight, params.max_gross)
        capped[t] = gross_capped
        if day_start > 0 and equity > 0 and equity / day_start - 1.0 < params.daily_loss_pause:
            row = hold_or_reduce(row, held)
            paused[t] = True
        out[t] = row
        realised = float(row @ returns[t])
        charge = float(np.abs(row - held).sum()) * turnover_rate + float(np.abs(row).sum()) * carry_rate
        if fees is not None:
            charge += float(row @ fees[t])
        equity *= 1.0 + (realised - charge)
        held = row
    events = pd.DataFrame({"gross_capped": capped, "daily_loss_pause": paused}, index=executed.index)
    return pd.DataFrame(out, index=executed.index, columns=executed.columns), events


def benchmark_returns(
    panel: Panel, execution: Execution = "open_to_close", symbols: list[str] | None = None
) -> pd.Series:
    """Equal-weight, always-long, zero-cost comparator (an opportunity-cost benchmark, not investable)."""
    chosen = symbols or panel.symbols
    return asset_returns(panel, execution)[chosen].mean(axis=1)
