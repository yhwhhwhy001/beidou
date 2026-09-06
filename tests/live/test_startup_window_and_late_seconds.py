"""DL-L4: a restart always reconciles; it only rebalances if it is still on time.

``deploy/run_live.sh`` passes ``--immediate`` unconditionally, so every restart re-runs the last
closed bar - which is how 90 of 119 fills landed a median 21.6 minutes after the close (E-18).  The
09-05 prescription was "only go immediate if ``now - last_close < 120s``", and KILL-R6 took it apart:
a 143-second catch-up would become a 57-minute stale book, a flatten-then-restart an hour of no
positions, and a crash up to 59 minutes with nobody reconciling the account.

So the two halves are separated, which is what the fix should always have been.

* **Reconciliation always runs.**  It is read-only, it is what discovers foreign positions and
  orphaned orders, and there is no window in which skipping it is safer.
* **Rebalancing is what the window gates**, and the window is *derived* rather than picked: the
  grace period the scheduler already waits, plus launchd's ThrottleInterval, plus how long this
  process actually took to start.  Outside it the bar is skipped and counted - the count is the
  point, because L1-01's error was measuring the fills that happened rather than the ones that
  should not have.
"""

from __future__ import annotations

from beidou_live.scheduler import rebalance_window_seconds, within_rebalance_window


def test_the_window_is_derived_from_the_parts_not_chosen() -> None:
    """grace + ThrottleInterval + startup cost.  120 was a guess; this is an addition."""
    assert rebalance_window_seconds(grace_seconds=5.0, throttle_interval=60.0, startup_seconds=90.0) == 155.0


def test_a_prompt_restart_still_rebalances() -> None:
    """The 143-second catch-up KILL-R6 used as its counter-example must stay inside the window."""
    window = rebalance_window_seconds(grace_seconds=5.0, throttle_interval=60.0, startup_seconds=90.0)

    assert within_rebalance_window(seconds_since_close=143.0, window_seconds=window) is True


def test_an_hour_late_does_not_rebalance() -> None:
    window = rebalance_window_seconds(grace_seconds=5.0, throttle_interval=60.0, startup_seconds=90.0)

    assert within_rebalance_window(seconds_since_close=3_600.0, window_seconds=window) is False


def test_the_boundary_is_inclusive_so_a_window_of_exactly_n_still_trades() -> None:
    assert within_rebalance_window(seconds_since_close=155.0, window_seconds=155.0) is True
    assert within_rebalance_window(seconds_since_close=155.1, window_seconds=155.0) is False


def test_a_negative_age_is_inside_the_window() -> None:
    """Clock skew between host and venue must not turn into a skipped bar (D-030)."""
    assert within_rebalance_window(seconds_since_close=-2.0, window_seconds=155.0) is True


def test_late_seconds_is_derived_from_the_bar_not_from_a_wall_clock_guess() -> None:
    from beidou_live.scheduler import late_seconds

    bar_open_ms = 1_757_145_600_000
    interval_ms = 3_600_000
    close_ms = bar_open_ms + interval_ms

    assert late_seconds(bar_open_ms, interval_ms, at_ms=close_ms + 21_600) == 21.6
    assert late_seconds(bar_open_ms, interval_ms, at_ms=close_ms) == 0.0


def test_a_fill_before_the_close_reports_zero_not_a_negative() -> None:
    """`late_seconds` measures lateness; earliness is not negative lateness, it is none."""
    from beidou_live.scheduler import late_seconds

    bar_open_ms = 1_757_145_600_000
    interval_ms = 3_600_000

    assert late_seconds(bar_open_ms, interval_ms, at_ms=bar_open_ms + interval_ms - 5_000) == 0.0
