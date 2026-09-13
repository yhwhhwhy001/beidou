"""The instrument behind `test_suite_duration.py`: measure the suite's wall clock, and say so.

Kept out of `beidou_*` on purpose.  This exists to watch a budget; charging its lines to the source
budget it watches would be its own small joke, and the packages are already at their ceilings.

See `test_suite_duration.py` for why the ceiling is loose and the measurement is always printed.
"""

from __future__ import annotations

import time
from typing import Any

# The refactor plan's M-003 threshold.  Kept, not redefined: measured 45.3 / 47.0 / 47.1 on 2026-09-07.
PLAN_SECONDS = 30

# The ratchet.  Raising it is allowed only in the commit that explains why - the rule
# `test_source_budget.py` already runs under.  This is that explanation.
#
# 2026-09-08: 120 was `~2.5x today's laptop reading`, with "a CI box two to three times slower" as an
# ASSUMPTION.  It was never true.  `ubuntu-latest` has now been measured on six consecutive runs of
# this suite - 106.1 / 118.5 / 128.3 / 143.2 / 148.5 / 153.2 seconds against 46-47 on the laptop, i.e.
# a ratio of 2.3x to 3.3x, not 2-3x - so THREE of the five runs since this file landed were already
# over 120.  Nobody saw them: `suite_verdict` refuses to promote a timing breach over a real failure,
# and there was a real failure in every one of those runs (an "offline" test that reached
# fapi.binance.com, red on GitHub's geo-blocked runners).  Fixing that test is what made this visible
# - the second gate standing behind the first, which is the shape this repository keeps finding.
#
# 240 is 1.6x the slowest CI run observed and ~5x the laptop's.  Loose on purpose, and honest about
# what that buys: this number is set by the slowest machine the suite runs on, so on the laptop it
# catches almost nothing.  What it still catches is a STEP change on either box - a sleep, a
# quadratic fixture, a fixture that rebuilds the panel per test - which on this suite is tens of
# seconds, not the 20 that separated 120 from the observed spread.  Drift is the printed line's job,
# not this one's; that split is the whole design and is unchanged.
#
# The alternative considered and NOT taken: parallelising CI (`pytest -n auto`) would cut the wall
# clock rather than raise the bar, but it adds a dependency and changes what a "run" means, which is
# a decision for the operator rather than a side effect of a ratchet breach.  Still not taken, and now
# priced: at 159ms a test it would buy back roughly half the wall clock on a two-core runner.
#
# 2026-09-13: 340.5s on CI, the first reading since 09-08 - the Types gate had been red for 23 pushes
# and the test step never ran once, so this ratchet went four days without a measurement, the same
# blindness the 240 note describes one gate further up.  Two things came out of the breach:
#
# 1. A sleep, exactly as advertised.  `MetricsArchiveClient` had no `backoff` seam, so the two tests
#    that exercise its 5xx path each really slept 1+2+4s - 14s on the laptop, ~42s here.  Fixed rather
#    than budgeted for; the seam is the one `onchain.CommunityClient` already carried.
# 2. The rest is VOLUME, not a slowdown, and the numbers say so in the only way that settles it:
#
#      laptop 2026-09-07   46.5s /  847 tests = 54.9ms     CI 2026-09-08 (slowest) 153.2s = 180.9ms
#      laptop 2026-09-13   97.2s / 1873 tests = 51.9ms     CI 2026-09-13 (est.)    298.4s = 159.4ms
#
#    The laptop column is measured on both dates; the CI estimate is 340.5s less the ~42s of sleeps
#    and is the one number here the next green run replaces.  Per test it got CHEAPER on both boxes.  The suite grew 2.21x in tests since the 240 was set and
#    its wall clock grew 2.22x; that is the whole of it.  A ceiling that fires on growth measures the
#    repository's size, not its speed, which is not what this instrument is for.
#
# 400 is set against the 298.4s estimate above, and deliberately also clears the 340.5s that was
# actually observed - so the next run cannot go red on my arithmetic being wrong about the sleeps.
# That leaves ~100s of headroom against the expectation, the same proportion the 240 carried over its
# own slowest reading (87s over 153.2s).  One CI reading stands behind this number where six stood
# behind 240; the next runs will say whether it wants tightening.
CEILING_SECONDS = 400

# Below this many selected tests the run was filtered (`pytest tests/live`, `-k`, `-m`), and a filtered
# run is not the thing the plan put a number on.  847 selected as of 2026-09-08 and 1,873 as of
# 2026-09-13, network markers: 0.  Left at 700: it is a floor under "was this the whole suite", not a
# second ratchet, and raising it with the count would make deleting tests fail the run.
FULL_SUITE_MIN = 700

_STARTED_AT: float | None = None


def set_limits(*, ceiling: float | None = None, full_suite_min: int | None = None) -> None:
    """Override the limits.  For the wiring test; the real conftest imports the hooks and nothing else."""
    global CEILING_SECONDS, FULL_SUITE_MIN
    if ceiling is not None:
        CEILING_SECONDS = ceiling
    if full_suite_min is not None:
        FULL_SUITE_MIN = full_suite_min


def measurement_line(duration: float) -> str:
    return f"suite: {duration:.1f}s (ceiling {CEILING_SECONDS}s; the plan's M-003 asked for {PLAN_SECONDS}s)"


def suite_verdict(*, duration: float, exitstatus: int, collected: int) -> tuple[int, str | None]:
    """`(exit status, message)`.  The message is the breach; `None` means there was nothing to say."""
    if collected < FULL_SUITE_MIN:
        return exitstatus, None
    if duration <= CEILING_SECONDS:
        return exitstatus, None
    message = (
        f"suite took {duration:.1f}s, over its {CEILING_SECONDS}s ceiling "
        f"(the plan's M-003 asked for {PLAN_SECONDS}s, breached since before it was measured). "
        "Make it faster, or raise CEILING_SECONDS in the commit that says why."
    )
    # Never promote a timing breach over a real failure: the operator reads the exit code first.
    return (exitstatus if exitstatus != 0 else 1), message


def pytest_configure(config: Any) -> None:  # the arg is unused; pytest matches hooks by name and signature
    global _STARTED_AT
    _STARTED_AT = time.monotonic()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if _STARTED_AT is None:
        return
    duration = time.monotonic() - _STARTED_AT
    collected = int(getattr(session, "testscollected", 0) or 0)
    status, message = suite_verdict(duration=duration, exitstatus=exitstatus, collected=collected)
    if collected >= FULL_SUITE_MIN:
        print("\n" + measurement_line(duration))
    if message:
        print(message)
    session.exitstatus = status
