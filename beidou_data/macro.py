"""US monthly macro releases under a POINT-IN-TIME contract (#32), which is the only reason to have them.

#32 sat as "挂起，等一次操作者动作" because the hard half of a macro column is not downloading it - FRED
serves it in one request - but knowing WHEN each number came into existence.  The operator has since
registered a key, and the free tier serves every vintage, so the half that was missing is now the half
this module is built around.

The one measurement that justifies the whole column, reproduced here on 2026-09-09 against the live
endpoint - January 2024 nonfarm payrolls, `PAYEMS`, reference period 2024-01-01:

    written 2024-02-02   157,700   <- the only value that existed for the next 35 days
    written 2024-03-08   157,533
    written 2024-04-05   157,560
    written 2025-02-07   157,049
    written 2026-02-11   157,032   <- what a query today returns, and it is 668,000 lower

A backtest that reads today's value on a bar in February 2024 is reading a number that would not be
written for another 739 days.  Nothing reports this: the series has one name, one reference date, and
one value per query, so the defect is a QUERY CONVENTION rather than a bug - the same shape as DL-D2's
five-minute stamp, one source further out, and larger.

Everything below was measured against api.stlouisfed.org and api.bls.gov on 2026-09-09.  The order is
the one DL-D2 forces: contract first, downloader second.

**1.  THREE THINGS HAVE TO BE DISTINGUISHED, AND THE EXISTING CONTRACT EXPRESSES TWO.**  A macro cell
has a REFERENCE PERIOD (which month the number describes), a FIRST-RELEASE INSTANT (when it could
first be read) and a REVISION HISTORY (what it was later changed to, and when).  `EventTimeContract`
carries the first two - `archive`/`rest` stamps name the reference period, `available_offset_ms` names
the earliest availability - and has no place for the third, because for every column before this one
the value at a stamp never changed.  The third is the RELEASE LEDGER that `parse_release_ledger`
builds: one row per RELEASE EVENT rather than per observation, so a revision is a new row and not an
overwrite.  That is the whole structural difference between this feed and the four before it.

**2.  THE AVAILABILITY LAG IS NOT A CONSTANT, SO IT CANNOT LIVE IN `available_offset_ms`.**  Measured
over 2019-01..2026-08, reference-period open to first release: PAYEMS and UNRATE 31/34/80 days
(min/median/max), CPIAUCSL and CPILFESL 37/41/78, INDPRO 42/45/93.  A single arithmetic offset would
have to be at least 80 days to be safe, which would blank the two most recent months on every bar
forever.  So `available_offset_ms` declares only what must be TRUE - the reference month has ENDED -
and the ACTUAL availability is carried per release, read from ALFRED's own `realtime_start`.  The
contract field becomes a floor the data is checked against rather than the number the aligner uses;
`test_every_release_lands_after_the_contracts_arithmetic_floor` is that check, and it passes with zero
margin on PAYEMS, which is correct: a 31-day month released on the 32nd day is exactly one day after
the month it describes ended.

**3.  `period_ms` IS 31 DAYS, AND THAT CHOICE IS WHAT MAKES THE RIVALS TESTABLE AT ALL.**  A calendar
month is not a constant number of milliseconds, and `verify_stamp_offset` proposes its rivals as
constant ms offsets of +-`period_ms`.  Shifting a month-open stamp by 31 days lands on another
month-open exactly when the month has 31 days - January, March, May, July, August, October, December -
so the rival is tested on 7 months in 12 and is simply absent for the other 5.  Measured, not reasoned:
against the BLS witness over 2024-01..2026-08 the +-31-day rivals drew 16-19 comparisons out of 30-32
declared-offset comparisons.  Declaring 30 days, or the 30.44-day average, would land BETWEEN month
opens for every month, draw zero comparisons, and `verify_stamp_offset` would answer UNVERIFIABLE for
the honest reason that nothing had been refuted.  A partial rival that fires is worth more than a
complete one that cannot.

**4.  THE WITNESS IS THE ORIGINAL PUBLISHER.**  FRED redistributes BLS's numbers, so "does FRED's
`date` name the month the data describes, or the month it was published?" cannot be answered from FRED
alone.  api.bls.gov's public v1 API needs no key and serves the same four series under their BLS ids,
stamped `(year, period)` - a LABEL rather than a timestamp, which is the point: BLS never converts to
an instant, so there is no second convention to collide with FRED's.  Measured 2026-09-09 at the
declared offset of zero against the +-31-day rivals:

    macro_nonfarm_payrolls  32/32   vs 0/18 and 0/19
    macro_cpi_headline      30/30   vs 0/16 and 0/17
    macro_cpi_core          30/30   vs 0/16 and 0/17
    UNRATE (refused, note 6) 31/31  vs 6/17 and 6/18

BLS v1 serves roughly the trailing three years, which is why the comparison counts are 30-32 against
90-92 FRED observations.  That is a smaller sample than DL-D5's 744 bars and it does not need to be
larger: what carries the evidence is the rivals being refuted, and one wrong month disagrees by more
than any tolerance.

**5.  AVAILABILITY IS ROUNDED UP TO TWO WHOLE UTC DAYS, BECAUSE ALFRED'S STAMP IS A DATE.**
`realtime_start` is a calendar date with no time and no stated zone, and the St. Louis Fed is in US
Central while BLS releases on Eastern.  The latest instant a US calendar date can still be running is
23:59:59 US/Pacific, i.e. 07:59:59 UTC the following day, so the smallest WHOLE UTC-day boundary that
no instant on that date can cross is two days after the date's UTC open - one day would be unsafe by
up to eight hours.  This is `metrics.usable_from_ms`'s rule (the boundary is what must be TRUE, never
an observed latency) applied to a stamp whose resolution is coarser than the bar.  It costs nothing
here: the series steps once a month, so two days of extra lag changes the value a bar reads on 2 days
in ~31, and only for bars that would otherwise be reading a number published hours earlier.

**6.  THE FAMILY IS PRE-REGISTERED AND UNRATE IS REFUSED, ON FOUR MEASUREMENTS THAT AGREE.**  The
selection rule and its reasoning are in `docs/RESEARCH_LOG.md` under this date; the short version is
that the family is the two BLS monthly releases the Fed's dual mandate names, and a series earns a
place only if the point-in-time treatment CHANGES what a signal would read.  UNRATE does not: 33 of 92
observations were ever revised against PAYEMS's 91 of 92; the revisions never change the SIGN of the
month-over-month move (0 of 58 flips, against PAYEMS's 8 of 90); its rivals are only 65% refuted (6/17,
6/18) because the unemployment rate takes about twenty distinct values in seven years and a one-month
shift therefore agrees by coincidence; and for the same reason 27 of those 39 values recur across
releases, which leaves `revision_leak` nearly blind on it.  Four independent readings of "this series
carries the least of what this column is for", and the last two are the same low cardinality defeating
two different checks.  Named in `REFUSED_SERIES` rather than omitted, so the next author finds the
refusal and its numbers instead of an empty space.

**7.  THE DAILY SERIES ARE NOT MERELY WEAKER, THEY ARE REFUSED BY THE ENDPOINT.**  `DFF`, `DGS10`,
`DGS2`, `T10Y2Y`, `VIXCLS` and `RRPONTSYD` answer HTTP 400 to a full-vintage query - "There are 5119
vintage dates in the specified real-time period ... This exceeds the [maximum]" - because a daily
series accrues a vintage every business day.  A point-in-time daily rate therefore costs one request
per as-of date rather than one per series, which is a different downloader and a different budget.
`WALCL` is the opposite failure: 88 observations, 88 rows, zero revisions, so its point-in-time column
is trivially equal to its naive one and this whole module buys nothing for it.  Both are recorded
because "we did not think of it" and "we measured it and it lost" are different states.

**8.  MISSING IS MISSING, AND BETWEEN RELEASES IS NOT MISSING.**  See `align_releases_to_bars`: the
value is held forward in RELEASE time, because a macro level genuinely is a step function in the
market's information set, and that is an argument no price column can make.  The forward hold is
bounded - see `MAX_STALENESS_MS` and the 76-day gap that sized it.

**9.  THE KEY IS READ FROM THE ENVIRONMENT AND ITS ABSENCE IS LOUD.**  There is no fallback to a
keyless query, because the keyless query returns TODAY's values and would silently turn this module
into the defect it exists to prevent.  See `api_key`, and `_get`'s redaction: FRED authenticates by
query parameter only, so an unredacted `httpx` error message would carry the key into a traceback.

**10.  RUN AGAINST THE LIVE ENDPOINTS AFTER THE SUITE WAS GREEN**, which is this repository's habit
since `onchain.covered_assets` was found reading one catalog page of the three it needed.  Nothing
here broke, and the numbers are worth more than that reassurance.  Over 67,200 hourly bars
(2019-01-01..2026-08-31), with all three contracts PASS against the live BLS witness:

    macro_nonfarm_payrolls  66,409 bars carry a value, 0 leak.  The naive frame - today's value for
                            every reference month - leaks 67,200 of 67,200, and disagrees with the
                            point-in-time frame on 66,409 of 66,409 comparable bars: 100.0%, median
                            gap 0.291%, max 0.780%.
    macro_cpi_headline      92.8% of bars differ, median 0.051%, max 0.257%.
    macro_cpi_core          91.5% of bars differ, median 0.038%, max 0.183%.

One hundred percent of bars wrong is the same shape DL-D2 found when the metrics stamps were joined
naively, and it is the reason this feed is a contract before it is a downloader.  The gaps are small
in level terms and that is the trap rather than the consolation: a signal reads the CHANGE, and 8 of
90 monthly changes have the opposite sign under revision (note 6).

The NaN counts are the head of the window and nothing else - 791 bars for payrolls and 1,079 for the
CPI pair, which is exactly the wait from 2019-01-01 to each series' first release becoming available.
No bar in the interior is blanked, so the 120-day staleness cap never fires on real data; it fires in
`test_a_publisher_that_goes_quiet_longer_than_the_cap_blanks_the_column_instead_of_repeating` and
nowhere else, which is where a bound that has never been exercised belongs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
import pandas as pd

from beidou_data.alignment import (
    CONTRACTS,
    EventTimeContract,
    Stamp,
    Verification,
    verify_stamp_offset,
)
from beidou_data.alignment import (
    admits_live_signal as alignment_admits_live_signal,
)

FRED_BASE_URL = "https://api.stlouisfed.org"
BLS_BASE_URL = "https://api.bls.gov"
API_KEY_ENV = "BEIDOU_FRED_API_KEY"

DAY_MS = 86_400_000
# Note 3: the LONGEST calendar month.  Not an approximation of a month - the specific number that makes
# a +-1-period rival land on a real month open for the 31-day months instead of between opens for all.
MONTH_MS = 31 * DAY_MS
# Note 5.  Two whole UTC days after the release DATE's UTC open, because the date has no time and no zone.
RELEASE_AVAILABLE_LAG_MS = 2 * DAY_MS

# Note 8.  Above the largest release-to-release silence the sample contains - 76 days for PAYEMS across
# the 2025 US government shutdown (2025-09-05 -> 2025-11-20), 55 days for CPIAUCSL - with enough margin
# that a second shutdown of the same length does not blank a correctly published series, and far below
# the "this series was discontinued and nobody noticed" case the cap exists for.
MAX_STALENESS_MS = 120 * DAY_MS

_RETRIES = 4  # 1s, 2s, 4s, matching `onchain.CommunityClient`; both upstreams throttle rather than fail

# The ledger's own columns.  Underscored names would collide with nothing, but these are stored beside a
# VALUE column whose name is the panel column, so they are spelled out once here and never inlined.
REFERENCE_OPEN = "reference_open_ms"
RELEASED = "released_ms"
AVAILABLE = "available_ms"
LEDGER_COLUMNS: tuple[str, ...] = (REFERENCE_OPEN, RELEASED, AVAILABLE)


class FredApiKeyMissing(RuntimeError):
    """Note 9.  There is no keyless fallback, because the keyless answer is the look-ahead."""


class FredRequestFailed(RuntimeError):
    """An upstream failure carrying the STATUS and the path, never the URL - the URL holds the key."""


@dataclass(frozen=True)
class MacroSeries:
    """One pre-registered series: its two publishers, the column it becomes, and why it was chosen.

    `bls_id` is not optional and that is deliberate.  A series with no independent publisher cannot
    refute the reference-period stamp (note 4), so under this module's own rule it could never be
    verified and therefore could never reach live - and a field that is allowed to be `None` is an
    invitation to add one anyway and discover the refusal much later.
    """

    column: str
    fred_id: str
    bls_id: str
    release: str
    why: str


# Note 6.  The family, written before any of it was aligned to a bar.  Two releases, three columns.
MACRO_SERIES: tuple[MacroSeries, ...] = (
    MacroSeries(
        column="macro_nonfarm_payrolls",
        fred_id="PAYEMS",
        bls_id="CES0000000001",
        release="Employment Situation",
        why=(
            "the labour quantity the dual mandate names, and the most revised series in the family: "
            "91 of 92 observations differ between first print and today, and 8 of 90 month-over-month "
            "changes flip SIGN under revision"
        ),
    ),
    MacroSeries(
        column="macro_cpi_headline",
        fred_id="CPIAUCSL",
        bls_id="CUSR0000SA0",
        release="Consumer Price Index",
        why=(
            "the price level the dual mandate names; seasonally adjusted, so the annual seasonal-factor "
            "revision rewrites past values - 83 of 91 observations differ from their first print and "
            "3 of 88 month-over-month changes flip sign"
        ),
    ),
    MacroSeries(
        column="macro_cpi_core",
        fred_id="CPILFESL",
        bls_id="CUSR0000SA0L1E",
        release="Consumer Price Index",
        why=(
            "the same release with food and energy removed, which is the number the policy path is "
            "actually set against; 82 of 91 observations differ from their first print"
        ),
    ),
)

MACRO_COLUMNS: tuple[str, ...] = tuple(series.column for series in MACRO_SERIES)
BY_COLUMN: dict[str, MacroSeries] = {series.column: series for series in MACRO_SERIES}

# Notes 6 and 7.  Considered, measured, and lost - kept so the next author reads the refusal and its
# number rather than adding the series back by analogy.
REFUSED_SERIES: dict[str, str] = {
    "UNRATE": (
        "revised on only 33 of 92 observations, 0 of 58 month-over-month sign flips, rivals refuted at "
        "just 6/17 and 6/18 because the rate takes ~20 distinct values in seven years, and 27 of its "
        "39 distinct values recur across releases so `revision_leak` is nearly blind on it"
    ),
    "INDPRO": (
        "the largest revisions in the candidate set (median 2.76%, max 8.67% at 2020-04) but a "
        "different mechanism - industrial activity, not the policy path - so it belongs to a SECOND "
        "family and one family is what was pre-registered"
    ),
    "WALCL": "88 observations, 88 vintage rows, zero revisions: point-in-time is trivially the naive read",
    "DFF/DGS2/DGS10/T10Y2Y/VIXCLS/RRPONTSYD": (
        "daily, so a full-vintage query is refused by the endpoint itself with HTTP 400 - 2597 to 5119 "
        "vintage dates exceed the maximum - and a point-in-time daily series costs one request per "
        "as-of date, which is a different downloader"
    ),
}


MACRO = EventTimeContract(
    name="macro",
    period_ms=MONTH_MS,
    # Both stamps name the reference period's OPEN and both offsets are zero, so `stamp_offset_ms` is
    # zero and the naive join is correct here - which is exactly why the rivals carry the evidence
    # rather than the match does (DL-D5 reached the same place from the other direction).
    archive=Stamp("date", 0, "the reference period OPEN (FRED/ALFRED)"),
    rest=Stamp("period_open", 0, "the reference period OPEN (BLS `(year, period)` rendered to it)"),
    # Note 2.  A FLOOR - the reference month has ended - and not the number the aligner uses.  31 days
    # is a safe floor for every month: it is exact for a 31-day month and conservative for shorter ones.
    available_offset_ms=MONTH_MS,
    measured=(
        "2026-09-09 against api.stlouisfed.org and api.bls.gov: at a stamp offset of 0, PAYEMS 32/32, "
        "CPIAUCSL 30/30 and CPILFESL 30/30 against BLS's own publication, with the +-31-day rivals "
        "refuted 0/18 and 0/19, 0/16 and 0/17, 0/16 and 0/17.  The +-31-day rival draws comparisons "
        "only on the seven 31-day months, which is why period_ms is 31 days (note 3).  Reference-open "
        "to first release ran 31/34/80 days for PAYEMS and 37/41/78 for the CPI pair over 2019-2026, "
        "which is why availability is carried per release and not by this contract's offset (note 2)."
    ),
)

# Registered into alignment's own table from THIS module, the way `index_price` does, and the direction
# of failure is the argument: a column absent from `CONTRACTS` is refused by `contract_for`, so
# forgetting this line can only ever make the gate stricter.  `beidou_data.onchain` keeps a SEPARATE
# registry instead - it was written when the union test asserted `set(CONTRACTS) == set(VALUE_COLUMNS)`
# and joining would have weakened that guard.  The union has since grown to three feeds, so the reason
# has expired and the two feeds now disagree about where a contract lives; see this module's report.
CONTRACTS.update(dict.fromkeys(MACRO_COLUMNS, MACRO))


def api_key(env: Mapping[str, str] | None = None) -> str:
    """The ALFRED key from the environment, or a loud refusal.  Note 9.

    Loud rather than degraded, and the difference matters more here than anywhere else in this package:
    the same endpoint answers WITHOUT a key for the current-vintage query, so a fallback would return a
    complete, plausible, correctly shaped frame in which every value is the one written today.  That is
    precisely the defect this module exists to prevent, delivered by the module itself.

    The value is returned and never logged; `_get` puts it in a query parameter because FRED offers no
    header auth, and redacts it back out of any error that would otherwise carry the URL.
    """
    value = (os.environ if env is None else env).get(API_KEY_ENV, "").strip()
    if not value:
        raise FredApiKeyMissing(
            f"{API_KEY_ENV} is not set; ALFRED's vintage query needs a key and there is no keyless "
            "fallback, because the keyless answer is today's revised values (beidou_data.macro note 9). "
            f"Load it the way deploy/run_live.sh does: eval \"$(grep -E '^export {API_KEY_ENV}=' "
            '"$HOME/.zshrc")"'
        )
    return value


def month_open_ms(year: int, month: int) -> int:
    """A `(year, month)` label rendered to the UTC instant the month opens.

    Kept here rather than inlined at the two call sites because it IS the conversion the contract
    declares for the BLS side: BLS publishes a label, this function is the whole of what turns it into
    a stamp, and note 4's comparison is only meaningful if FRED's date and this agree by construction
    on nothing except the calendar.
    """
    return int(pd.Timestamp(year=year, month=month, day=1, tz="UTC").timestamp() * 1000)


def _date_open_ms(date: str) -> int:
    return int(pd.Timestamp(date[:10], tz="UTC").timestamp() * 1000)


def empty_ledger(column: str) -> pd.DataFrame:
    """A ledger with the right columns and no rows, so an absence has the same shape as a presence."""
    frame = pd.DataFrame({name: pd.Series(dtype="int64") for name in LEDGER_COLUMNS})
    frame[column] = pd.Series(dtype=float)
    return frame


def parse_release_ledger(payload: Mapping[str, Any], column: str) -> pd.DataFrame:
    """ALFRED's full-vintage observations as one row per RELEASE EVENT.  Note 1.

    The request that produces this payload asks for `realtime_start=1776-07-04&realtime_end=9999-12-31`,
    which makes FRED return every distinct value a `date` ever had together with the window it was
    current for.  Parsing it into a row per release rather than per observation is the structural
    decision of this module: a revision becomes a NEW ROW, so the history is additive and a question
    like "what did this look like on 2024-02-05" is a filter rather than a re-download.

    `realtime_end` is deliberately DROPPED.  It is the day before the next release's `realtime_start`,
    so carrying it would store the same fact twice and give the two copies somewhere to disagree; every
    consumer here derives "which value was in force" from `available_ms` ordering instead, which is the
    only ordering that also survives a ledger assembled from two downloads.

    A value of "." is FRED's missing marker and the row is DROPPED, not carried as NaN: a release that
    published nothing is not a release, and keeping it would let `align_releases_to_bars` treat a
    non-publication as a fresh print and reset the staleness clock.
    """
    rows = [row for row in (payload.get("observations") or []) if str(row.get("value", ".")) not in (".", "")]
    if not rows:
        return empty_ledger(column)
    released = np.array([_date_open_ms(str(row["realtime_start"])) for row in rows], dtype="int64")
    frame = pd.DataFrame(
        {
            REFERENCE_OPEN: np.array([_date_open_ms(str(row["date"])) for row in rows], dtype="int64"),
            RELEASED: released,
            # Note 5.  The rounding lives here, at the one point a date becomes an instant, so no caller
            # can accidentally key on the raw date and take up to eight hours of look-ahead.
            AVAILABLE: released + RELEASE_AVAILABLE_LAG_MS,
            column: np.array([float(row["value"]) for row in rows], dtype=float),
        }
    )
    return frame.sort_values([AVAILABLE, REFERENCE_OPEN], ignore_index=True)


def parse_bls_series(payload: Mapping[str, Any], column: str) -> pd.DataFrame:
    """The BLS witness in the shape `verify_stamp_offset` wants for the `rest` side.  Note 4.

    The value lands under the CANONICAL column name because `_agreement` compares columns present in
    both frames by name; under the vendor's own name it would compare nothing and report a perfect
    score over an empty intersection.  `alignment.Verification` grew `compared_columns` because that
    hole was found in production one feed earlier, and this is the shape that avoids re-opening it.

    BLS spells missing as "-", annual averages as period "M13", and quarterly rows as "Q..".  Only
    monthly rows with a real value survive, because a month is what the contract's stamp names.
    """
    series = (payload.get("Results") or {}).get("series") or []
    rows = [
        row
        for row in (series[0].get("data") or [] if series else [])
        if str(row.get("period", "")).startswith("M")
        and str(row.get("period", "")) != "M13"
        and str(row.get("value", "-")) not in ("-", "", ".")
    ]
    if not rows:
        return pd.DataFrame({MACRO.rest.column: pd.Series(dtype="int64"), column: pd.Series(dtype=float)})
    frame = pd.DataFrame(
        {
            MACRO.rest.column: np.array(
                [month_open_ms(int(row["year"]), int(str(row["period"])[1:])) for row in rows], dtype="int64"
            ),
            column: np.array([float(row["value"]) for row in rows], dtype=float),
        }
    )
    return frame.sort_values(MACRO.rest.column, ignore_index=True)


def latest_vintage_frame(ledger: pd.DataFrame, column: str) -> pd.DataFrame:
    """The ledger collapsed to one row per reference period, holding the value CURRENT TODAY.

    This is the frame the contract check compares against BLS, and it is the naive frame - the one a
    keyless FRED query would have returned - which is the right thing to compare: BLS also publishes
    only its current vintage, so the two sides are like for like and the check is about the STAMP.
    The point-in-time question is a different question and `revision_leak` is where it is asked.
    """
    if ledger.empty:
        return pd.DataFrame({MACRO.archive.column: pd.Series(dtype="int64"), column: pd.Series(dtype=float)})
    newest = ledger.sort_values(AVAILABLE).drop_duplicates(REFERENCE_OPEN, keep="last")
    return pd.DataFrame(
        {
            MACRO.archive.column: newest[REFERENCE_OPEN].to_numpy(dtype="int64"),
            column: newest[column].to_numpy(dtype=float),
        }
    ).sort_values(MACRO.archive.column, ignore_index=True)


def verify_macro_contract(
    ledger: pd.DataFrame, witness: pd.DataFrame, column: str, *, tolerance: float = 1e-9, min_overlap: int = 12
) -> Verification:
    """Hold FRED's reference-period stamp against BLS's own, and against the two it must refute.

    `tolerance` is relative and looser than `alignment`'s 1e-12 default for a reason that is measured
    rather than preferred: these two publishers round for PRESENTATION at different places - CPIAUCSL
    is served to three decimals by both, but a rounded index at 332.813 has a last-digit weight of
    3.0e-6 relative, five orders above float noise.  1e-9 is below any rounding either side does today
    and far below the smallest real monthly move (~0.05%), and the check still returned exact agreement
    at every one of the 30-32 comparisons, so nothing was bought with the slack.
    """
    return verify_stamp_offset(
        MACRO,
        latest_vintage_frame(ledger, column),
        witness,
        value_columns=[column],
        tolerance=tolerance,
        min_overlap=min_overlap,
    )


def revision_history(ledger: pd.DataFrame, reference_open_ms: int, column: str) -> pd.DataFrame:
    """Every value one reference period ever had, in release order.  The third column of note 1.

    The plainest possible view, and it exists as a function because the alternative - a reader
    filtering the ledger by hand - is where somebody sorts by `reference_open_ms` instead of by
    availability and reads a revision history in the wrong order.
    """
    rows = ledger[ledger[REFERENCE_OPEN] == int(reference_open_ms)]
    return rows.sort_values(AVAILABLE, ignore_index=True)[[RELEASED, AVAILABLE, column]]


def as_of(ledger: pd.DataFrame, instant_ms: int, column: str) -> pd.DataFrame:
    """The whole series as it stood at one instant: one row per reference period, latest value THEN.

    The general primitive, and the one that makes the point-in-time claim checkable by hand - it is
    what reproduces the operator's table, and `align_releases_to_bars` is only its head.  A reference
    period with no release available yet is ABSENT rather than NaN, because "this month had not been
    published" and "this month was published as nothing" are different facts and only the first one is
    true here.
    """
    visible = ledger[ledger[AVAILABLE] <= int(instant_ms)]
    if visible.empty:
        return pd.DataFrame({REFERENCE_OPEN: pd.Series(dtype="int64"), column: pd.Series(dtype=float)})
    newest = visible.sort_values(AVAILABLE).drop_duplicates(REFERENCE_OPEN, keep="last")
    return newest.sort_values(REFERENCE_OPEN, ignore_index=True)[[REFERENCE_OPEN, column]]


def align_releases_to_bars(
    ledger: pd.DataFrame,
    bars: pd.DatetimeIndex,
    column: str,
    *,
    interval_ms: int,
    max_staleness_ms: int = MAX_STALENESS_MS,
) -> pd.Series:
    """For each bar, the latest reference period PUBLISHED by the bar's close; NaN before the first.

    Note 8, and the frequency question this feed is obliged to answer out loud: the publication rate is
    monthly and the bar is hourly, so roughly 730 consecutive bars read one number.  Holding it forward
    is a CHOICE, and the argument for it is one no price column can make - between two payroll prints
    the market's information set genuinely contains the last print and nothing newer, so the forward
    hold is the true state rather than an invented quote.  `spot.py` note 7 refuses to fill for the
    opposite reason and both are right: filling a PRICE invents a trade that did not happen, while
    filling a RELEASE states a fact that remained true.

    Two properties make the hold safe, and each is a test below.

    First, it steps at the RELEASE, never at the reference period.  A bar in mid-February 2024 reads
    January's payrolls only from 2024-02-04 (note 5), not from 2024-02-01 when the reference month
    ended, and not at all before the print exists.

    Second, it is BOUNDED.  An unbounded hold is how a discontinued series keeps answering forever -
    XMRUSDT's halted spot listing priced a +324% basis for two years, one feed over - so a bar whose
    newest release is older than `max_staleness_ms` reads NaN.  The bound is on the publisher's
    SILENCE, not on the age of what the number describes: the 2025 US government shutdown left PAYEMS
    76 days between prints while the numbers themselves stayed correct, so a cap at or below that would
    have blanked a healthy series.

    A revision to an OLDER reference period does not change what this returns, and that is deliberate:
    what a bar reads here is the current published LEVEL, whose reference period only ever moves
    forward.  A signal that wants the revised tail as it stood at some instant wants `as_of`, which is
    the frame this is the head of.
    """
    empty = pd.Series(np.nan, index=bars, name=column, dtype=float)
    if ledger.empty or column not in ledger.columns:
        return empty
    ordered = ledger.sort_values([AVAILABLE, REFERENCE_OPEN], ignore_index=True)
    available = ordered[AVAILABLE].to_numpy(dtype="int64")
    references = ordered[REFERENCE_OPEN].to_numpy(dtype="int64")
    released = ordered[RELEASED].to_numpy(dtype="int64")
    values = ordered[column].to_numpy(dtype=float)

    # The running head: after processing releases in availability order, what a reader sees.  A release
    # for an EARLIER reference period than the head is a revision to history and leaves the head alone;
    # `>=` rather than `>` so a revision to the CURRENT period does replace it.
    head_value = np.empty(len(ordered), dtype=float)
    head_released = np.empty(len(ordered), dtype="int64")
    best_reference = np.iinfo("int64").min
    value, release = np.nan, 0
    for i in range(len(ordered)):
        if references[i] >= best_reference:
            best_reference, value, release = references[i], values[i], released[i]
        head_value[i], head_released[i] = value, release

    # `as_unit("ms").view("int64")`, never a division: a pandas 2.x DatetimeIndex carries its own
    # resolution, so dividing assumes a unit where a conversion states it.  `metrics.align_to_bars`
    # returned NaN for every bar the first time that was got wrong.
    closes = np.asarray(bars.as_unit("ms").view("int64")) + int(interval_ms)
    position = np.searchsorted(available, closes, side="right") - 1
    seen = position >= 0
    if not bool(seen.any()):
        return empty
    picked = np.where(seen, position, 0)
    out = np.where(seen, head_value[picked], np.nan)
    stale = seen & ((closes - head_released[picked]) > int(max_staleness_ms))
    out = np.where(stale, np.nan, out)
    return pd.Series(out, index=bars, name=column, dtype=float)


def align_family_to_bars(
    ledgers: Mapping[str, pd.DataFrame],
    bars: pd.DatetimeIndex,
    *,
    interval_ms: int,
    max_staleness_ms: int = MAX_STALENESS_MS,
) -> pd.DataFrame:
    """The whole family on one bar index: bars x SERIES, which is the shape macro data actually has.

    Not bars x symbols.  A macro release is one number for the whole market, so the honest frame has a
    column per SERIES and no symbol axis at all - `Panel.metrics` and `Panel.spot` are bars x symbols
    because open interest and a spot price really do differ per symbol, and copying that shape here
    would encode a cross-section that does not exist.  `broadcast_to_symbols` is where the copy happens,
    if a caller needs it, and it says what the copy costs.

    Constant width whatever the caller downloaded, for `align_index_to_perp_bars`'s reason: a series
    that is missing must occupy the SAME columns as one that is present, or an absence changes the
    frame's shape instead of its values.
    """
    frame = pd.DataFrame(index=bars)
    for column in MACRO_COLUMNS:
        ledger = ledgers.get(column)
        frame[column] = (
            align_releases_to_bars(ledger, bars, column, interval_ms=interval_ms, max_staleness_ms=max_staleness_ms)
            if ledger is not None
            else pd.Series(np.nan, index=bars, dtype=float)
        )
    return frame


def broadcast_to_symbols(frame: pd.DataFrame, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """One bars x symbols frame per macro column, every symbol carrying the identical series.

    Provided because `Panel`'s foreign fields are bars x symbols and a caller will need this shape, and
    named `broadcast` rather than `to_panel` because the copy is the whole content of the operation and
    a reader has to see it.  What it costs, stated once so nobody rediscovers it in a shortlist:

    every cross-sectional operator in `beidou_alpha` returns a DEGENERATE result on these columns.  A
    rank over a row of identical values is the same constant for every symbol; a cross-sectional demean
    is exactly zero everywhere, to floating point.  So a macro column can only enter a signal as a
    TIME-SERIES term - a level, a change, an interaction with something that does vary across symbols -
    and any leaf that ranks or demeans it is not weak, it is empty.  That is a property of the data and
    not of the implementation, which is why it is written at the point where the shape is manufactured.
    """
    return {column: pd.DataFrame(dict.fromkeys(symbols, frame[column])) for column in frame.columns}


@dataclass(frozen=True)
class RevisionLeak:
    """Bars carrying a value that had not been published yet, and the worst one by how early it is.

    The refusal this whole module is for.  A verified stamp offset says the value describes the month
    it claims to; it says NOTHING about whether that value existed when a backtest read it, and the
    PAYEMS table at the top of this file is 668,000 units of exactly that difference.
    """

    column: str
    bars: int
    checked: int
    leaked: int
    worst_bar_ms: int | None
    worst_value: float | None
    worst_available_ms: int | None

    @property
    def clean(self) -> bool:
        return self.leaked == 0

    @property
    def reason(self) -> str:
        if self.checked == 0:
            return f"{self.column}: no non-empty bar to judge"
        if self.clean:
            return f"{self.column}: {self.checked}/{self.bars} bars carry a value, none published later than its bar"
        bar = pd.Timestamp(self.worst_bar_ms, unit="ms", tz="UTC") if self.worst_bar_ms is not None else "?"
        if self.worst_available_ms is None:
            return f"{self.column}: {self.leaked}/{self.checked} bars carry a value the ledger never published"
        early = (self.worst_available_ms - (self.worst_bar_ms or 0)) / DAY_MS
        return (
            f"{self.column}: {self.leaked}/{self.checked} bars carry a value published after the bar, "
            f"worst {early:.0f} days early (bar {bar}, value {self.worst_value})"
        )


def revision_leak(aligned: pd.Series, ledger: pd.DataFrame, column: str, *, interval_ms: int) -> RevisionLeak:
    """Does every value on this frame have a release that was available by its bar's close?

    THE test this column exists to make possible, and it is deliberately NOT written in terms of
    `align_releases_to_bars`.  Comparing the aligner against itself would pass for any aligner at all,
    including a broken one; this asks the property directly - could this number have been read yet? -
    from the ledger alone, so it also runs against a frame that arrived from somewhere else entirely.

    A bar is a leak when the value it carries appears in the ledger only under releases that became
    available AFTER the bar closed, or when it appears in the ledger not at all.  The second case is
    reported with no availability, because there is no release to name.

    A value is dated by the EARLIEST release that carried it, not the latest, and that is measured
    rather than tidy: over 2019-2026 five PAYEMS values, one CPIAUCSL value and two CPILFESL values
    were published on two or more different dates - 158,432 was May 2024's payrolls on 2024-07-05 and,
    after the 2026 benchmark revision, December 2025's on 2026-03-06.  Dating by the latest release
    would report the July 2024 bar as leaking by twenty months.

    What it does not catch, stated so the guarantee is not overread.  A bar reading a value that WAS
    published but belongs to the wrong reference period passes here, because such a value was in fact
    readable; this check is the point-in-time property alone and `verify_macro_contract` is the
    reference-period one.  And the same coincidence that forces the earliest-release rule also blunts
    it: a leak is invisible whenever the leaked number was already published for some other month.  On
    the three admitted series that is 8 values out of 1,359 rows, which is why they are admitted - on
    UNRATE it is 27 of 39 distinct values, which is the fourth reason that series is refused.
    """
    bars = len(aligned)
    if ledger.empty or column not in ledger.columns:
        return RevisionLeak(column, bars, 0, 0, None, None, None)
    values = ledger[column].to_numpy(dtype=float)
    available = ledger[AVAILABLE].to_numpy(dtype="int64")
    # Per distinct published value, the EARLIEST instant any release carried it.  Earliest, because one
    # release being late does not make a number unreadable if an earlier one already published it.
    earliest: dict[float, int] = {}
    for value, when in zip(values, available, strict=True):
        if np.isnan(value):
            continue
        previous = earliest.get(value)
        if previous is None or when < previous:
            earliest[value] = int(when)
    index_ms = np.asarray(pd.DatetimeIndex(aligned.index).as_unit("ms").view("int64"))
    closes = index_ms + int(interval_ms)
    checked = leaked = 0
    worst_early = -1
    worst: tuple[int, float, int | None] | None = None
    for bar_ms, close, value in zip(index_ms, closes, aligned.to_numpy(dtype=float), strict=True):
        if np.isnan(value):
            continue
        checked += 1
        first = earliest.get(float(value))
        if first is not None and first <= close:
            continue
        leaked += 1
        # An unpublished value sorts worst of all: there is no release to date it against, so nothing
        # can bound how early it is.  Ranked above every merely-early bar by using infinity.
        early = np.iinfo("int64").max if first is None else first - int(bar_ms)
        if early > worst_early:
            worst_early, worst = int(early), (int(bar_ms), float(value), first)
    if worst is None:
        return RevisionLeak(column, bars, checked, 0, None, None, None)
    return RevisionLeak(column, bars, checked, leaked, worst[0], worst[1], worst[2])


def admits_live_signal(
    column: str, verification: Verification | None, leak: RevisionLeak | None = None
) -> tuple[bool, str]:
    """RISK-G3 for a macro column: alignment's four refusals, plus the point-in-time one.

    The first four are `alignment.admits_live_signal`'s, unchanged and over alignment's own registry,
    because this feed registers there.  The fifth is what nothing in `alignment` can express: a column
    whose stamp offset is verified can still be unusable, because the VALUE at that stamp was rewritten
    after the bars that read it.  A missing `leak` argument is refused for the same reason a missing
    `verification` is - not-yet-shown and shown-false are both "no".

    The gap this leaves is real and worth naming rather than hiding: `alignment.admits_live_signal`
    called directly on a macro column answers only the first four, so it can say yes where this says
    no.  `test_alignments_gate_is_weaker_than_this_one_and_only_in_one_direction` pins that, and the
    edit that would close it is an `extra_refusals` hook on alignment's own function.
    """
    admitted, reason = alignment_admits_live_signal(column, verification)
    if not admitted:
        return False, reason
    if leak is None:
        return False, f"RISK-G3: {column} has no point-in-time evidence on record"
    if not leak.clean:
        return False, f"RISK-G3: {leak.reason}"
    return True, f"{reason}; {leak.reason}"


class AlfredClient:
    """ALFRED's vintage query, which is the only endpoint here that needs the key.

    Shaped after `onchain.CommunityClient` - injectable transport, retry on 5xx only, backoff as a seam
    rather than a knob - because those were learned once already.  The one thing it does that no other
    client in this package does is redact: FRED authenticates by query parameter only, so `httpx`'s own
    `raise_for_status` would put the key into an exception message and from there into a traceback, a
    log line, or a governance report.  `_get` therefore raises `FredRequestFailed` carrying the path
    and the status and never the URL.
    """

    def __init__(
        self,
        key: str,
        base_url: str = FRED_BASE_URL,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._key = key
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)
        self._backoff = backoff

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AlfredClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        delay = self._backoff
        for attempt in range(_RETRIES):
            response = self._client.get(path, params={**params, "api_key": self._key, "file_type": "json"})
            if response.status_code < 500 or attempt == _RETRIES - 1:
                if response.status_code >= 400:
                    # Never `raise_for_status`: its message embeds the URL, and the URL holds the key.
                    raise FredRequestFailed(f"{path} answered HTTP {response.status_code}")
                payload = response.json()
                assert isinstance(payload, dict)
                return payload
            if delay:
                time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def release_ledger(self, series: MacroSeries, *, start: str, end: str) -> pd.DataFrame:
        """Every vintage of one series over `[start, end]`, as a release ledger.

        `realtime_start=1776-07-04` and `realtime_end=9999-12-31` are ALFRED's own idiom for "the whole
        real-time axis" and they are what turn this from a FRED query into an ALFRED one.  Without them
        the endpoint answers with today's vintage only - a complete, correctly shaped, silently
        look-ahead frame, which is why `api_key` refuses to run keyless rather than falling back.

        Not paged, and that is measured rather than assumed: a monthly series over 2019-2026 returns
        386-584 rows in one response, well inside the endpoint's limit.  The DAILY series are the ones
        that overflow, and they overflow at the VINTAGE-COUNT limit with an HTTP 400 rather than by
        paging (note 7), which is a refusal to answer and not a page to follow.
        """
        payload = self._get(
            "/fred/series/observations",
            {
                "series_id": series.fred_id,
                "observation_start": start,
                "observation_end": end,
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
            },
        )
        return parse_release_ledger(payload, series.column)

    def family_ledgers(
        self, *, start: str, end: str, series: Iterable[MacroSeries] = MACRO_SERIES
    ) -> dict[str, pd.DataFrame]:
        return {item.column: self.release_ledger(item, start=start, end=end) for item in series}


class BlsWitnessClient:
    """api.bls.gov's public v1 API, used for exactly one thing: falsifying the reference-period stamp.

    Separate from `AlfredClient` and deliberately tiny, for the reason `onchain.WitnessClient` is: this
    source is never stored and never becomes a panel column, so it has no contract and no point-in-time
    evidence and by this module's own rule may not reach live.  Its whole job is to be the ORIGINAL
    publisher's opinion about which month a number belongs to, which is what turns "FRED's stamp
    matched" into a check that can fail.

    No key, and none is wanted: v1 is anonymous and rate-limited to a small daily budget, which is the
    right size for something run when a contract is re-verified rather than on a schedule.
    """

    def __init__(
        self, base_url: str = BLS_BASE_URL, *, timeout: float = 60.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BlsWitnessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def series(self, item: MacroSeries, *, start_year: int, end_year: int) -> pd.DataFrame:
        response = self._client.get(
            f"/publicAPI/v1/timeseries/data/{item.bls_id}",
            params={"startyear": str(start_year), "endyear": str(end_year)},
        )
        response.raise_for_status()
        payload = response.json()
        assert isinstance(payload, dict)
        return parse_bls_series(payload, item.column)
