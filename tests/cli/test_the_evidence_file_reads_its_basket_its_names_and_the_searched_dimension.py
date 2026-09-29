"""N2-N5 of the 09-29 checklist: what the validation report gained, and that the verdict reads none of it.

N2 prints the out-of-sample book against its basket under D-045's rules, N3 how much of it is one or three
names, N5 growth figures named for their caliber, N4 a neighbourhood that moves the dimension the run actually
searched.  All four are reported and never enforced.  The fresh-context review of 2026-09-29 (M4) found the
first version of this file tested the functions and not the wiring; the stitching test below closes that.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import beidou_alpha.validation.basket as research_basket
import beidou_live.benchmark as live_benchmark
from beidou_alpha.validation.basket import against_basket, nw_ols, signal_state
from beidou_alpha.validation.concentration import concentration
from beidou_alpha.validation.metrics import cagr, calmar, max_drawdown, payoff_ratio
from beidou_alpha.validation.verdict import decide
from beidou_alpha.validation.walk_forward import param_key, stitched_oos, walk_forward_evaluate, walk_forward_folds
from beidou_cli.research_grids import DEFAULT_GRIDS
from beidou_cli.research_report import _concentration_rows
from beidou_cli.research_validate_cmd import _neighbourhood_keys

BPY = 8760.0
EVIDENCE = Path("reports/research/tsmom-validation-20260925T143836Z.json")
NEW_KEYS = ("against_basket", "concentration", "cagr_full_sample", "calmar_full_sample", "payoff_ratio_bar", "oos_cagr")


def _hours(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")


def _bits(values: pd.Series) -> np.ndarray:
    return values.to_numpy(dtype=np.float64).view(np.uint64)


def test_the_research_estimator_is_the_live_one_including_where_it_clamps() -> None:
    """`nw_ols` restates `beidou_live.benchmark.nw_covariance` because alpha may not import live."""
    assert research_basket.NW_LAGS == live_benchmark.NW_LAGS
    assert research_basket.MAX_LAG_SHARE == live_benchmark.MAX_LAG_SHARE
    rng = np.random.default_rng(3)
    for n in (800, 150):  # 150 bars: the bandwidth clamps to a quarter of the sample, 37
        x = rng.normal(0.0, 0.01, n)
        y = 0.0002 + 0.7 * x + rng.normal(0.0, 0.01, n)
        fit = nw_ols(y, x)
        design = np.column_stack([np.ones(n), x])
        coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
        found = live_benchmark.nw_covariance(design, y - design @ coefficients, live_benchmark.NW_LAGS)
        assert fit is not None and found is not None
        covariance, used = found
        se = np.sqrt(np.diag(covariance))
        assert fit["nw_lags"] == used
        assert fit["nw_covers_intended_horizon"] is (used >= 48)
        assert np.isclose(fit["beta_t"], coefficients[1] / se[1], rtol=1e-12, atol=0.0)
        assert np.isclose(fit["alpha_t"], coefficients[0] / se[0], rtol=1e-12, atol=0.0)
    assert nw_ols(np.zeros(40), np.arange(40.0)) is None, "under 48 bars there is no fit, as live refuses"


def test_a_book_that_is_its_exposure_times_the_basket_reads_conditional_beta_one() -> None:
    rng = np.random.default_rng(5)
    index = _hours(600)
    basket = pd.Series(rng.normal(0.0, 0.01, 600), index=index)
    exposure = pd.Series(np.where(np.arange(600) % 200 < 100, 2.0, 0.5), index=index)
    weights = pd.DataFrame({"A": exposure / 2, "B": exposure / 2}, index=index)
    block = against_basket(exposure * basket, weights, basket, None, BPY, basket_name="pit members at each bar")
    assert block["enforced"] is False and block["basket"] == "pit members at each bar"
    assert math.isclose(block["conditional"]["beta"], 1.0, rel_tol=1e-9)
    assert abs(block["conditional"]["alpha_bps_per_bar"]) < 1e-9
    assert not math.isclose(block["constant"]["beta"], 1.0, rel_tol=1e-3), "the constant fit books exposure as beta"
    assert block["btc_buy_and_hold"] is None
    assert block["signal_state"]["all_long_share"] == 1.0
    assert set(block["basis"]) == {"returns", "basket", "conditional", "signal_state"}


def test_the_signal_state_counts_each_bar_by_the_sign_pattern_it_held() -> None:
    state = signal_state(pd.DataFrame({"A": [0.1, -0.1, 0.1, 0.0], "B": [0.1, -0.2, -0.1, 0.0]}))
    assert state["all_long_share"] == state["all_short_share"] == 0.25
    assert state["two_sided_share"] == state["flat_share"] == 0.25


def test_the_out_of_sample_frames_are_the_folds_own_choices_bit_for_bit() -> None:
    """Folds that choose DIFFERENT configurations, so a wiring that used one key throughout would differ."""
    n, index = 1200, _hours(1200)
    rng = np.random.default_rng(17)
    early = np.where(np.arange(n) < 400, 0.004, -0.002)
    frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    params = {"a": {"x": 1}, "b": {"x": 2}}
    late = np.where(np.arange(n) < 400, -0.001, 0.006)  # a wins the first two training windows, b the last two
    for name, drift in (("a", early), ("b", late)):
        net = pd.DataFrame({s: drift / 2 + rng.normal(0.0, 0.001, n) for s in ("S1", "S2")}, index=index)
        weights = pd.DataFrame({"S1": 0.5, "S2": -0.25}, index=index)
        frames[param_key(params[name])] = (weights, net)
    nets = {key: frame[1].sum(axis=1) for key, frame in frames.items()}
    by_key = {param_key(p): p for p in params.values()}
    wf = walk_forward_evaluate(nets, by_key, walk_forward_folds(n, 4, min_train=400, purge=0), BPY)
    assert len({param_key(o.chosen_params) for o in wf.folds}) == 2, "the fixture must make the folds disagree"
    weights, net = stitched_oos(wf, frames, index)
    assert net.index.equals(wf.oos_returns.index) and weights.index.equals(wf.oos_returns.index)
    assert np.array_equal(_bits(net.sum(axis=1)), _bits(wf.oos_returns))
    assert (
        concentration(net, net, BPY)["oos"]["sharpe"]
        == against_basket(wf.oos_returns, weights, wf.oos_returns * 0 + 0.001, None, BPY, basket_name="x")["book"][
            "sharpe"
        ]
    )


def test_concentration_reads_full_and_out_of_sample_from_their_own_frames() -> None:
    rng = np.random.default_rng(11)
    index = _hours(2000)
    full = pd.DataFrame({"A": rng.normal(0.0004, 0.002, 2000), "B": rng.normal(0.0, 0.002, 2000)}, index=index)
    oos = pd.DataFrame({"A": rng.normal(0.0, 0.002, 1000), "B": rng.normal(0.0004, 0.002, 1000)}, index=index[:1000])
    block = concentration(full, oos, BPY)
    assert block["full_sample"]["top"][0][0] == "A"
    assert block["oos"]["top"][0][0] == "B"
    assert block["oos"]["leave_one_out_min_without"] == "B"
    assert block["oos"]["leave_one_out_min_sharpe"] < block["oos"]["sharpe"]
    assert set(block["basis"]) == {"units", "leave_one_out"}


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
    assert cagr(pd.Series([0.5, 0.1]), BPY) is None, "a growth a float cannot hold is None, not an OverflowError"
    assert payoff_ratio(pd.Series([0.01, 0.02])) is None, "a series with no losing bar has no payoff ratio"


def test_the_neighbourhood_moves_the_dimension_the_run_searched() -> None:
    combos = [{"crowding_window": 0, "vol_window": 400}, {"crowding_window": 72, "vol_window": 400}]
    keys = _neighbourhood_keys("tsmom", combos)
    assert "crowding_window" in keys
    assert set(DEFAULT_GRIDS["tsmom"]) <= set(keys)
    assert _neighbourhood_keys("tsmom", combos[:1]) == tuple(sorted(DEFAULT_GRIDS["tsmom"]))


def test_the_verdict_reads_none_of_them() -> None:
    """Whole blocks replaced with hostile values, and the source of `decide` names none of the new keys."""
    report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    before = decide(report)
    hostile = json.loads(json.dumps(report))
    hostile["against_basket"] = dict.fromkeys(("constant", "conditional", "book", "signal_state", "bars"), -99.0)
    hostile["concentration"] = {"full_sample": -99.0, "oos": -99.0}
    hostile["full_sample"].update(cagr_full_sample=-0.99, calmar_full_sample=-99.0, payoff_ratio_bar=0.0)
    hostile["walk_forward"].update(oos_cagr=-0.99, oos_calmar=-99.0)
    assert decide(hostile) == before
    source = Path("beidou_alpha/validation/verdict.py").read_text(encoding="utf-8")
    assert not [key for key in NEW_KEYS if key in source], "decide must not read the reported-only readings"


# --- 2026-09-29 review: the edges the validate path does not reach today -----------------------------


def test_a_hole_in_the_design_is_the_none_nw_ols_promised_not_an_svd_error() -> None:
    x = np.linspace(-0.01, 0.01, 200)
    x[10] = np.nan
    assert nw_ols(0.5 * np.nan_to_num(x), x) is None
    assert nw_ols(np.where(np.arange(200) == 5, np.nan, 0.5 * np.nan_to_num(x)), np.nan_to_num(x)) is None


def test_a_bar_with_no_reading_adds_no_time_to_growth_and_no_depth_to_the_drawdown() -> None:
    with_hole = np.array([0.001, np.nan, np.nan, 0.001, -0.002])
    without = np.array([0.001, 0.001, -0.002])
    assert cagr(with_hole, 4.0) == cagr(without, 4.0)
    assert max_drawdown(with_hole) == max_drawdown(without)
    assert calmar(with_hole, 4.0) == calmar(without, 4.0)


def test_a_book_with_no_name_to_leave_out_prints_n_a_rather_than_none() -> None:
    frame = pd.DataFrame({"BTCUSDT": np.full(200, 0.001)}, index=_hours(200))
    row = _concentration_rows(concentration(frame, frame, BPY))["OOS Sharpe without its most important name"]
    assert row.startswith("n/a: no name to leave out") and "None" not in row, row
