"""R1: how many trials a window may buy, read off the ledger rather than remembered.

R3, R4, R5 and R7 are properties of the book's state and live in `lifecycle`.  R1 is not: it is a
property of the trials ledger, which is append-only, shared between sessions, and the only record that
can say what a window actually cost.  Keeping it here rather than in `lifecycle` is what stops the
state machine from holding a counter that a parallel session's `research validate` has already made
wrong - the 2026-09-08 incident was exactly that shape, a search charging 514 rows with nothing on the
terminal saying so.

The count is of DISTINCT trials, folded the way `unique_trials` folds them, because that is what the
DSR denominator reads.  Counting raw lines would charge a replay twice and make the budget stricter
than the thing it is budgeting.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from beidou_alpha.validation.ledger import TrialRecord, all_trials, unique_trials
from beidou_governance.policy import Policy


@dataclass(frozen=True)
class LedgerBudget:
    window_start: str
    spent: int
    allowed: int

    @property
    def remaining(self) -> int:
        return max(0, self.allowed - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.allowed


def _recorded_at(record: TrialRecord) -> datetime | None:
    try:
        return datetime.fromisoformat(record.recorded_at)
    except ValueError:
        return None


def window_spend(lines: Iterable[str], *, window_start: datetime, policy: Policy | None = None) -> LedgerBudget:
    """What this window has already bought, and what R1 still allows.

    Rows with an unreadable timestamp are counted as inside the window.  That is the conservative
    direction: an unparseable row is one this function cannot prove was bought earlier, and a budget
    that resolves its own doubt in favour of spending more is not a budget.
    """
    policy = policy or Policy()
    inside: list[TrialRecord] = []
    for record in all_trials(lines):
        when = _recorded_at(record)
        if when is None or when >= window_start:
            inside.append(record)
    return LedgerBudget(
        window_start=window_start.isoformat(),
        spent=len(unique_trials(inside)),
        allowed=policy.max_ledger_rows_per_window,
    )


def refusals(budget: LedgerBudget, wanted: int) -> Sequence[str]:
    """R1 as the scheduler asks it: may this run of ``wanted`` trials start at all?

    A run that would cross the line is refused whole rather than truncated.  Truncating it would
    produce a search whose `declared_trials` counts candidates that were never scored, which is the
    number `research validate --prior-trials` feeds into the DSR denominator - a half-charged family
    is worse than an uncharged one.
    """
    if wanted <= 0:
        return ()
    if budget.exhausted:
        return (f"R1: the window's {budget.allowed}-trial budget is spent ({budget.spent})",)
    if wanted > budget.remaining:
        return (f"R1: {wanted} trials wanted, {budget.remaining} left of {budget.allowed} this window",)
    return ()
