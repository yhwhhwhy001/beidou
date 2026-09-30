"""The decay reading names the regime it was taken in (backtest-guard 2026-09-30, Ⅲ.1) - reported, never enforced.

The in-force tsmom evidence reads its OOS Sharpe by basket-volatility tercile at 2.97 / 1.44 / 0.91 while M-010
and the decay rule compare against the unconditional number.  So the daily report prints, beside the decay row,
where today's basket volatility sits in that split.  Whether the rule itself should condition on it is the
operator's call; nothing here changes a status or a page.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from beidou_live.report_decay import _decay_lines, _regime_line, expectations_from_evidence

SPLIT = {
    "low": {"bars": 3, "sharpe": 2.97, "vol_from": 0.40, "vol_to": 0.71},
    "mid": {"bars": 3, "sharpe": 1.44, "vol_from": 0.71, "vol_to": 0.88},
    "high": {"bars": 3, "sharpe": 0.91, "vol_from": 0.88, "vol_to": 2.23},
}


def _payload(annual_vol: float | None, split: dict[str, Any] | None = SPLIT) -> dict[str, Any]:
    basket = (
        {"measured": False, "why": "no members"}
        if annual_vol is None
        else {"measured": True, "sigma_1h": annual_vol / math.sqrt(8760.0)}
    )
    return {
        "decay": {"tsmom": {"status": "INSUFFICIENT_DATA", "why": "0 whole windows, needs 2", "bars": 28}},
        "evidence_window": {"since_ms": 1_790_000_000_000, "construction": "e32f3856ac1e"},
        "expectations": {"tsmom": {"oos_window_sharpe_q10": -1.85, "regime_split": split}},
        "event_risk": {"market": {"series": {"basket": basket}}},
    }


def test_the_evidence_the_registry_cites_carries_its_regime_split() -> None:
    registry = yaml.safe_load(Path("config/alpha_registry.yaml").read_text(encoding="utf-8"))
    entry = next(s for s in registry["strategies"] if s["id"] == "tsmom")
    report = json.loads(Path(entry["evidence"]["report"]).read_text(encoding="utf-8"))
    split = expectations_from_evidence({"tsmom": report})["tsmom"]["regime_split"]
    assert sorted(split) == ["high", "low", "mid"]
    assert (
        split["low"]["vol_to"] <= split["mid"]["vol_from"] + 1e-9
        and split["mid"]["vol_to"] <= split["high"]["vol_from"] + 1e-9
    )
    assert split["low"]["sharpe"] > split["high"]["sharpe"], "the finding this line exists for"


@pytest.mark.parametrize(
    ("annual_vol", "expected"),
    [
        (0.80, "落在证据的 mid 段（0.71–0.88）"),
        (0.30, "低于证据最低段的下沿 0.40"),
        (2.50, "高于证据最高段的上沿 2.23"),
    ],
)
def test_the_line_places_today_in_the_split(annual_vol: float, expected: str) -> None:
    line = _regime_line(_payload(annual_vol), "tsmom")
    assert line is not None and expected in line, line
    assert f"篮子年化波动约 {annual_vol:.2f}" in line
    assert "low 2.97 / mid 1.44 / high 0.91" in line, "tiers are listed low to high by their edges"


def test_no_basket_or_no_split_prints_nothing_rather_than_a_guess() -> None:
    assert _regime_line(_payload(None), "tsmom") is None
    assert _regime_line(_payload(0.80, split=None), "tsmom") is None
    assert _regime_line(_payload(0.80), "flow") is None


def test_the_decay_section_prints_it_beside_the_status_it_changes_nothing_in() -> None:
    lines = _decay_lines(_payload(0.80))
    assert "mid 段" in lines["tsmom regime"]
    assert lines["tsmom"].startswith("INSUFFICIENT_DATA"), "the reading beside it is untouched"
