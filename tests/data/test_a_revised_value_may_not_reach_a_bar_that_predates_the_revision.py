"""#32's whole reason for existing, written as a test that has to be able to go red.

Every ledger in this file is REAL: the vintages were read from api.stlouisfed.org on 2026-09-09 and
pasted in, because a synthetic revision history would let the check pass on data that has never had a
revision in it.  Nothing here reaches the network.

The measurement, once: January 2024 nonfarm payrolls was first published as 157,700 on 2024-02-02 and
reads 157,032 today, 668,000 lower, after four revisions - the last of them written 2026-02-11, which
is 739 days after a February 2024 bar would have read it.  A backtest that queries FRED without a
real-time window gets today's number for that bar and nothing anywhere reports it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beidou_data.alignment import PASS, UNVERIFIABLE, Verification
from beidou_data.alignment import admits_live_signal as alignment_admits_live_signal
from beidou_data.macro import (
    AVAILABLE,
    DAY_MS,
    MACRO,
    MACRO_COLUMNS,
    MAX_STALENESS_MS,
    REFERENCE_OPEN,
    RELEASE_AVAILABLE_LAG_MS,
    RELEASED,
    admits_live_signal,
    align_family_to_bars,
    align_releases_to_bars,
    as_of,
    broadcast_to_symbols,
    parse_release_ledger,
    revision_history,
    revision_leak,
)

COLUMN = "macro_nonfarm_payrolls"
HOUR_MS = 3_600_000

# Read from ALFRED on 2026-09-09: every vintage of three consecutive reference months.  The 2024-01
# rows are the operator's table; the neighbours are here because the head of the series has to be
# exercised across a MONTH boundary and not only across a revision.
PAYEMS_VINTAGES: tuple[tuple[str, str, str], ...] = (
    ("2023-12-01", "2024-01-05", "157232"),
    ("2023-12-01", "2024-02-02", "157347"),
    ("2023-12-01", "2024-03-08", "157304"),
    ("2023-12-01", "2025-02-07", "156930"),
    ("2023-12-01", "2026-02-11", "156857"),
    ("2024-01-01", "2024-02-02", "157700"),
    ("2024-01-01", "2024-03-08", "157533"),
    ("2024-01-01", "2024-04-05", "157560"),
    ("2024-01-01", "2025-02-07", "157049"),
    ("2024-01-01", "2026-02-11", "157032"),
    ("2024-02-01", "2024-03-08", "157808"),
    ("2024-02-01", "2024-04-05", "157830"),
    ("2024-02-01", "2024-05-03", "157796"),
    ("2024-02-01", "2025-02-07", "157271"),
    ("2024-02-01", "2026-02-11", "157238"),
)

# The 2025 US government shutdown, also real.  September 2025's payrolls did not appear until
# 2025-11-20 - 76 days after the previous print - and October AND November 2025 both first appeared on
# 2025-12-16, which is the case that breaks any aligner assuming one release per month.
SHUTDOWN_VINTAGES: tuple[tuple[str, str, str], ...] = (
    ("2025-08-01", "2025-09-05", "159540"),
    ("2025-08-01", "2025-11-20", "159507"),
    ("2025-08-01", "2025-12-16", "159485"),
    ("2025-09-01", "2025-11-20", "159626"),
    ("2025-09-01", "2025-12-16", "159593"),
    ("2025-10-01", "2025-12-16", "159488"),
    ("2025-11-01", "2025-12-16", "159552"),
    ("2025-10-01", "2026-01-09", "159420"),
    ("2025-11-01", "2026-01-09", "159476"),
)


def ms(stamp: str) -> int:
    return int(pd.Timestamp(stamp, tz="UTC").timestamp() * 1000)


def _ledger(rows: tuple[tuple[str, str, str], ...] = PAYEMS_VINTAGES) -> pd.DataFrame:
    """Through `parse_release_ledger`, never hand-built, so the parser is under test here too."""
    payload = {
        "observations": [
            {"date": date, "realtime_start": released, "realtime_end": "9999-12-31", "value": value}
            for date, released, value in rows
        ]
    }
    return parse_release_ledger(payload, COLUMN)


def _bars(start: str, periods: int, freq: str = "1h") -> pd.DatetimeIndex:
    return pd.date_range(pd.Timestamp(start, tz="UTC"), periods=periods, freq=freq)


# --------------------------------------------------------------------------------------------------
# The three things a macro contract has to keep apart: reference period, first release, revision history
# --------------------------------------------------------------------------------------------------


def test_the_ledger_reproduces_the_operators_table_of_what_january_2024_payrolls_was_worth_when() -> None:
    """The revision history, which is the column `EventTimeContract` has no field for.

    One row per RELEASE, so a revision is additive.  The 668,000 between the first and the last is the
    entire argument for this feed, and it is asserted rather than described so a parser change that
    collapses the history to its head cannot pass.
    """
    history = revision_history(_ledger(), ms("2024-01-01"), COLUMN)

    assert [pd.Timestamp(v, unit="ms", tz="UTC").date().isoformat() for v in history[RELEASED]] == [
        "2024-02-02",
        "2024-03-08",
        "2024-04-05",
        "2025-02-07",
        "2026-02-11",
    ]
    assert list(history[COLUMN]) == [157700.0, 157533.0, 157560.0, 157049.0, 157032.0]
    assert history[COLUMN].iloc[0] - history[COLUMN].iloc[-1] == 668.0  # thousands of persons


def test_an_as_of_query_returns_the_series_as_it_stood_and_not_as_it_reads_today() -> None:
    """`as_of` is the primitive; the aligner is only its head.  Both directions are asserted.

    A reference period with no release yet is ABSENT rather than NaN, because "not published" and
    "published as nothing" are different facts and only the first is true.
    """
    ledger = _ledger()

    early = as_of(ledger, ms("2024-02-05"), COLUMN)
    late = as_of(ledger, ms("2026-09-09"), COLUMN)

    assert list(early[REFERENCE_OPEN]) == [ms("2023-12-01"), ms("2024-01-01")]
    assert list(early[COLUMN]) == [157347.0, 157700.0]
    assert list(late[COLUMN]) == [156857.0, 157032.0, 157238.0]
    # February 2024 had not been published on 2024-02-05 and is absent, not zero and not NaN.
    assert ms("2024-02-01") not in set(early[REFERENCE_OPEN])
    assert as_of(ledger, ms("2023-01-01"), COLUMN).empty


def test_every_release_lands_after_the_contracts_arithmetic_floor_and_the_floor_has_no_slack() -> None:
    """Note 2: `available_offset_ms` is a floor to check the data against, not the number in use.

    What must be TRUE is that the reference month has ended, which is 31 days for the longest month.
    The real releases clear it by 33 to 400+ days and the tightest is exactly the case the floor was
    sized for - a 31-day month whose print landed days after it closed - so the floor is asserted to
    hold, and asserted to be BELOW the real availability everywhere, which is what makes it safe to
    keep as a declaration rather than delete.
    """
    ledger = _ledger()

    floor = ledger[REFERENCE_OPEN] + MACRO.available_offset_ms

    assert bool((ledger[AVAILABLE] >= floor).all()), "a release predates the end of the month it describes"
    assert MACRO.available_from(0) == 31 * DAY_MS
    # And the floor is never the number the aligner uses: the real lag is always strictly larger.
    assert bool((ledger[AVAILABLE] > floor).all())


# --------------------------------------------------------------------------------------------------
# The test this column exists for
# --------------------------------------------------------------------------------------------------


def test_a_bar_in_february_2024_reads_the_january_print_and_not_the_number_written_in_2026() -> None:
    """The headline claim, on real bars.  157,700 was the only value that existed then."""
    bars = _bars("2024-02-05", 24)

    aligned = align_releases_to_bars(_ledger(), bars, COLUMN, interval_ms=HOUR_MS)

    assert set(aligned) == {157700.0}
    assert 157032.0 not in set(aligned), "today's value must not appear on a 2024 bar"


def test_substituting_the_later_revision_into_an_early_bar_makes_the_check_fail() -> None:
    """The red half.  A check that cannot fail here buys nothing, so the failure is the assertion.

    The correctly aligned frame is clean; the same frame with ONE cell replaced by the value written
    on 2026-02-11 is not, and the report names the bar, the value and how far in the future the
    release that first carried it was.  `revision_leak` asks the ledger directly rather than
    re-running the aligner, so this stays red even if the aligner is the thing that broke.
    """
    ledger, bars = _ledger(), _bars("2024-02-05", 24)
    aligned = align_releases_to_bars(ledger, bars, COLUMN, interval_ms=HOUR_MS)

    assert revision_leak(aligned, ledger, COLUMN, interval_ms=HOUR_MS).clean

    leaked = aligned.copy()
    leaked.iloc[0] = 157032.0  # the value FRED returns today for this reference month
    report = revision_leak(leaked, ledger, COLUMN, interval_ms=HOUR_MS)

    assert not report.clean, "a value written in 2026 was accepted on a bar in 2024"
    assert report.leaked == 1 and report.checked == 24
    assert report.worst_bar_ms == ms("2024-02-05") and report.worst_value == 157032.0
    assert report.worst_available_ms == ms("2026-02-13")  # 2026-02-11 plus the two-day rounding
    assert (report.worst_available_ms - report.worst_bar_ms) // DAY_MS == 739
    assert "739 days early" in report.reason


def test_a_value_the_ledger_never_published_at_all_is_the_worst_kind_of_leak() -> None:
    """The other failure the checker has to name: a number from nowhere.

    Ranked above every merely-early bar because there is no release to date it against, so nothing can
    bound how wrong it is - a forward-filled zero and a hand-edited cell both land here.
    """
    ledger, bars = _ledger(), _bars("2024-02-05", 6)
    aligned = align_releases_to_bars(ledger, bars, COLUMN, interval_ms=HOUR_MS)
    aligned.iloc[3] = 0.0  # the classic: a missing macro value filled with zero

    report = revision_leak(aligned, ledger, COLUMN, interval_ms=HOUR_MS)

    assert not report.clean and report.leaked == 1
    assert report.worst_value == 0.0 and report.worst_available_ms is None
    assert "never published" in report.reason


def test_a_number_republished_years_later_is_still_dated_by_when_it_FIRST_appeared() -> None:
    """The half of `revision_leak` a same-value coincidence would otherwise break, and it is not rare.

    Measured on the 2019-2026 ledger: 5 PAYEMS values, 1 CPIAUCSL value and 2 CPILFESL values were
    published on two or more different dates.  The pair below is real - 158,432 was May 2024's payrolls
    on 2024-07-05 and, after the 2026 benchmark revision, December 2025's on 2026-03-06.  A bar in July
    2024 carrying it is CORRECT, and a checker that dated each value by its LATEST release would call
    that a leak 20 months early and send somebody hunting a bug that is not there.

    This is also the fourth independent reason UNRATE is refused: 27 of its 39 distinct values recur
    across releases, so on that series this check is nearly blind.
    """
    both = (
        ("2024-05-01", "2024-06-07", "158543"),
        ("2024-05-01", "2024-07-05", "158432"),
        ("2025-12-01", "2026-01-09", "159526"),
        ("2025-12-01", "2026-02-11", "158497"),
        ("2025-12-01", "2026-03-06", "158432"),
    )
    bars = _bars("2024-07-10", 4)
    aligned = pd.Series(158432.0, index=bars, name=COLUMN)

    assert revision_leak(aligned, _ledger(both), COLUMN, interval_ms=HOUR_MS).clean
    # And the guarantee still bites where nothing published the number that early.
    assert not revision_leak(aligned, _ledger(both[2:]), COLUMN, interval_ms=HOUR_MS).clean


def test_the_checker_would_pass_a_frame_that_simply_carried_todays_values_only_if_it_ran_late() -> None:
    """The limit of the check, pinned so the guarantee is not overread.

    `revision_leak` asks one question - could this number have been read yet - and on bars AFTER the
    last revision, today's values and the point-in-time values are both readable, so a naive frame
    passes there.  It is the early bars that catch it, which is why the check runs over the whole
    sample and not over a window someone picked.
    """
    ledger = _ledger()
    late_bars = _bars("2026-03-01", 6)
    naive = pd.Series(157032.0, index=late_bars, name=COLUMN)

    assert revision_leak(naive, ledger, COLUMN, interval_ms=HOUR_MS).clean
    early = pd.Series(157032.0, index=_bars("2024-02-05", 6), name=COLUMN)
    assert not revision_leak(early, ledger, COLUMN, interval_ms=HOUR_MS).clean


# --------------------------------------------------------------------------------------------------
# The release-day boundary, and the eight hours a day-open reading would have taken
# --------------------------------------------------------------------------------------------------


def test_the_january_print_becomes_readable_two_whole_days_after_the_date_alfred_stamps_it() -> None:
    """Note 5.  `realtime_start` is a DATE with no time and no zone, so only a whole-day boundary is safe.

    The last bar that cannot see the print closes at 2024-02-03T23:00Z; the first that can closes at
    2024-02-04T00:00Z.  A reader keying on the raw date would have handed it to a bar 48 hours earlier,
    which is asserted here rather than described because 48 hours is the size of the mistake.
    """
    bars = _bars("2024-02-01", 96)

    aligned = align_releases_to_bars(_ledger(), bars, COLUMN, interval_ms=HOUR_MS)
    carrying = aligned[aligned == 157700.0]

    assert carrying.index[0] == pd.Timestamp("2024-02-03 23:00", tz="UTC")
    assert aligned.loc[pd.Timestamp("2024-02-03 22:00", tz="UTC")] == 157232.0  # still December's print
    naive_open = pd.Timestamp("2024-02-02", tz="UTC")
    assert (carrying.index[0] + pd.Timedelta(hours=1) - naive_open) == pd.Timedelta(days=2)
    assert RELEASE_AVAILABLE_LAG_MS == 2 * DAY_MS


# --------------------------------------------------------------------------------------------------
# Publication frequency against bar frequency: the forward hold, and its bound
# --------------------------------------------------------------------------------------------------


def test_between_two_prints_the_value_is_held_forward_and_before_the_first_it_is_nan() -> None:
    """The frequency choice, stated as behaviour: a step function in RELEASE time, NaN before it starts.

    Not zero and not backfilled.  The hold is defensible where a price ffill is not - between two
    payroll prints the market's information set really does contain the last print - and the step is
    at the release, so the plateau boundaries are release dates rather than month ends.
    """
    bars = _bars("2023-12-15", 60 * 24)  # 60 days of hourly bars across two prints

    aligned = align_releases_to_bars(_ledger(), bars, COLUMN, interval_ms=HOUR_MS)

    # The boundary is `available <= bar close`, so the bar OPENING at 22:00 on 2024-01-06 is the last
    # one that cannot see the print and the 23:00 bar - closing exactly at the availability instant -
    # is the first that can.  Asserted at the bar rather than at the day, because an off-by-one here is
    # an hour of look-ahead that no aggregate would show.
    assert aligned.loc[: pd.Timestamp("2024-01-06 22:00", tz="UTC")].isna().all(), "nothing published yet"
    assert aligned.loc[pd.Timestamp("2024-01-06 23:00", tz="UTC")] == 157232.0
    plateaus = aligned.dropna().groupby(aligned.dropna()).size()
    assert set(plateaus.index) == {157232.0, 157700.0}
    # 672 consecutive hourly bars - 28 whole days - reading one number, and the next plateau runs to the
    # end of the window.  Pinned exactly rather than bounded, because the honest size of holding a
    # monthly release across an hourly grid is the fact this test is here to state.
    assert plateaus[157232.0] == 672 and plateaus[157700.0] == 217
    assert not (aligned.dropna() == 0).any()


def test_a_publisher_that_goes_quiet_longer_than_the_cap_blanks_the_column_instead_of_repeating() -> None:
    """The bound on the forward hold, sized against a real 76-day silence rather than a round number.

    The 2025 shutdown left PAYEMS unpublished from 2025-09-05 to 2025-11-20 while the numbers stayed
    correct, so the default cap has to survive it - and a cap tight enough to blank it is asserted to
    blank it, because a bound nobody can see fire is a bound nobody has tested.
    """
    ledger = _ledger(SHUTDOWN_VINTAGES)
    during = _bars("2025-11-19", 1)

    assert align_releases_to_bars(ledger, during, COLUMN, interval_ms=HOUR_MS).iloc[0] == 159540.0
    tight = align_releases_to_bars(ledger, during, COLUMN, interval_ms=HOUR_MS, max_staleness_ms=60 * DAY_MS)
    assert np.isnan(tight.iloc[0])
    assert MAX_STALENESS_MS > 76 * DAY_MS, "the cap must clear the largest measured silence"


def test_a_revision_to_the_newest_month_moves_the_head_and_a_revision_to_an_older_one_does_not() -> None:
    """The two halves of "what a bar reads is the current published LEVEL", which is one `>=`.

    February 2024 was the newest reference month from 2024-03-10 onward and was restated twice while it
    held that position - 157,830 on 2024-04-05 and 157,796 on 2024-05-03 - so a bar after each must
    move.  The same 2024-04-05 release also restated JANUARY to 157,560, and no bar reads that: a
    revision to a month the series has already moved past changes the history, not the level.  A `>`
    in place of the `>=` passes every other test in this file and fails here.
    """
    ledger = _ledger()

    april = align_releases_to_bars(ledger, _bars("2024-04-08", 2), COLUMN, interval_ms=HOUR_MS)
    may = align_releases_to_bars(ledger, _bars("2024-05-06", 2), COLUMN, interval_ms=HOUR_MS)

    assert set(april) == {157830.0} and set(may) == {157796.0}
    # January's own restatement is on record, and is not what any bar reads.
    assert 157560.0 in set(revision_history(ledger, ms("2024-01-01"), COLUMN)[COLUMN])
    assert 157560.0 not in set(april) | set(may)


def test_two_reference_months_published_on_the_same_day_leave_the_later_one_at_the_head() -> None:
    """The shutdown's second surprise, and the one an aligner assuming one release per month gets wrong.

    October and November 2025 payrolls BOTH first appeared on 2025-12-16.  What a bar reads afterwards
    is November's 159,552, not October's 159,488 - the head moves to the latest reference period, and
    the two arrived in one instant so nothing but the reference date can order them.
    """
    ledger = _ledger(SHUTDOWN_VINTAGES)

    aligned = align_releases_to_bars(ledger, _bars("2025-12-19", 4), COLUMN, interval_ms=HOUR_MS)

    assert set(aligned) == {159552.0}
    assert 159488.0 not in set(aligned)
    # And a revision to an OLDER month does not move the head: August was restated the same day.
    assert 159485.0 not in set(aligned)


# --------------------------------------------------------------------------------------------------
# Shape: one series for the whole market, not one column per symbol
# --------------------------------------------------------------------------------------------------


def test_the_family_frame_is_bars_by_series_and_keeps_its_width_when_a_series_is_missing() -> None:
    bars = _bars("2024-02-05", 4)

    frame = align_family_to_bars({COLUMN: _ledger()}, bars, interval_ms=HOUR_MS)

    assert list(frame.columns) == list(MACRO_COLUMNS)
    assert frame[COLUMN].tolist() == [157700.0] * 4
    assert frame.drop(columns=[COLUMN]).isna().all().all(), "an absent series is NaN, not an absent column"


def test_broadcasting_a_market_wide_series_across_symbols_is_cross_sectionally_empty_by_construction() -> None:
    """Why `broadcast_to_symbols` names the copy: a rank or a demean over it carries no information.

    Asserted rather than only documented, because "the leaf scored zero" is a much more expensive way
    to learn this than a test - and it is a property of the data, so no implementation can fix it.
    """
    bars = _bars("2024-02-05", 4)
    frame = align_family_to_bars({COLUMN: _ledger()}, bars, interval_ms=HOUR_MS)

    wide = broadcast_to_symbols(frame, ("BTCUSDT", "ETHUSDT", "SOLUSDT"))[COLUMN]

    assert list(wide.columns) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert bool((wide.nunique(axis=1) == 1).all()), "every symbol carries the identical series"
    demeaned = wide.sub(wide.mean(axis=1), axis=0)
    assert bool((demeaned == 0.0).all().all()), "a cross-sectional demean of a market-wide series is zero"


# --------------------------------------------------------------------------------------------------
# RISK-G3: the fifth refusal, and the gap it leaves in alignment's own gate
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verification", "leak", "expected"),
    [
        (None, "clean", "no verification on record"),
        (Verification(UNVERIFIABLE, "x", 0, 0, (), (COLUMN,)), "clean", "UNVERIFIABLE"),
        (Verification(PASS, "ok", 30, 30, (), ()), "clean", "never compared"),
        (Verification(PASS, "ok", 30, 30, (), (COLUMN,)), None, "no point-in-time evidence"),
        (Verification(PASS, "ok", 30, 30, (), (COLUMN,)), "dirty", "days early"),
    ],
)
def test_five_refusals_stand_between_a_macro_column_and_a_live_signal(
    verification: Verification | None, leak: str | None, expected: str
) -> None:
    ledger, bars = _ledger(), _bars("2024-02-05", 24)
    aligned = align_releases_to_bars(ledger, bars, COLUMN, interval_ms=HOUR_MS)
    if leak == "dirty":
        aligned.iloc[0] = 157032.0
    report = None if leak is None else revision_leak(aligned, ledger, COLUMN, interval_ms=HOUR_MS)

    admitted, reason = admits_live_signal(COLUMN, verification, report)

    assert admitted is False
    assert expected in reason and "RISK-G3" in reason


def test_a_verified_and_leak_free_column_is_admitted_and_carries_both_reasons() -> None:
    ledger, bars = _ledger(), _bars("2024-02-05", 24)
    aligned = align_releases_to_bars(ledger, bars, COLUMN, interval_ms=HOUR_MS)
    verification = Verification(PASS, "32/32 at the declared offset", 32, 32, (), (COLUMN,))

    admitted, reason = admits_live_signal(
        COLUMN, verification, revision_leak(aligned, ledger, COLUMN, interval_ms=HOUR_MS)
    )

    assert admitted is True
    assert "32/32" in reason and "none published later than its bar" in reason


def test_alignments_gate_is_weaker_than_this_one_and_only_in_one_direction() -> None:
    """The cost of registering in alignment's table, pinned instead of hidden.

    `alignment.admits_live_signal` knows the four refusals it was written with and cannot know about a
    value that was rewritten later, so on a leaking frame it says yes where this module says no.  The
    reverse never happens - this one starts by calling that one.  The edit that closes the gap is an
    `extra_refusals` hook on alignment's function, which is a change to a module this task uses rather
    than rewrites; until then the rule is that a macro column is gated HERE.
    """
    ledger, bars = _ledger(), _bars("2024-02-05", 24)
    leaking = align_releases_to_bars(ledger, bars, COLUMN, interval_ms=HOUR_MS)
    leaking.iloc[0] = 157032.0
    verification = Verification(PASS, "32/32", 32, 32, (), (COLUMN,))
    report = revision_leak(leaking, ledger, COLUMN, interval_ms=HOUR_MS)

    assert alignment_admits_live_signal(COLUMN, verification)[0] is True
    assert admits_live_signal(COLUMN, verification, report)[0] is False
    # Never the other way: every refusal alignment makes is a refusal here.
    assert admits_live_signal(COLUMN, None, report)[0] is False
