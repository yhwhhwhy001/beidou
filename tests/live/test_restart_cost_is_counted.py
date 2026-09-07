"""AC-L4's other half: the numbers land in `cycles.jsonl` and the daily report never mentions them.

DL-L4 gave every restart two facts - how late it woke relative to the bar close, and whether that cost
a rebalance - and wrote them into the cycle row.  The acceptance criterion asks for them in the DAILY
REPORT, because that is where an operator sees them; in the JSONL they are read by nobody.

RISK-P2 is why they exist at all: every deployment restart is assumed to cost late fills and 7bps of
gross, and the plan asked for that assumption to be COUNTED rather than carried.  L1-01's error was the
same shape in reverse - measuring the fills that happened rather than the ones that should not have.
"""

from __future__ import annotations

from pathlib import Path

from beidou_live.reports import restart_cost


def test_a_clean_day_costs_nothing() -> None:
    rows = [{"bar": "t1", "skip": False}, {"bar": "t2", "skip": False}]

    assert restart_cost(rows) == {
        "missed_rebalances": 0,
        "skipped_bars": 0,
        "worst_late_seconds": None,
        "late_bars": 0,
    }


def test_a_late_restart_is_counted_not_assumed() -> None:
    """The real row from restart #1: 972.8s after the close, one rebalance skipped."""
    rows = [
        {"bar": "t1", "skip": False},
        {"bar": "t2", "phase": "SKIPPED", "late_seconds": 972.814, "missed_rebalances": 1},
    ]

    cost = restart_cost(rows)

    assert cost["missed_rebalances"] == 1
    assert cost["skipped_bars"] == 1
    assert cost["worst_late_seconds"] == 972.814
    assert cost["late_bars"] == 1


def test_a_prompt_restart_is_late_but_not_missed() -> None:
    """Restart #2 landed inside the window: late, and it still traded.  Both facts are kept."""
    rows = [{"bar": "t1", "late_seconds": 7.4}]

    cost = restart_cost(rows)

    assert cost["late_bars"] == 1
    assert cost["worst_late_seconds"] == 7.4
    assert cost["missed_rebalances"] == 0
    assert cost["skipped_bars"] == 0


def test_the_counter_is_a_running_total_not_a_sum() -> None:
    """`missed_rebalances` lives on the engine and counts up within one process's life.

    Summing the column would double-count every row after the first miss; the number wanted is how
    many were missed in the window, which is the largest value seen, per process.
    """
    rows = [
        {"bar": "t1", "missed_rebalances": 1, "phase": "SKIPPED"},
        {"bar": "t2", "missed_rebalances": 2, "phase": "SKIPPED"},
    ]

    assert restart_cost(rows)["missed_rebalances"] == 2


def test_a_restart_resets_the_counter_and_both_stretches_count() -> None:
    """The counter is per process, so a drop means a new process started; the day owes both."""
    rows = [
        {"bar": "t1", "missed_rebalances": 2, "phase": "SKIPPED"},
        {"bar": "t2", "missed_rebalances": 1, "phase": "SKIPPED"},
    ]

    assert restart_cost(rows)["missed_rebalances"] == 3


def test_the_daily_report_carries_it(tmp_path: Path) -> None:
    """Through the real path, not a hand-built payload: a store, the report, the rendered section."""
    from beidou_live.reports import daily_markdown, daily_payload
    from beidou_live.state import StateStore

    base = 1_756_800_000_000  # 2025-09-02T08:00Z
    store = StateStore(tmp_path)
    store.append_cycle(
        {"bar_open_ms": base, "equity": 10_000.0, "skip": False, "guard_reasons": [], "targets": {}, "orders": []}
    )
    store.append_cycle(
        {
            "bar_open_ms": base + 3_600_000,
            "equity": 10_000.0,
            "skip": False,
            "guard_reasons": [],
            "targets": {},
            "orders": [],
            "phase": "SKIPPED",
            "late_seconds": 972.814,
            "missed_rebalances": 1,
        }
    )

    payload = daily_payload(store, "2025-09-02", {})

    assert payload["restarts"]["missed_rebalances"] == 1
    assert payload["restarts"]["worst_late_seconds"] == 972.814
    text = daily_markdown(payload)
    assert "missed_rebalances" in text
    assert "972.8" in text
