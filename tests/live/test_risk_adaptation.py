"""M-015: the report can tell "sizing adapts per symbol" from "every symbol is treated alike".

The operator asked three times why every order carries the same 5x, because nothing in the report
answered it.  D-037 already established that the exchange leverage is inert here and that adaptation
lives in the weight; these tests are the instrument that says so from the live record, and - the part
that matters - that would stop saying so if stage 1 were ever removed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from beidou_alpha.portfolio import PortfolioParams, asset_vol, build_weights
from beidou_live.reports import RISK_COMPRESSION_LIMIT, risk_adaptation
from beidou_live.state import LiveState, StateStore

HOUR = 3_600_000
BASE = 1_788_000_000_000
DAY = datetime.fromtimestamp(BASE / 1000, tz=UTC).strftime("%Y-%m-%d")

# Roughly the live book on 2026-09-05: a 12x spread in annualised volatility across 15 names.
VOLS = {
    "BNBUSDT": 0.79,
    "BTCUSDT": 0.81,
    "ETHUSDT": 0.88,
    "SOLUSDT": 0.96,
    "SUIUSDT": 1.16,
    "XRPUSDT": 1.25,
    "DOGEUSDT": 1.25,
    "HYPEUSDT": 1.27,
    "ADAUSDT": 1.50,
    "1000PEPEUSDT": 1.66,
    "TRUMPUSDT": 2.22,
    "ENAUSDT": 2.45,
    "ZECUSDT": 2.50,
    "CYSUSDT": 6.15,
    "AKEUSDT": 9.63,
}


def _store(tmp_path: Path, targets: dict[str, float], vols: dict[str, float] | None) -> StateStore:
    store = StateStore(tmp_path / "live")
    record: dict[str, object] = {"as_of_ms": BASE, "equity": 10_692.0, "targets": targets}
    if vols is not None:
        record["asset_vol"] = vols
    store.append_cycle(record)
    store.save(LiveState(leverage_set=dict.fromkeys(targets, 5)))
    return store


def test_inverse_vol_sizing_compresses_a_12x_market_spread_into_a_small_risk_spread(tmp_path: Path) -> None:
    """The live shape: uniform 5x on the venue, and risk contributions that are nearly flat anyway."""
    targets = {symbol: 0.30 / vol for symbol, vol in VOLS.items()}  # stage 1, alone
    block = risk_adaptation(_store(tmp_path, targets, VOLS), DAY)
    assert block["enforced"] and block["status"] == "OK"
    assert block["vol_spread"] > 12.0, "the markets really are that different"
    assert block["risk_spread"] < 1.01, "and stage 1 takes essentially all of it back out"
    assert block["leverage_distinct"] == 1, "the exchange leverage is uniform, and that is not the alert"


def test_deleting_stage_one_makes_compression_read_one_and_alerts(tmp_path: Path) -> None:
    """The falsifier.  Size every symbol alike and risk_spread converges on vol_spread."""
    targets = dict.fromkeys(VOLS, 0.05)
    block = risk_adaptation(_store(tmp_path, targets, VOLS), DAY)
    assert math.isclose(block["compression"], 1.0, rel_tol=1e-12)
    assert block["status"] == "ALERT" and block["compression"] > RISK_COMPRESSION_LIMIT


def test_the_live_book_sits_far_below_the_limit_rather_than_just_inside_it(tmp_path: Path) -> None:
    """Measured 0.13 against a 0.50 bound: the limit is wide on purpose, not tuned to today."""
    targets = {symbol: 0.30 / vol for symbol, vol in VOLS.items()}
    targets["BNBUSDT"] *= 1.25  # the band holding a stale weight while sigma moved under it
    targets["AKEUSDT"] *= 0.80
    block = risk_adaptation(_store(tmp_path, targets, VOLS), DAY)
    assert block["compression"] < 0.20, "honest path-dependence must not approach the limit"


def test_a_cycle_written_before_asset_vol_existed_refuses_instead_of_passing(tmp_path: Path) -> None:
    """The failure this codebase keeps finding: a number that reads like a pass while measuring nothing."""
    block = risk_adaptation(_store(tmp_path, dict.fromkeys(VOLS, 0.05), None), DAY)
    assert block["enforced"] is False and "predates" in block["reason"]
    assert "status" not in block, "no verdict at all, rather than OK"


def test_a_book_of_two_names_refuses_because_a_spread_over_two_is_noise(tmp_path: Path) -> None:
    targets = {"BTCUSDT": 0.06, "AKEUSDT": 0.005, "ETHUSDT": 0.0}
    block = risk_adaptation(_store(tmp_path, targets, VOLS), DAY)
    assert block["enforced"] is False and "noise" in block["reason"]
    assert len(block["rows"]) == 2, "the zero-weight symbol is not held"


def test_no_cycles_that_day_refuses(tmp_path: Path) -> None:
    block = risk_adaptation(_store(tmp_path, {"BTCUSDT": 0.06}, VOLS), "2020-01-01")
    assert block["enforced"] is False and "no cycles" in block["reason"]


def test_the_reported_divisor_is_the_one_build_weights_actually_divides_by(tmp_path: Path) -> None:
    """Anti-drift: the report's sigma must be the construction's sigma, not a re-derivation.

    Stage 1 is ``target * vol_target / asset_vol``, and stage 2 multiplies the whole row by one
    scalar, so with a constant conviction of 1 and no cap binding, ``weight * asset_vol`` is the
    SAME number for every symbol - ``vol_target`` times that scalar.  Asserting equality across
    symbols rather than equality to ``vol_target`` is deliberate: the scalar legitimately sits
    below 1 whenever the book's own volatility exceeds the target, which it does here.  If a later
    change makes the exported ``asset_vol`` describe a different quantity than the divisor, this
    identity breaks and this test is how it is noticed.
    """
    rng = np.random.default_rng(20260905)
    index = pd.date_range("2026-01-01", periods=600, freq="h", tz="UTC")
    close = pd.DataFrame(
        {
            name: 100.0 * np.exp(np.cumsum(rng.normal(0.0, scale, len(index))))
            for name, scale in (("CALM", 0.002), ("MID", 0.006), ("WILD", 0.020))
        },
        index=index,
    )
    params = PortfolioParams(vol_target=0.30, max_weight=10.0, max_gross=100.0, max_scalar=1.0)
    weights = build_weights(pd.DataFrame(1.0, index=index, columns=close.columns), close, 8760.0, params)
    sigma = asset_vol(close, params, 8760.0)
    risk = weights.iloc[-1] * sigma.iloc[-1]
    assert np.allclose(risk, risk.iloc[0], rtol=1e-9), risk.to_dict()
    assert 0.0 < float(risk.iloc[0]) <= params.vol_target, "the stage-2 scalar only ever shrinks the row"
    assert sigma.iloc[-1]["WILD"] / sigma.iloc[-1]["CALM"] > 5.0, "the fixture must have a real vol spread"
    assert math.isclose(
        float(weights.iloc[-1]["WILD"] / weights.iloc[-1]["CALM"]),
        float(sigma.iloc[-1]["CALM"] / sigma.iloc[-1]["WILD"]),
        rel_tol=1e-9,
    )
