"""A two-leg book priced as one synthetic instrument per symbol: long spot, short the perpetual.

`run_backtest` prices one weight per symbol against one return series and one funding series.  A hedge
is two weights that always move together, so it is priced as ONE weight on the spread between them:

    return    r_spread = r_spot - r_perp      (both legs on the run's own return convention)
    funding   f_spread = -f_perp              (the short leg receives what longs pay)
    cost      turnover_bps once per unit of |dw|, which is both legs' fees and slippage

Every reading `validate` takes downstream - walk-forward, CPCV, cost and slippage stress, the book
guards' replay - then prices the hedge without a second code path.  The synthetic open and close are
built so `asset_returns` recovers exactly those returns under BOTH conventions, which
`tests/alpha/test_the_hedged_book_prices_two_legs_as_one.py` holds.

A leg can stop trading while the hedge is on: a spot bar goes missing (a halted listing, an archive
hole), a perpetual bar goes missing or trades nothing.  A delisted perpetual does the second for months,
because the archive keeps printing its last price at zero volume (FTTUSDT at 1.59 from 2022-11-14 04:00).
The leg that did not trade is marked at its last traded price, so for those bars the book carries the
other leg naked, and the move the leg missed lands on the bar it trades again.  Scoring such a bar as
zero would be the optimistic reading: LUNAUSDT's perpetual last traded at 2022-05-12 15:00, and its spot
fell from 0.00887 to 0.00005 over the next nine hours.

The mark lasts one day.  The signal decides daily and refuses a symbol whose legs did not both trade on
the decision bar, so a day is the longest the book can hold a leg that has stopped; past it the spread is
not priced, and a leg that comes back later moves neither the book nor the synthetic price.  It may come
back as something else: LUNAUSDT's spot resumes on 2022-05-31 at 8.5558, and that is LUNA 2.0.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.panel import Panel, interval_seconds

#: Per-leg notional as a fraction of equity.  The spot leg needs its whole notional in cash, and the
#: short perpetual at 2x needs half its notional as margin: 2/3 + 1/3 fills the account, and the
#: perpetual leg's margin is exhausted only by a +50% move the spot leg has not yet been moved across to
#: cover.  Sharpe does not depend on it - gross, costs and funding are all linear in the weights - but the
#: drawdown and the stress windows' worst day do, so it is fixed before the run, not chosen by it.
HEDGED_NOTIONAL = 2.0 / 3.0


def spread_panel(panel: Panel) -> Panel:
    """The long-spot / short-perpetual spread of every symbol, as a panel `run_backtest` can price."""
    spot_open, spot_close = panel.spot_field("open"), panel.spot_field("close")
    if spot_open is None or spot_close is None:
        raise ValueError("a hedged book needs the spot leg's open and close in `panel.spot`")
    spot_open = spot_open.reindex(index=panel.index, columns=panel.close.columns)
    spot_close = spot_close.reindex(index=panel.index, columns=panel.close.columns)
    day = max(1, round(86_400 / interval_seconds(panel.interval)))
    traded = panel.close.notna() & (panel.volume > 0)
    perp, spot = panel.close.where(traded).ffill(limit=day), spot_close.ffill(limit=day)
    spread_cc = spot / spot.shift(1) - 1.0 - (perp / perp.shift(1) - 1.0)
    spread_oc = (spot_close / spot_open - 1.0).fillna(0.0).where(spot.notna()) - (
        (panel.close / panel.open - 1.0).where(traded).fillna(0.0).where(perp.notna())
    )
    for name, frame in (("close-to-close", spread_cc), ("open-to-close", spread_oc)):
        broken = np.argwhere(frame.to_numpy(dtype=float) <= -1.0)
        if len(broken):
            row, column = broken[0]
            raise ValueError(
                f"{frame.columns[column]} at {frame.index[row]}: {name} spread return "
                f"{frame.iat[row, column]:.4f} would price the synthetic leg at or below zero"
            )
    close = (1.0 + spread_cc.fillna(0.0)).cumprod().where(perp.notna() & spot.notna())
    opened = close / (1.0 + spread_oc)
    return Panel(
        interval=panel.interval,
        open=opened,
        high=opened.where(opened >= close, close),
        low=opened.where(opened <= close, close),
        close=close,
        volume=panel.volume,
        quote_volume=panel.quote_volume,
        funding=None if panel.funding is None else -panel.funding,
        reference=panel.reference,
    )


def hedged_weights(
    scores: pd.DataFrame,
    close: pd.DataFrame,
    membership: pd.DataFrame | None,
    *,
    min_history_bars: int,
    decision_hour_utc: int,
    notional: float = HEDGED_NOTIONAL,
) -> pd.DataFrame:
    """Equal notional across the symbols the signal holds, decided on the decision bar only.

    Membership and listing age are read on the decision bar and held to the next one, so a symbol that
    crosses ``min_history_bars`` mid-day does not re-weight the book an hour later.  Rows before the
    signal's first decision are NaN, so `run_backtest` starts where the signal does.
    """
    eligible = close.notna() & (close.notna().cumsum() >= min_history_bars)
    if membership is not None:
        eligible &= membership.reindex(index=close.index, columns=close.columns, fill_value=False).astype(bool)
    chosen = ((scores == 1.0) & eligible).astype(float)
    count = chosen.sum(axis=1)
    weights = chosen.div(count.where(count > 0), axis=0).fillna(0.0) * notional
    stamps = pd.DatetimeIndex(close.index)
    decided = (stamps.hour == decision_hour_utc) & (stamps.minute == 0) & scores.notna().any(axis=1).to_numpy()
    weights.loc[~decided, :] = np.nan
    return weights.ffill()


def hedged_construction(*, notional: float, min_history_bars: int, decision_hour_utc: int) -> dict[str, Any]:
    """What `validate` records as the report's `portfolio`, and folds into the ledger's construction digest."""
    return {
        "book": "hedged_spread",
        "legs": "long spot + short perpetual, equal notional",
        "sizing": "equal notional across held symbols, no vol targeting",
        "notional_per_leg": notional,
        "decision_hour_utc": decision_hour_utc,
        "min_history_bars": min_history_bars,
    }
