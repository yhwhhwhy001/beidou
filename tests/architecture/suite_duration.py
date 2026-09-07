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

# The ratchet.  ~2.5x today's reading, which is the loosest thing that is still worth having: it clears
# the 4% run-to-run spread and a CI box two to three times slower than this laptop, while a sleep, a
# stray network call or a quadratic fixture still trips it.  Raising it is allowed only in the commit
# that explains why - the rule `test_source_budget.py` already runs under.
CEILING_SECONDS = 120

# Below this many selected tests the run was filtered (`pytest tests/live`, `-k`, `-m`), and a filtered
# run is not the thing the plan put a number on.  741 selected as of 2026-09-07, network markers: 0.
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
