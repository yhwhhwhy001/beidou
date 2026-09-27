"""DL-G5 / DRILL-G4: the deployment-health checks, and the boundary the Phase 7 review drew.

The review's sharpest correction (KILL-AR-04) was that calling the canary a filter would do two kinds
of harm: let a bad candidate through on a technicality, and let a good one be blocked by an unrelated
venue hiccup - then be recorded as evidence against the candidate.  So the first test here is the
negative one: a shadow that is perfectly healthy and perfectly unprofitable must pass.

Every rate is measured against the ARMED loop's own cycles over the same window.  A constant would
fail the whole canary on a day the venue was slow, which is the false negative that teaches an
operator to ignore an instrument.
"""

from __future__ import annotations

from typing import Any

import pytest

from beidou_governance.canary import SOAK_CYCLES, evaluate

HOUR_MS = 3_600_000
FIRST_BAR_MS = 1_767_225_600_000  # 2026-01-01T00:00Z


def _cycles(
    n: int,
    *,
    first: int = 0,
    construction: str = "aaa",
    guards: int = 0,
    phase: str | None = None,
    universe: tuple[str, ...] = ("A",),
) -> list[dict[str, Any]]:
    """``n`` hourly cycles from bar ``first``; the first ``guards`` of them fired a guard."""
    rows = []
    for i in range(n):
        row: dict[str, Any] = {
            "at": f"2026-01-01T{i:02d}:00:00+00:00",
            "bar_open_ms": FIRST_BAR_MS + (first + i) * HOUR_MS,
            "construction": construction,
            "guard_reasons": ["MAX_GROSS"] if i < guards else [],
            "universe": list(universe),
            "targets": dict.fromkeys(universe, 0.1),
            "skipped": [],
        }
        if phase:
            row["phase"] = phase
        rows.append(row)
    return rows


def test_a_healthy_and_unprofitable_shadow_passes() -> None:
    """KILL-AR-04.  Nothing here reads P&L, and a canary that did would be an alpha filter."""
    shadow = _cycles(SOAK_CYCLES)
    for row in shadow:
        row["equity"] = 100.0 - row["at"].count("0")  # losing money, healthily
    assert evaluate(shadow, _cycles(SOAK_CYCLES)).healthy


def test_an_unfinished_soak_is_not_a_pass() -> None:
    result = evaluate(_cycles(SOAK_CYCLES - 1), _cycles(SOAK_CYCLES))
    assert not result.healthy
    assert [c.name for c in result.failures] == ["soak"]


def test_drill_g4_the_startup_gate_is_asked_by_the_write_not_the_canary() -> None:
    """2026-09-27, operator ruling: the canary stopped carrying a `startup_gate` check.

    Nothing ever supplied it.  The shadow is a dry run, a dry run never refuses, and `plan`/`apply`
    passed no count, so the check read `0 refusals` whatever the soak saw.  Measured that day: the
    process writing the scored round had printed `evidence: tsmom: evidence verdict FAIL does not allow
    live use` when it started.  DRILL-G4's production run (2026-09-12) was already the other path - the
    transaction's gate refused the write and rolled it back - and that gate now asks both halves `live
    run` refuses on (`tests/cli/test_the_write_refuses_what_an_armed_start_refuses.py`).
    """
    result = evaluate(_cycles(SOAK_CYCLES), _cycles(SOAK_CYCLES))
    assert result.healthy
    assert "startup_gate" not in [c.name for c in result.checks]
    with pytest.raises(TypeError, match="gate_refusals"):
        evaluate(_cycles(SOAK_CYCLES), _cycles(SOAK_CYCLES), gate_refusals=1)  # type: ignore[call-arg]


def test_a_construction_that_moves_mid_soak_fails() -> None:
    shadow = _cycles(SOAK_CYCLES)
    shadow[100]["construction"] = "bbb"
    assert "construction_stable" in [c.name for c in evaluate(shadow, _cycles(SOAK_CYCLES)).failures]


def test_guards_firing_at_the_armed_books_own_rate_is_a_pass() -> None:
    """A canary that demands an improvement over the armed book is asking the wrong question."""
    shadow = _cycles(SOAK_CYCLES, guards=40)
    armed = _cycles(SOAK_CYCLES, guards=40)
    assert evaluate(shadow, armed).healthy
    # Materially worse is not.
    assert "guard_rate" in [c.name for c in evaluate(_cycles(SOAK_CYCLES, guards=120), armed).failures]


def test_an_error_streak_fails_but_a_single_error_does_not() -> None:
    """A 503 burst through the proxy is the venue, not the candidate; a sustained one is a deployment."""
    one = _cycles(SOAK_CYCLES)
    one[5]["phase"] = "ERROR"
    assert evaluate(one, _cycles(SOAK_CYCLES)).healthy

    many = _cycles(SOAK_CYCLES)
    for i in range(5, 12):
        many[i]["phase"] = "ERROR"
    assert "no_error_streak" in [c.name for c in evaluate(many, _cycles(SOAK_CYCLES)).failures]


def test_a_target_outside_the_managed_universe_fails() -> None:
    shadow = _cycles(SOAK_CYCLES)
    shadow[3]["targets"]["NOT_MANAGED"] = 0.2
    assert "targets_in_universe" in [c.name for c in evaluate(shadow, _cycles(SOAK_CYCLES)).failures]


def test_an_order_the_rebalancer_would_truncate_fails() -> None:
    shadow = _cycles(SOAK_CYCLES)
    shadow[7]["skipped"] = [{"symbol": "A", "reason": "PARTICIPATION_CAP"}]
    assert "participation" in [c.name for c in evaluate(shadow, _cycles(SOAK_CYCLES)).failures]


def test_an_empty_shadow_is_never_healthy() -> None:
    """Vacuous truth is the failure mode of every all() over an empty sequence."""
    assert not evaluate([], []).healthy


# --- the baseline is the armed loop over the round's own bars --------------------------------------
#
# Until 2026-09-26 both callers handed `evaluate` the armed loop's whole `cycles.jsonl` while three
# docstrings said "the same window".  Measured that day on the real records: no guard had fired in
# 1,397 rows across five loops, so no reading moved; the denominator went from 577 to 81.


def test_an_old_storm_in_the_armed_record_buys_the_candidate_no_headroom() -> None:
    """Read against the whole file, guards the armed book hit weeks earlier raise the shadow's bar.

    Here far enough to hide a candidate capping its gross on one bar in eight, while the armed book,
    over the same bars, capped nothing.  The shadow sat through the storm too, in the round before the
    scored one; that round is listed and not scored, and it lends the scored one no window either.
    """
    armed = _cycles(SOAK_CYCLES, first=-SOAK_CYCLES, guards=60) + _cycles(SOAK_CYCLES)
    shadow = _cycles(SOAK_CYCLES, first=-SOAK_CYCLES, guards=60) + _cycles(SOAK_CYCLES, guards=21)
    assert [c.name for c in evaluate(shadow, armed).failures] == ["guard_rate"]


def test_an_outage_both_loops_sat_through_does_not_fail_the_candidate() -> None:
    """The false negative this module exists to avoid, arriving through the denominator.

    Stale bars hit both loops, which read one venue through one proxy.  Over the round's own bars the
    two rates match; spread over the armed loop's whole history they do not, and 20 stale bars in 168
    read as a candidate that guards three times as often as the book it would join.
    """
    armed = _cycles(400, first=-400) + _cycles(SOAK_CYCLES, guards=20)
    shadow = _cycles(SOAK_CYCLES, guards=20)
    assert evaluate(shadow, armed).healthy


def test_no_armed_cycle_beside_the_round_is_not_a_pass() -> None:
    """Could not be compared is not passed, and the detail says which fact was missing.

    An armed loop that decided nothing over the round's bars - stopped, or a record from before the
    soak - leaves no baseline.  Nor does a round whose cycles carry no bar to place it by.
    """
    unstamped = [{k: v for k, v in row.items() if k != "bar_open_ms"} for row in _cycles(SOAK_CYCLES)]
    for shadow, armed in (
        (_cycles(SOAK_CYCLES), []),
        (_cycles(SOAK_CYCLES), _cycles(SOAK_CYCLES, first=-SOAK_CYCLES)),
        (unstamped, _cycles(SOAK_CYCLES)),
    ):
        result = evaluate(shadow, armed)
        assert [c.name for c in result.failures] == ["guard_rate"]
        assert "no decided armed cycle" in result.failures[0].detail, result.failures[0].detail
