"""The pool entry gate's arithmetic: which names the book could not open, and why that is a sizing fact.

Operator ruling 2026-09-30, the "faithful" option: a pool refresh skips a name whose would-be weight cannot
clear the band's entry line, and the next name by volume takes the slot.  The would-be weight is the book's
risk per name over the name's own sigma - under ``conviction_mode: sign`` every held name carries the same
``|w| * sigma``, and a name the book does not hold would be sized to that same number.

The reading that started it (2026-09-30, live heartbeat, k 0.175): risk per name 1.39% of equity, entry line
2.0 x 0.005 = 1%, so a name above about 139% annualised vol could not be opened.  AKEUSDT 281%, LSKUSDT 172%
and NEARUSDT 168% were flat on every planned cycle from 09-27T15:00Z; ENAUSDT at 131% was held.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_alpha.portfolio import PortfolioParams, enterable, entry_line, per_name_risk

INDEX = pd.date_range("2026-09-30", periods=3, freq="h", tz="UTC")
SIGMA = pd.DataFrame(
    {"BTCUSDT": 0.35, "ENAUSDT": 1.31, "NEARUSDT": 1.68, "LSKUSDT": 1.72, "AKEUSDT": 2.81}, index=INDEX
)


def test_the_entry_line_is_where_d2_and_d3_let_a_target_stand() -> None:
    shipped = PortfolioParams(no_trade_band=0.005, flat_inside_band=True, band_entry_multiple=2.0)
    assert entry_line(shipped) == pytest.approx(0.01)
    # Without D2 a target only has to clear the band, which is where the live planner blocks an entry.
    assert entry_line(PortfolioParams(no_trade_band=0.005, band_entry_multiple=2.0)) == pytest.approx(0.005)
    assert entry_line(PortfolioParams()) == 0.0, "no band: every target can stand, the gate removes nothing"


def test_the_risk_per_name_is_what_every_held_name_carries() -> None:
    risk = 0.0139
    weights = pd.DataFrame(
        {
            "BTCUSDT": risk / 0.35,
            "ENAUSDT": -risk / 1.31,  # a short carries the same risk
            "NEARUSDT": 0.0,  # not held: its zero says nothing about the book's scale
            "LSKUSDT": 0.0,
            "AKEUSDT": 0.0,
        },
        index=INDEX,
    )
    weights.iloc[2] = 0.0  # a bar where the book holds nothing
    out = per_name_risk(weights, SIGMA)
    assert out.iloc[:2].tolist() == pytest.approx([risk, risk])
    assert np.isnan(out.iloc[2]), "no book, no scale: NaN, never a zero that would gate every name out"


def test_a_name_is_enterable_when_its_would_be_weight_clears_the_line() -> None:
    risk = pd.Series([0.0139, 0.0139, np.nan], index=INDEX)
    sigma = SIGMA.copy()
    sigma.loc[INDEX[1], "ENAUSDT"] = np.nan  # no sigma yet: nothing to judge
    out = enterable(risk, sigma, 0.01)
    assert out.iloc[0].to_dict() == {
        "BTCUSDT": True,
        "ENAUSDT": True,  # 1.39 / 1.31 = 1.06% clears 1%
        "NEARUSDT": False,  # 0.83%
        "LSKUSDT": False,  # 0.81%
        "AKEUSDT": False,  # 0.49%
    }
    assert bool(out.loc[INDEX[1], "ENAUSDT"]), "a name without sigma is not judged, so not gated"
    assert bool(out.iloc[2].all()), "a bar without book risk gates nothing"


def test_the_gate_is_off_unless_named() -> None:
    assert PortfolioParams().pool_entry_gate is False
    assert PortfolioParams.from_mapping({"pool_entry_gate": True}).pool_entry_gate is True


def test_off_is_recorded_as_absent_so_no_digest_moves_and_on_cannot_hide_behind_an_old_report() -> None:
    """The field is written down only when on: every record made before it existed must read the same when off.

    And the other direction, which is the one that matters: a report written before the gate existed says
    nothing about it, and nothing is exactly what "off" means - so the startup gate must refuse to run the
    gate on the strength of such a report, rather than skip the comparison as it does for other absent keys.
    """
    from beidou_alpha.portfolio import as_recorded
    from beidou_alpha.registry import StrategyEntry, construction_problems, evidence_construction_digest

    off, on = PortfolioParams().__dict__, PortfolioParams(pool_entry_gate=True).__dict__
    assert "pool_entry_gate" not in as_recorded(off) and as_recorded(on)["pool_entry_gate"] is True
    without = {key: value for key, value in off.items() if key != "pool_entry_gate"}
    assert evidence_construction_digest(off, None, None) == evidence_construction_digest(without, None, None)
    assert evidence_construction_digest(on, None, None) != evidence_construction_digest(without, None, None)

    entry = StrategyEntry("tsmom")
    old_report = {"portfolio": {"vol_target": 0.15}}  # written before the gate existed
    assert construction_problems(entry, old_report, {**off, "vol_target": 0.15}) == []
    refused = construction_problems(entry, old_report, {**on, "vol_target": 0.15})
    assert refused and "pool_entry_gate" in refused[0], "gate on, evidence silent about it: refused"
    assert construction_problems(entry, {"portfolio": {**on, "vol_target": 0.15}}, {**off, "vol_target": 0.15})
