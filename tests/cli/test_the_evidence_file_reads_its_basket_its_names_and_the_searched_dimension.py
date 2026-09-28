"""N2-N5 of the 09-29 checklist: what the validation report gained, and that the verdict reads none of it.

N2 prints the out-of-sample book against its point-in-time basket under D-045's rules, N3 how much of it is
one or three names, N5 growth figures named for their caliber, N4 a neighbourhood that moves the dimension
the run actually searched.  All four are reported and never enforced: `decide` must return the same verdict
whatever they say, which the last test holds against the evidence tsmom ships.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from beidou_alpha.validation.basket import against_basket, nw_ols, signal_state
from beidou_alpha.validation.concentration import concentration
from beidou_alpha.validation.metrics import cagr, calmar, max_drawdown, payoff_ratio
from beidou_alpha.validation.verdict import decide
from beidou_cli.research_grids import DEFAULT_GRIDS
from beidou_cli.research_validate_cmd import _neighbourhood_keys
from beidou_live.benchmark import nw_covariance

BPY = 8760.0
EVIDENCE = Path("reports/research/tsmom-validation-20260925T143836Z.json")


def _hours(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")


def test_the_research_estimator_is_the_live_one() -> None:
    """`nw_ols` restates `beidou_live.benchmark.nw_covariance` because alpha may not import live."""
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 0.01, 800)
    y = 0.0002 + 0.7 * x + rng.normal(0.0, 0.01, 800)
    fit = nw_ols(y, x)
    design = np.column_stack([np.ones(800), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    found = nw_covariance(design, y - design @ coefficients, 48)
    assert fit is not None and found is not None
    covariance, used = found
    se = np.sqrt(np.diag(covariance))
    assert fit["nw_lags"] == used == 48
    assert np.isclose(fit["beta_t"], coefficients[1] / se[1], rtol=1e-12, atol=0.0)
    assert np.isclose(fit["alpha_t"], coefficients[0] / se[0], rtol=1e-12, atol=0.0)


def test_a_book_that_is_its_exposure_times_the_basket_reads_conditional_beta_one() -> None:
    rng = np.random.default_rng(5)
    index = _hours(600)
    basket = pd.Series(rng.normal(0.0, 0.01, 600), index=index)
    exposure = pd.Series(np.where(np.arange(600) % 200 < 100, 2.0, 0.5), index=index)
    weights = pd.DataFrame({"A": exposure / 2, "B": exposure / 2}, index=index)
    block = against_basket(exposure * basket, weights, basket, None, BPY)
    assert block["enforced"] is False
    assert math.isclose(block["conditional"]["beta"], 1.0, rel_tol=1e-9)
    assert abs(block["conditional"]["alpha_bps_per_bar"]) < 1e-9
    assert not math.isclose(block["constant"]["beta"], 1.0, rel_tol=1e-3), "the constant fit books exposure as beta"
    assert block["btc_buy_and_hold"] is None
    assert block["signal_state"]["all_long_share"] == 1.0


def test_the_signal_state_counts_each_bar_by_the_sign_pattern_it_held() -> None:
    state = signal_state(pd.DataFrame({"A": [0.1, -0.1, 0.1, 0.0], "B": [0.1, -0.2, -0.1, 0.0]}))
    assert state["all_long_share"] == state["all_short_share"] == 0.25
    assert state["two_sided_share"] == state["flat_share"] == 0.25


def test_concentration_names_the_symbol_the_book_leans_on() -> None:
    rng = np.random.default_rng(11)
    index = _hours(2000)
    oos = pd.DataFrame(
        {
            "A": rng.normal(0.0004, 0.002, 2000),
            "B": rng.normal(0.0, 0.002, 2000),
            "C": rng.normal(0.0, 0.002, 2000),
        },
        index=index,
    )
    block = concentration(oos, oos, BPY)
    assert block["enforced"] is False
    assert block["oos"]["top"][0][0] == "A"
    assert block["oos"]["leave_one_out_min_without"] == "A"
    assert block["oos"]["leave_one_out_min_sharpe"] < block["oos"]["sharpe"]


def test_shares_are_withheld_when_the_names_summed_to_a_loss() -> None:
    oos = pd.DataFrame({"A": [-0.01, -0.01], "B": [0.001, 0.0]}, index=_hours(2))
    block = concentration(oos, oos, BPY)
    assert block["oos"]["top1_share"] is None and block["full_sample"]["top3_share"] is None


def test_growth_figures_and_the_bar_level_payoff() -> None:
    flat_up = pd.Series([0.0001] * 8760)
    assert math.isclose(cagr(flat_up, BPY), 1.0001**8760 - 1.0, rel_tol=1e-9)
    assert calmar(flat_up, BPY) is None, "no drawdown, no Calmar"
    choppy = pd.Series([0.02, -0.01, 0.02, -0.01])
    assert payoff_ratio(choppy) == 2.0
    assert math.isclose(calmar(choppy, 4.0), cagr(choppy, 4.0) / abs(max_drawdown(choppy)), rel_tol=1e-12)
    assert cagr(pd.Series([], dtype=float), BPY) is None
    assert payoff_ratio(pd.Series([0.01, 0.02])) is None, "a series with no losing bar has no payoff ratio"


def test_the_neighbourhood_moves_the_dimension_the_run_searched() -> None:
    combos = [{"crowding_window": 0, "vol_window": 400}, {"crowding_window": 72, "vol_window": 400}]
    keys = _neighbourhood_keys("tsmom", combos)
    assert "crowding_window" in keys
    assert set(DEFAULT_GRIDS["tsmom"]) <= set(keys)
    assert _neighbourhood_keys("tsmom", combos[:1]) == tuple(sorted(DEFAULT_GRIDS["tsmom"]))


def test_the_verdict_reads_none_of_them() -> None:
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    before = decide(report)
    hostile = json.loads(json.dumps(report))
    hostile["against_basket"] = {"constant": {"beta": 99.0, "alpha_t": -99.0}, "signal_state": {}}
    hostile["concentration"] = {"oos": {"top1_share": 99.0, "leave_one_out_min_sharpe": -99.0}}
    hostile["full_sample"].update(cagr_full_sample=-0.99, calmar_full_sample=-99.0, payoff_ratio=0.0)
    hostile["walk_forward"].update(oos_cagr=-0.99, oos_calmar=-99.0)
    assert decide(hostile) == before
