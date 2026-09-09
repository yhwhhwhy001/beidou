"""DL-C1 / KILL-A: the backtest stops pretending a 40 USDT order and a 4M one cost the same.

Everything else in `backtest.py` is scale-free, and that is arithmetic rather than a finding: gross
P&L, turnover cost and funding are all linear in the weights, so scaling every weight by k leaves the
Sharpe unchanged to the last digit.  The flat 7 bps is what makes that true, it is a good
approximation at demo notional (40-800 USDT), and it is the assumption KILL-Q12 has held real capital
out of scope on since 2026-09-05.

What is asserted here is the SHAPE, never a cost number.  The coefficient is an assumption this system
cannot calibrate - its only fills are demo, order/ADV around 1e-8, where the model predicts under a
hundredth of a basis point - so a test that pinned a bps figure would be pinning a literature constant
and calling it a measurement.  The shape is what has to hold: off by default, the flat model as the
exact limit, monotone in size, superlinear in size, and causal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.backtest import CostModel, ImpactModel, impact_costs, run_backtest
from beidou_alpha.panel import Panel


def _panel(bars: int = 900, symbols: tuple[str, ...] = ("AAA", "BBB"), volume: float = 1_000.0) -> Panel:
    index = pd.date_range("2024-01-01", periods=bars, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, size=(bars, len(symbols))), axis=0)),
        index=index,
        columns=list(symbols),
    )
    volumes = pd.DataFrame(volume, index=index, columns=list(symbols))
    return Panel(
        interval="1h",
        open=close.shift(1).bfill(),
        high=close * 1.001,
        low=close * 0.999,
        close=close,
        volume=volumes,
        quote_volume=volumes * close,
    )


def _weights(panel: Panel) -> pd.DataFrame:
    """A book that trades every bar, so turnover is never zero and the charge is always exercised."""
    sign = pd.Series(np.where(np.arange(len(panel.close)) % 2 == 0, 0.4, -0.4), index=panel.close.index)
    return pd.DataFrame(dict.fromkeys(panel.close.columns, sign))


def test_it_is_off_by_default_so_every_archived_report_still_reproduces() -> None:
    panel = _panel()
    weights = _weights(panel)
    flat = run_backtest(panel, weights, CostModel(turnover_bps=7.0))
    explicit_zero = run_backtest(panel, weights, CostModel(turnover_bps=7.0), impact=ImpactModel(capital=0.0))
    assert flat.costs.equals(explicit_zero.costs)


def test_the_flat_model_is_this_models_own_limit_rather_than_a_separate_branch() -> None:
    """T-C1-1.  The charge is continuous in size and vanishes at zero - no branch, no discontinuity.

    Stated as the exact square-root relation rather than as "small at small capital", because "small"
    depends on the panel's ADV and would make this test a statement about the fixture.  What matters is
    that the extra charge over the flat model scales as sqrt(capital) all the way down, so capital = 0
    is the limit of the same expression rather than a separate code path.
    """
    panel = _panel()
    weights = _weights(panel)
    base = run_backtest(panel, weights, CostModel(turnover_bps=7.0)).costs.to_numpy().sum()
    charged = [
        run_backtest(panel, weights, CostModel(turnover_bps=7.0), impact=ImpactModel(capital=c)).costs.to_numpy().sum()
        for c in (1.0, 100.0, 10_000.0)
    ]
    assert all(value > base for value in charged)
    assert np.isclose(charged[1] - base, (charged[0] - base) * 10.0, rtol=1e-9)
    assert np.isclose(charged[2] - base, (charged[0] - base) * 100.0, rtol=1e-9)


@pytest.mark.parametrize("factor", [4.0, 9.0, 100.0])
def test_the_charge_grows_with_the_square_root_of_size(factor: float) -> None:
    """Doubling capital does not double impact; quadrupling it does.  That is the whole point of size."""
    panel = _panel()
    rets = panel.close / panel.open - 1.0
    turnover = _weights(panel).diff().abs().fillna(0.4)
    columns = list(panel.close.columns)
    small = impact_costs(turnover, rets, panel, columns, ImpactModel(capital=1_000.0)).to_numpy().sum()
    large = impact_costs(turnover, rets, panel, columns, ImpactModel(capital=1_000.0 * factor)).to_numpy().sum()
    assert np.isclose(large / small, np.sqrt(factor), rtol=1e-9)


def test_it_reads_no_bar_the_decision_could_not_have() -> None:
    """Both inputs are trailing and shifted; replacing the future must not move a single charge."""
    panel = _panel()
    weights = _weights(panel)
    columns = list(panel.close.columns)
    rets = panel.close / panel.open - 1.0
    turnover = weights.diff().abs().fillna(0.4)
    model = ImpactModel(capital=1_000_000.0)
    before = impact_costs(turnover, rets, panel, columns, model)

    cutoff = 700
    tampered = _panel()
    tampered.close.iloc[cutoff:] *= 3.0
    tampered.volume.iloc[cutoff:] *= 50.0
    if tampered.quote_volume is not None:
        tampered.quote_volume.iloc[cutoff:] *= 150.0
    after = impact_costs(turnover, rets, tampered, columns, model)
    pd.testing.assert_frame_equal(before.iloc[:cutoff], after.iloc[:cutoff])


def test_a_thinner_market_costs_more_for_the_same_order() -> None:
    """The quantity being modelled is participation, so ADV has to be in the denominator."""
    thick, thin = _panel(), _panel(volume=10.0)
    columns = list(thick.close.columns)
    rets = thick.close / thick.open - 1.0
    turnover = _weights(thick).diff().abs().fillna(0.4)
    model = ImpactModel(capital=1_000_000.0)
    assert (
        impact_costs(turnover, rets, thin, columns, model).to_numpy().sum()
        > impact_costs(turnover, rets, thick, columns, model).to_numpy().sum()
    )


def test_negative_parameters_are_refused() -> None:
    with pytest.raises(ValueError):
        ImpactModel(capital=-1.0)
    with pytest.raises(ValueError):
        ImpactModel(coefficient=-1.0)


def test_the_stress_arms_carry_the_charge_the_headline_carries() -> None:
    """`cost_stress` is a GATE (`verdict.decide` reads x2), so it has to be priced as the run is labelled.

    Found 2026-09-09 on the first AC-C1 artefact: its header said `impact_model: {capital: 100000}`, its
    walk-forward was priced under the square-root law, and both stress blocks were priced flat - within
    0.002 Sharpe of the flat report produced the day before.  The error ran in the permissive direction.

    The property is what the fix relies on: the impact charge composes with a SCALED `CostModel` rather
    than being scaled by it.  A multiplier on `turnover_bps` is a question about fees, and impact is not
    a fee.
    """
    panel = _panel()
    weights = _weights(panel)
    impact = ImpactModel(capital=5_000_000.0)
    for multiplier in (1.0, 1.5, 2.0):
        cost = CostModel(turnover_bps=7.0 * multiplier)
        flat = run_backtest(panel, weights, cost).costs.to_numpy().sum()
        charged = run_backtest(panel, weights, cost, impact=impact).costs.to_numpy().sum()
        assert charged > flat, f"x{multiplier}: the stress arm ignored the impact model"
    # and the charge itself does not move with the fee multiplier - it is size, not price
    charges = [
        run_backtest(panel, weights, CostModel(turnover_bps=7.0 * m), impact=impact).costs.to_numpy().sum()
        - run_backtest(panel, weights, CostModel(turnover_bps=7.0 * m)).costs.to_numpy().sum()
        for m in (1.0, 1.5, 2.0)
    ]
    assert max(charges) - min(charges) < 1e-9, f"the impact charge scaled with the fee multiplier: {charges}"


def test_a_priced_run_is_a_different_trial_from_a_flat_one() -> None:
    """The ledger folded DL-C1's impact-priced tsmom onto the flat rows as a replay, so `spent` never moved.

    Running one configuration under two cost models and keeping whichever passes is the selection DSR
    exists to expose, and §19 had already written down that adopting the cost model would charge rows.
    Conditional on `enabled`, because an unconditional field would give every future FLAT run a
    signature no archived row shares - charging genuine replays as new trials, the same defect mirrored.
    """
    from beidou_cli.research_cmd import _construction_digest

    portfolio = {"vol_target": 0.30, "no_trade_band": 0.002}
    cost = CostModel(turnover_bps=7.0)
    flat = _construction_digest(portfolio, cost, "open_to_close")
    off = _construction_digest(portfolio, cost, "open_to_close", ImpactModel(capital=0.0))
    priced = _construction_digest(portfolio, cost, "open_to_close", ImpactModel(capital=100_000.0))
    bigger = _construction_digest(portfolio, cost, "open_to_close", ImpactModel(capital=1_000_000.0))

    assert off == flat, "an archived flat row must keep its signature, or every replay is charged as new"
    assert priced != flat, "an impact-priced run is a different trial"
    assert bigger != priced, "and so is the same law at a different size"
