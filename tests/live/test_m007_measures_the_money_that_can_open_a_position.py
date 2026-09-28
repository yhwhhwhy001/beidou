"""M-007's denominator (2026-09-19): `margin_cap` was calibrated where collateral does not exist.

The backtest models no collateral (RISK-G11), so `margin_cap: 0.40` was always a statement about the
USDT line.  Measuring it against total equity - about half BTC here - made the ruler 1.9x looser than
the policy it stands for, and M-007 had never once reported a breach.

This is the same correction as 2026-09-14, one layer down: that one fixed the BAR (the plan's 50%
judging where the profile said 40%), this one fixes the DENOMINATOR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beidou_live.reports import daily_alerts, margin_and_rejections
from beidou_live.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000
BAR = 1_789_000_000_000


def _store(tmp_path: Path, rows: list[dict]) -> StateStore:
    store = StateStore(tmp_path)
    with store.cycles_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    store.trades_path.write_text("", encoding="utf-8")
    return store


def _cycle(i: int, *, usage: float, equity: float, usdt: float, gross: float = 0.0) -> dict:
    return {
        "bar_open_ms": BAR + i * HOUR,
        "equity": equity,
        "margin_usage": usage,
        "gross_before": gross,
        "collateral": {"equity": equity, "usdt_equity": usdt, "collateral": equity - usdt},
    }


def test_the_tradable_reading_rescales_by_the_collateral_share(tmp_path: Path) -> None:
    """Half the equity is BTC, so the same book consumes twice the share of what can open a position."""
    store = _store(tmp_path, [_cycle(0, usage=0.20, equity=10_000.0, usdt=5_000.0, gross=9_000.0)])

    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    assert margin["peak_standing_usage"] == 0.20
    assert margin["peak_standing_usage_tradable"] == 0.40
    assert margin["last_gross_over_tradable"] == 1.80, "gross is 0.9x equity but 1.8x tradable"


def test_a_book_inside_the_budget_on_equity_can_be_outside_it_on_tradable_usdt(tmp_path: Path) -> None:
    """The 2026-09-13T22:00Z shape: 22.73% of equity, 48.71% of tradable, and M-007 said OK."""
    store = _store(tmp_path, [_cycle(0, usage=0.2273, equity=10_800.0, usdt=5_040.0)])

    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    assert margin["over_budget"] is False, "the old ruler cannot see it"
    assert margin["over_budget_tradable"] is True
    assert margin["peak_standing_usage_tradable"] > 0.48


def test_the_disagreement_between_the_two_rulers_is_reported_once(tmp_path: Path) -> None:
    """Not twice, and not under the same wording: the GAP is the finding, not a second breach."""
    store = _store(tmp_path, [_cycle(0, usage=0.2273, equity=10_800.0, usdt=5_040.0)])
    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    _, notices = daily_alerts({"margin": margin, "risk_budget": {}, "drift": {}, "restarts": {}})

    tradable = [n for n in notices if "可动用 USDT" in n]
    assert len(tradable) == 1
    assert "48" in tradable[0] and "22" in tradable[0], "both readings are in the one notice"
    assert "约束侧按可动用 USDT" in tradable[0], "and it says which ruler the constraint side divides by"
    assert "risk-g11-denominator" in tradable[0], "naming the ruling that put it there, not a date"


def test_the_notice_reads_both_rulers_on_the_tradable_peaks_own_bar(tmp_path: Path) -> None:
    """The two peaks can sit on different bars: 09-13T22:00Z and 09-14T02:00Z on the live record.

    The notice used to set the window's equity peak beside the tradable one under the words "同一根 bar",
    with a fixed "约 1.9 倍" after them that was neither ratio.  Both now come from the tradable peak's bar.
    """
    rows = [
        _cycle(0, usage=0.25, equity=10_000.0, usdt=8_000.0),  # the equity peak, 31.25% tradable
        _cycle(1, usage=0.22, equity=11_000.0, usdt=5_000.0),  # the tradable peak, 48.40%
    ]
    margin = margin_and_rejections(_store(tmp_path, rows), since_ms=None, margin_cap=0.40)

    _, notices = daily_alerts({"margin": margin, "risk_budget": {}, "drift": {}, "restarts": {}})

    (notice,) = [n for n in notices if "可动用 USDT" in n]
    assert margin["peak_standing_usage"] == 0.25 and margin["standing_usage_at_tradable_peak"] == 0.22
    assert "22.00%" in notice and "25.00%" not in notice, "the equity reading of the same bar, not the window's peak"
    assert "2.20 倍" in notice, "11,000 / 5,000 on that bar"


def test_a_breach_on_both_rulers_does_not_produce_two_notices(tmp_path: Path) -> None:
    store = _store(tmp_path, [_cycle(0, usage=0.55, equity=10_000.0, usdt=5_000.0)])
    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    _, notices = daily_alerts({"margin": margin, "risk_budget": {}, "drift": {}, "restarts": {}})

    assert margin["over_budget"] and margin["over_budget_tradable"]
    assert len([n for n in notices if "M-007" in n]) == 1


def test_a_cycle_without_a_collateral_reading_is_skipped_not_counted_as_zero(tmp_path: Path) -> None:
    """Cycles predate `collateral`; a missing reading must not enter the series as a flattering 0."""
    rows = [
        {"bar_open_ms": BAR, "equity": 10_000.0, "margin_usage": 0.30},  # no collateral block
        _cycle(1, usage=0.20, equity=10_000.0, usdt=5_000.0),
    ]
    store = _store(tmp_path, rows)

    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    assert margin["peak_standing_usage"] == 0.30, "the equity series still sees both"
    assert margin["peak_standing_usage_tradable"] == 0.40, "the tradable series sees only the priced one"


def test_a_pure_usdt_account_reads_the_same_on_both_rulers(tmp_path: Path) -> None:
    """The degenerate case that says the rescaling is a denominator swap and nothing else."""
    store = _store(tmp_path, [_cycle(0, usage=0.33, equity=7_000.0, usdt=7_000.0)])

    margin = margin_and_rejections(store, since_ms=None, margin_cap=0.40)

    assert margin["peak_standing_usage"] == margin["peak_standing_usage_tradable"]


def test_the_constraint_side_divides_by_the_usdt_balance_in_the_loop_only(tmp_path: Path) -> None:
    """The line this test used to pin is crossed now, by a ruling rather than by a tidy-up.

    Until 2026-09-28 it was `test_the_constraint_side_still_divides_by_total_equity`: `max_gross` and
    `margin_cap` on total equity, and no quiet unification of the denominators before the operator ruled.
    The operator ruled for the USDT line (`risk-g11-denominator`).  The ruling lives in the loop's guard,
    which scales `max_gross` by the USDT share; the shared `clamp_book` still has nowhere to pass one, because
    the backtest that shares it holds no collateral and its cap already is the USDT one.
    """
    import inspect

    from beidou_alpha.overlays.exposure import clamp_book
    from beidou_live.guards import evaluate_guards
    from tests.live.helpers_construction import live_config_for_profile

    assert list(inspect.signature(clamp_book).parameters) == ["values", "max_weight", "max_gross"]

    guards = live_config_for_profile().guards
    decision = evaluate_guards(
        {f"S{i}": 0.15 for i in range(10)},  # 1.5 x equity, 3 x a USDT balance of half of it
        current_weights={},
        kill_switch=False,
        equity=10_000.0,
        day_start_equity=10_000.0,
        latest_bar_ms=1,
        expected_bar_ms=1,
        interval_ms=1,
        params=guards,
        usdt_equity=5_000.0,
    )

    assert guards.max_gross_denominator == "usdt_equity" and decision.reasons == ["GROSS_CAPPED"]
    assert sum(abs(value) for value in decision.targets.values()) == pytest.approx(guards.max_gross * 0.5)


def test_the_report_says_what_the_ruling_on_file_says() -> None:
    """The M-007 notice and the gross line of `daily_markdown` both say the constraint side divides by USDT.

    First they named a date that went stale (「构造冻结到 2026-10-13」 outlived the freeze by a day), then an
    open entry (#225).  The operator ruled on 2026-09-28.  Both claims are checked where they live: the entry
    is resolved, and the shipped profile holds the denominator the two sentences name.  Reopen the entry or
    roll the profile back, and this goes red until the sentences say so.
    """
    from beidou_governance import reopen
    from tests.live.helpers_construction import live_config_for_profile

    entry = {entry.id: entry for entry in reopen.load(ROOT / reopen.LIST)}["risk-g11-denominator"]

    assert entry.check == "resolved", "the M-007 notice and the gross line say this was ruled; say what changed"
    assert live_config_for_profile().guards.max_gross_denominator == "usdt_equity", (
        "the M-007 notice and the gross line in `daily_markdown` say the cap reads the USDT balance"
    )
