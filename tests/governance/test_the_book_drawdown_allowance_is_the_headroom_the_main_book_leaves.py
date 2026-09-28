"""EXP-AE2, exercised: D-018's drawdown allowance is the headroom the main book leaves under the budget.

D-018 lets a sleeve worsen the book's out-of-sample drawdown by at most `max_oos_mdd_worsening`.  From the
first run that was 1pp, a number with no derivation behind it.  On 2026-09-17 one was written down before any
candidate was read (RESEARCH_LOG, EXP-AE2): the allowance is the declared drawdown budget minus the main book's
bootstrap q95 drawdown, in the tighter of the two universes.  Its inputs all exist before any candidate and none
is read off one, which is what makes it a rule rather than a fit.  Exercising it waited for the operator to decide
k: 0.175, live since 2026-09-27.  The same day the operator asked for the allowance to be re-derived and put up
for signature (card 「先改规则」).

The one thing the pre-registration could not say is the unit, because on 09-17 there was only one.  Since policy
0.3.6 the -70% budget is counted in tradable USDT, while a research backtest - and so the D-018 reading - is in
the book's own capital, i.e. total equity.  In total equity the budget is 0.70 / 1.7479 = 0.4005, which is R8's
`rollback_at`.  The "+4.40pp" beside k=0.175 in `live.demo.yaml` is the same headroom in tradable USDT: charged
against a research reading it would let a sleeve take the static q95 to about -73% of the tradable money, past
the budget the allowance exists to protect.

Every choice the pre-registration left open went the tighter way, and `test_every_open_choice_took_the_tighter_side`
holds that: total-equity units (2.5pp, not 4.4), honest drift (not the 9.4pp the in-sample drift gives), the
point q95 the pre-registration names, floored rather than rounded.

What this does NOT do.  It moves no archived verdict: `research book` passes `BOOK_RULE` in at run time and a
report records the rule it was judged by, so only later runs see 2.5pp.  The live loop does not read it.  And it
does not follow the collateral: the allowance is 0.70 / factor - 0.3753, so it shrinks as bitcoin's share of the
account grows and reaches zero at a factor of 1.865 (it read 1.861 on 2026-09-20), which is also where the main
book alone crosses the budget.  The constant is re-derived when k or the rungs are, not every hour.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from beidou_cli.research_book_eval import BOOK_RULE
from beidou_governance.policy import Policy

ROOT = Path(__file__).resolve().parents[2]

# The run the k decision itself read (`live.demo.yaml`, the `vol_target` paragraph of 2026-09-27): 2,000 draws per
# universe, honest and in-sample drift, the 09-25 daily report's factor.  Pinned rather than globbed, so a new k
# has to name its own run here.
K_BISECT = ROOT / "reports" / "research" / "k-bisect-0175-20260925.json"

# The operator's -70%, counted in tradable USDT since policy 0.3.6.
DECLARED_BUDGET = 0.70


def _bisect() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = json.loads(K_BISECT.read_text(encoding="utf-8"))
    return rows[0], rows[1:]


def _vol_target() -> float:
    profile = yaml.safe_load((ROOT / "config" / "live.demo.yaml").read_text(encoding="utf-8"))
    return float(profile["portfolio"]["vol_target"])


def _main_book(rows: list[dict[str, Any]], *, k: float, mode: str, drift: str) -> dict[str, Any]:
    (row,) = [
        r
        for r in rows
        if r["set"] == "main"
        and r["arm"] == "ladder@k"
        and r["rungs"] == "today"
        and r["k"] == k
        and r["mode"] == mode
        and r["drift"] == drift
    ]
    return row


def _headroom(meta: dict[str, Any], row: dict[str, Any]) -> float:
    """Budget minus the main book's q95, both in total equity (the unit a research backtest reads)."""
    return DECLARED_BUDGET / float(meta["factor_today"]) - abs(float(row["q95_total"]))


def test_the_allowance_is_the_budget_minus_the_main_book_q95_in_the_tighter_universe() -> None:
    meta, rows = _bisect()
    k = _vol_target()
    assert meta["chosen_k"] == k, (
        f"the live k is {k} but the bootstrap this allowance was derived from chose {meta['chosen_k']}.  "
        "A new k is a new main book: re-derive D-018's `max_oos_mdd_worsening` by EXP-AE2 "
        "(RESEARCH_LOG 2026-09-17 §五) from that k's own bootstrap, and point K_BISECT at it."
    )
    rollback_at = -min(threshold for threshold, _ in Policy().drawdown_ladder)
    assert DECLARED_BUDGET / meta["factor_today"] == pytest.approx(rollback_at, abs=1e-4), (
        "R8's rungs no longer sit on this budget and factor; re-derive the allowance with them."
    )

    by_universe = {
        mode: _headroom(meta, _main_book(rows, k=k, mode=mode, drift="honest")) for mode in ("pit", "static")
    }

    assert by_universe["static"] == pytest.approx(0.02515, abs=1e-5)
    assert by_universe["pit"] == pytest.approx(0.03831, abs=1e-5)
    tighter = min(by_universe.values())
    assert BOOK_RULE["max_oos_mdd_worsening"] == math.floor(tighter * 1000) / 1000 == 0.025


def test_the_usdt_headroom_is_the_same_number_in_other_units() -> None:
    """4.40pp of tradable USDT and 2.52pp of total equity are one headroom; the conversion is the factor."""
    meta, rows = _bisect()
    factor = float(meta["factor_today"])
    for mode in ("pit", "static"):
        row = _main_book(rows, k=meta["chosen_k"], mode=mode, drift="honest")
        today = row["by_factor"]["today"]
        assert today["q95_usdt"] == pytest.approx(row["q95_total"] * factor)
        assert today["margin_pp"] / 100.0 / factor == pytest.approx(_headroom(meta, row))

    static = _main_book(rows, k=meta["chosen_k"], mode="static", drift="honest")
    assert static["by_factor"]["today"]["margin_pp"] == pytest.approx(4.396, abs=1e-3)
    # What 4.4pp would have allowed, had it been read as total equity: the static q95 in tradable USDT.
    assert (abs(static["q95_total"]) + 0.04396) * factor == pytest.approx(0.7329, abs=1e-4)


def test_every_open_choice_took_the_tighter_side() -> None:
    meta, rows = _bisect()
    k = meta["chosen_k"]
    for mode in ("pit", "static"):
        honest = _headroom(meta, _main_book(rows, k=k, mode=mode, drift="honest"))
        in_sample = _headroom(meta, _main_book(rows, k=k, mode=mode, drift="orig"))
        assert honest < in_sample, f"{mode}: honest drift must be the tighter reading"
        tradable = _main_book(rows, k=k, mode=mode, drift="honest")["by_factor"]["today"]["margin_pp"] / 100.0
        assert honest < tradable, f"{mode}: total-equity units must be the tighter reading"
    assert BOOK_RULE["max_oos_mdd_worsening"] <= min(
        _headroom(meta, _main_book(rows, k=k, mode=mode, drift="honest")) for mode in ("pit", "static")
    )


def test_the_allowance_is_zero_where_the_main_book_alone_reaches_the_budget() -> None:
    """The headroom is 0.70 / factor - 0.3753: it follows bitcoin's share of the account, and runs out at 1.865."""
    meta, rows = _bisect()
    static = _main_book(rows, k=meta["chosen_k"], mode="static", drift="honest")
    exhausted_at = DECLARED_BUDGET / abs(static["q95_total"])
    assert exhausted_at == pytest.approx(1.8650, abs=1e-4)
    assert DECLARED_BUDGET / 1.861 - abs(static["q95_total"]) == pytest.approx(0.0008, abs=1e-4), (
        "at the factor read on 2026-09-20 the allowance would have been 0.08pp"
    )
