"""Event-time contracts: what a column's numbers describe, and when live could first have read them.

RISK-G3 in one sentence: a new data column can carry a look-ahead that NOTHING reports, because the
defect is a timestamp CONVENTION rather than a bug.  The metrics column is the measured case.  On
2026-09-07 the T+1 archive's `create_time` was found to equal the REST `timestamp` minus five minutes
on 166 of 166 buckets, and to match on 0 of 165 at an offset of 0 or +-10 minutes.  The obvious join -
the two sources' own stamps taken as equal - was therefore wrong for EVERY bucket, in the direction
that flatters: research would have read a value five minutes before the loop could have.  One bucket of
open interest moves a median 0.090%, p95 1.15%.  Small, systematic, always favourable, and silent.

Two things follow, and this module is both of them.

First, the relationship is DECLARED rather than implemented.  A conversion buried in a parser is a fact
about one source pair that no other column can inherit and no test can point at; a contract is a
statement a sample can be held against.  So a contract names, per source, what that source's own stamp
means as a signed offset from the EVENT time - the instant the number describes - plus the offset from
event time to AVAILABLE time, the earliest instant live could read it.

Second, and this is the half that is easy to get wrong: a check that only CONFIRMS the declared offset
is not a check.  A column whose values barely move agrees with itself at every offset, so "the declared
offset matches" would pass on exactly the data that cannot tell offsets apart.  What the 2026-09-07
session collected was a pair - 166/166 at the declared offset AND 0/165 at its neighbours - and the
second number is the evidence.  `verify_stamp_offset` requires the rival offsets to be REFUTED by the
same sample and answers UNVERIFIABLE, not PASS, when they are not.

The refusals are RISK-G3's, and the first three are deliberately the same three `parity_satisfied`
makes one level up: an undeclared column, a declared column with no verification on record, and a
verification that came back FAIL or UNVERIFIABLE all mean "this column does not reach a live signal".
Not-yet-shown and shown-false are both "no", because the obligation is to have shown the offset.

The fourth was found by pointing this module at the production stores on 2026-09-09, and it is the one
nothing else here would have predicted: a verification is admitted PER COLUMN, on the strength of that
column's own comparisons.  Four of the six metrics columns turned out to have no live counterpart at
all - `snapshot_metrics` polls `/futures/data/openInterestHist`, which never returns the three ratio
fields `REST_TO_ARCHIVE` maps - so those columns are NaN in every snapshot row.  `metrics_parity` folds
columns into a row verdict and skips NaN pairs, so it reads `differing: 0, rate: 0.0` over data it
never compared, and the M-011 gate reports parity met.  Agreement on a neighbouring column is not
evidence about this one.

Describes `beidou_data.metrics`; does not replace it.  That module already converts both stamps to one
canonical `open_time`, and it runs in the live loop, so the contract states what it does and a test
holds the two against each other.  Whichever one a later change moves, the test fails.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import pandas as pd

from beidou_data.metrics import PERIOD_MS, VALUE_COLUMNS

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIABLE = "UNVERIFIABLE"

# The merge key.  Underscored because it is this module's own scratch column and must never be mistaken
# for `open_time`, which is what the STORE holds; the whole subject here is stamps that look alike.
_EVENT = "_event_time_ms"


class UndeclaredColumn(LookupError):
    """RISK-G3's first refusal: a column nobody wrote a contract for may not reach a live signal."""


@dataclass(frozen=True)
class Stamp:
    """What ONE source's own timestamp names, as a signed offset from the event time.

    `offset_ms` is `source stamp - event time`, so a source that stamps a bucket by its close carries
    `+period` and one that stamps it by its open carries `0`.  Signed and in milliseconds rather than
    "buckets" because the next column need not be bucketed at all - a settlement or an announcement has
    an event time and an availability with no period between them.

    `means` is prose and is load-bearing anyway: the 2026-09-07 session had to MEASURE which stamp was
    the open and which the close (re-reading the three newest REST buckets six minutes apart returned
    byte-identical values, so the newest row is a complete bucket and its stamp is the close).  A
    contract that carried only the number would let the next author guess that part again.
    """

    column: str
    offset_ms: int
    means: str

    def event_time(self, stamp_ms: int) -> int:
        return int(stamp_ms) - self.offset_ms

    def event_times(self, stamps: pd.Series) -> pd.Series:
        return _epoch_ms(stamps) - self.offset_ms


class Rival(NamedTuple):
    """One refuted hypothesis: how the sample scored under an offset the contract says is wrong."""

    rest_offset_ms: int
    matched: int
    compared: int


@dataclass(frozen=True)
class Verification:
    """The answer, with its numbers, because "it passed" is not a measurement.

    `matched`/`compared` is the 166/166 half and `rivals` is the 0/165 half.  Both are carried so a
    report can print what was actually shown rather than a bare verdict.

    `compared_columns` is named separately from the row counts and is the field that catches a whole
    second family of defect.  Row counts are column-blind: a column that exists in ONE source only
    contributes no comparison, and a check that folds columns into a row verdict then reports perfect
    agreement over data it never looked at.  Measured on the production stores 2026-09-09 - four of the
    six metrics columns are archive-only, and `metrics_parity` reads `differing: 0, rate: 0.0` anyway.
    So a column is admitted on the strength of its OWN comparisons, never on its neighbours'.
    """

    verdict: str
    reason: str
    compared: int
    matched: int
    rivals: tuple[Rival, ...] = ()
    compared_columns: tuple[str, ...] = ()
    uncompared_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventTimeContract:
    """One data column's event time, availability, and the two stamps that name them.

    `available_offset_ms` is deliberately arithmetic and not observed latency.  The measured REST delay
    on 2026-09-07 was two to three minutes after a bucket closed, and baking that into a correctness
    boundary would make the boundary quietly wrong the day the venue gets faster or slower - the same
    reasoning `beidou_data.metrics.usable_from_ms` already gives.  The boundary is what must be TRUE
    (the bucket has closed); the latency is an operational fact that belongs in a health report.
    """

    name: str
    period_ms: int
    archive: Stamp
    rest: Stamp
    available_offset_ms: int
    measured: str

    @property
    def stamp_offset_ms(self) -> int:
        """`rest stamp - archive stamp` for one bucket: the number the check has to falsify."""
        return self.rest.offset_ms - self.archive.offset_ms

    def available_from(self, event_time_ms: int) -> int:
        return int(event_time_ms) + self.available_offset_ms

    def rival_rest_offsets(self) -> tuple[int, ...]:
        """The offsets this sample must REFUTE before a match at the declared one means anything.

        Three hypotheses, chosen because each one is a defect somebody actually reaches for: the naive
        join (both stamps taken as equal - the historical defect), and one period either side of the
        declared offset (an off-by-one in whichever direction).  Deduped against the declared offset,
        so for metrics - declared +1 bucket, archive at 0 - the rivals are exactly the 0 and +10 minutes
        the 2026-09-07 session tested.
        """
        declared = self.rest.offset_ms
        candidates = (self.archive.offset_ms, declared - self.period_ms, declared + self.period_ms)
        return tuple(sorted({offset for offset in candidates if offset != declared}))


# The first instance, and the one that was measured rather than assumed.  Every field here restates
# something `beidou_data.metrics` already does; `tests/data/test_a_column_may_not_reach_live_without_a
# _declared_offset.py` holds the two against each other so neither can move alone.
METRICS = EventTimeContract(
    name="metrics",
    period_ms=PERIOD_MS["5m"],
    archive=Stamp("create_time", 0, "the bucket OPEN"),
    rest=Stamp("timestamp", PERIOD_MS["5m"], "the bucket CLOSE"),
    available_offset_ms=PERIOD_MS["5m"],
    measured=(
        "2026-09-07 against the venue: archive create_time == rest timestamp - 5m on 166/166 buckets, "
        "and 0/165 at offsets of 0 and +-10m.  Which stamp is the close was measured too: re-reading "
        "the three newest REST buckets six minutes apart returned byte-identical values."
    ),
)

# Keyed by COLUMN, not by feed, because RISK-G3's refusal is per column: "该列不进实盘".  A feed's
# columns share a contract, and a column nobody listed here has none - which is the point.
CONTRACTS: dict[str, EventTimeContract] = dict.fromkeys(VALUE_COLUMNS, METRICS)


def contract_for(column: str) -> EventTimeContract:
    try:
        return CONTRACTS[column]
    except KeyError:
        raise UndeclaredColumn(
            f"{column!r} has no event-time contract; declare one in beidou_data.alignment.CONTRACTS "
            "and verify it before any signal reads it (RISK-G3)"
        ) from None


def admits_live_signal(column: str, verification: Verification | None) -> tuple[bool, str]:
    """RISK-G3: may a signal that reads this column trade?  Answered with its reason.

    The reason travels with the bool on purpose, the same way `parity_satisfied` returns one: what a
    governance report needs to print is why a candidate has been sitting in `booked`, and a bare False
    forces the caller to re-derive it - which is where the two copies start to disagree.
    """
    try:
        contract = contract_for(column)
    except UndeclaredColumn as exc:
        return False, f"RISK-G3: {exc}"
    if verification is None:
        return False, f"RISK-G3: {column} is under the {contract.name} contract but has no verification on record"
    if verification.verdict != PASS:
        return False, f"RISK-G3: {contract.name} is {verification.verdict} - {verification.reason}"
    if column not in verification.compared_columns:
        # The column-blind hole, closed.  A verification passing on its NEIGHBOURS says nothing about a
        # column that appeared on one side only, and that is not hypothetical: on 2026-09-09 four of the
        # six metrics columns had no live counterpart at all while M-011 read `rate: 0.0`.
        return False, f"RISK-G3: the {contract.name} verification never compared {column} itself"
    return True, f"{contract.name}: {verification.reason}"


def _epoch_ms(stamps: pd.Series) -> pd.Series:
    """Epoch milliseconds from however a source spells its stamp.

    The archive writes `create_time` as a UTC datetime string and REST writes `timestamp` as integer
    milliseconds; a contract that accepted only one would push the parsing back into every caller, and
    parsing is where the resolution bugs live.  `as_unit("ms")` rather than dividing by a constant, for
    the reason `align_to_bars` records: a pandas 2.x datetime carries its own resolution, so a division
    assumes a unit where a conversion states it.  That mistake is this module's own subject, one level
    down - the first version of `align_to_bars` returned NaN for every bar because of it.
    """
    if pd.api.types.is_numeric_dtype(stamps):
        return stamps.astype("int64")
    return pd.to_datetime(stamps, utc=True).dt.as_unit("ms").astype("int64")


def restamp(frame: pd.DataFrame, stamp: Stamp, *, event_column: str = "open_time") -> pd.DataFrame:
    """A canonical frame put back into ONE source's own stamp convention.

    Both stores hold `open_time`, so the raw stamps a check would want are no longer on disk.  They are
    recoverable exactly - the conversion is a constant shift and nothing else - and it is worth being
    precise about what that bounds: the arithmetic goes back in exactly as declared, so a check on
    restamped frames CANNOT re-observe the venue and cannot refute the contract by itself.

    What it can still refute is the half that matters.  The VALUES in the two stores came from two
    independent downloads of the same buckets, and they either coincide at the declared offset and
    scatter at its rivals, or they do not.  That is the same evidence the 2026-09-07 session collected;
    only the stamp bookkeeping is reconstructed.
    """
    out = frame.drop(columns=[event_column])
    out[stamp.column] = frame[event_column].astype("int64") + stamp.offset_ms
    return out


def _agreement(
    archive: pd.DataFrame, rest: pd.DataFrame, columns: Sequence[str], tolerance: float
) -> tuple[int, int, tuple[str, ...]]:
    """Rows the two sources can be compared on, rows on which they agree, and which columns carried it.

    A row counts as compared only if at least one value column is present on BOTH sides.  A pair of
    NaNs is not agreement - it is the absence of a comparison - and counting it as a match is how a
    check on a mostly-empty column reports a perfect score.  Same lesson `metrics_parity` records when
    it returns `rate: None` instead of 1.0, applied to the denominator instead of the numerator.

    The tolerance is RELATIVE, and that is a correction rather than a preference.  These columns span a
    ratio near 1 and an open-interest notional near 1e10 USDT, so one absolute number cannot serve both
    ends.  Measured on the production stores 2026-09-09: the largest disagreement between the two
    sources is exactly ONE float64 ULP (29 of ADAUSDT's 155 shared buckets, 1.5e-8 absolute at 8.4e7,
    1.8e-16 relative) - the two sources' decimal spellings of one number, not a difference in the
    number.  An absolute tolerance prices that by magnitude: `metrics_parity`'s 1e-6 covers a ULP up to
    2^33 = 8.590e9 and not past it, and BTCUSDT's notional was 8.515e9 that day.  So the margin on the
    absolute check is 0.9% of the value, and a 1% rise in BTC open interest makes it report float
    spelling as a source disagreement.  The default here is 1e-12 relative: four orders above the ULP
    noise, five below the smallest real move this data shows (a bucket of open interest moves a median
    0.090%), and independent of scale in a way no absolute number can be.
    """
    merged = archive.merge(rest, on=_EVENT, suffixes=("_a", "_r"))
    if merged.empty:
        return 0, 0, ()
    comparable = pd.Series(False, index=merged.index)
    agrees = pd.Series(True, index=merged.index)
    carried: list[str] = []
    for column in columns:
        left = pd.to_numeric(merged[f"{column}_a"], errors="coerce")
        right = pd.to_numeric(merged[f"{column}_r"], errors="coerce")
        both = left.notna() & right.notna()
        if not bool(both.any()):
            continue
        carried.append(column)
        comparable |= both
        scale = np.maximum(left.abs().to_numpy(), right.abs().to_numpy())
        agrees &= ~both | ((left - right).abs() <= tolerance * scale)
    return int((comparable & agrees).sum()), int(comparable.sum()), tuple(carried)


def verify_stamp_offset(
    contract: EventTimeContract,
    archive: pd.DataFrame,
    rest: pd.DataFrame,
    *,
    value_columns: Sequence[str] | None = None,
    tolerance: float = 1e-12,
    min_overlap: int = 12,
) -> Verification:
    """Does the declared offset hold on these two samples, and do its rivals fail on the same ones?

    Both frames carry their OWN source's stamp column - `create_time` and `timestamp` for metrics - and
    not `open_time`.  Handing this function canonical frames would be the circular version of the check:
    the conversion under test would already have been applied.  `restamp` exists for the case where only
    canonical frames survive, and says in its own docstring what that costs.

    `min_overlap` defaults to twelve, one hour of five-minute buckets, which is what a single
    `snapshot_metrics` poll retrieves - a deliberately low bar, because the count is not what carries
    the evidence.  Refuting the rivals is.  A thousand buckets of a column that never moves still
    answers UNVERIFIABLE, and twelve buckets of a moving one is a real measurement.
    """
    columns = [
        column
        for column in (value_columns if value_columns is not None else archive.columns)
        if column in archive.columns and column in rest.columns and column not in ("symbol", _EVENT)
    ]
    if not columns:
        return Verification(UNVERIFIABLE, "the two samples share no comparable value column", 0, 0)
    for label, frame, stamp in (("archive", archive, contract.archive), ("rest", rest, contract.rest)):
        if stamp.column not in frame.columns:
            return Verification(UNVERIFIABLE, f"the {label} sample has no {stamp.column!r} column", 0, 0)

    keyed_archive = archive.assign(**{_EVENT: contract.archive.event_times(archive[contract.archive.column])})
    rest_stamps = _epoch_ms(rest[contract.rest.column])
    for label, keyed in (("archive", keyed_archive[_EVENT]), ("rest", rest_stamps)):
        if keyed.duplicated().any():
            # A repeated event time fans the merge out, which inflates every count below and can make a
            # rival look refuted by arithmetic rather than by the data.  Refused rather than deduped:
            # dropping rows to make a check runnable is how the check stops meaning anything.
            return Verification(UNVERIFIABLE, f"the {label} sample repeats an event time", 0, 0)

    def score(rest_offset_ms: int) -> tuple[int, int, tuple[str, ...]]:
        keyed_rest = rest.assign(**{_EVENT: rest_stamps - rest_offset_ms})
        return _agreement(keyed_archive, keyed_rest, columns, tolerance)

    matched, compared, carried = score(contract.rest.offset_ms)
    uncarried = tuple(column for column in columns if column not in carried)
    seen = (carried, uncarried)
    if compared < min_overlap:
        return Verification(
            UNVERIFIABLE,
            f"only {compared} comparable overlapping rows, {min_overlap} required",
            compared,
            matched,
            (),
            *seen,
        )
    if matched != compared:
        return Verification(
            FAIL,
            f"the declared offset of {contract.stamp_offset_ms} ms holds on {matched}/{compared} rows",
            compared,
            matched,
            (),
            *seen,
        )
    rivals = tuple(Rival(offset, *score(offset)[:2]) for offset in contract.rival_rest_offsets())
    blind = [rival for rival in rivals if rival.compared == 0 or rival.matched == rival.compared]
    if blind:
        return Verification(
            UNVERIFIABLE,
            "this sample cannot tell the declared offset apart from "
            + ", ".join(f"{rival.rest_offset_ms} ms ({rival.matched}/{rival.compared})" for rival in blind)
            + "; the values do not move enough to carry the evidence",
            compared,
            matched,
            rivals,
            *seen,
        )
    note = f"; {len(uncarried)} column(s) had no rows on both sides: {list(uncarried)}" if uncarried else ""
    return Verification(
        PASS,
        f"{matched}/{compared} at the declared offset, "
        + ", ".join(f"{rival.matched}/{rival.compared} at {rival.rest_offset_ms} ms" for rival in rivals)
        + note,
        compared,
        matched,
        rivals,
        *seen,
    )
