"""R0-R10: every governance threshold, in one place, behind a version (R10).

Two properties this module exists to hold.

**Thresholds live in code, not in configuration.**  A YAML the machine can edit is a machine that
can move its own bar, and the whole argument for taking the human out of the runtime loop is that
the rules are fixed in advance.  Changing a number here changes `policy_digest()`, which changes
what the loop records every cycle (R9), which makes a silent drift impossible to hide.

**Every number says where it came from.**  `PROVENANCE` marks each rule 推导 (derived from a
mechanism), 先例 (an earlier operator decision this reuses) or E5 (a judgement call with no
evidence behind it).  Four of the eleven are E5 and saying so is the point: an unlabelled table
of constants reads as though somebody measured them.

The window is one month (Q3, operator ruling 2026-09-08).  Two of the numbers below are what they
are *because* of that ruling rather than independently: `windows_to_main` went 3 -> 9 and
`freeze_windows` 2 -> 6 to hold the pre-registered strength constant when the window shortened from
a quarter to a month.  `min_clean_days` equals the window exactly, which is why it is an admission
condition in `lifecycle` rather than a background assumption: `construction_fingerprint` includes
`strategy_weights` (`beidou_live/engine.py`, from `LiveConfig` over `registry.enabled`), so every
promotion resets M-010's 30-day clock, and a monthly window has zero slack against it.  A month is
an opportunity to promote, never an obligation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

POLICY_VERSION = "0.2.0"
"""0.2.0 (2026-09-08): R1 charges a mine ROUND as one event rather than as its row count.

Why it moved.  R1's budget was anchored on "one mine round cost 514 rows" and then set to 500 a
quarter - so under Q3's monthly window it became 170, which is a THIRD of a single round, and
`refusals` refuses whole rather than truncating (a truncated search reports a `declared_trials`
counting candidates nobody scored).  R1 + R2 + the scheduler therefore made `research mine`
impossible to run at all, in any window, forever.

The fix is not a bigger number.  R1 exists to bound how many SELECTIONS a window makes, and one mine
round is one selection - enumerate the whole space, take the top k by marginal - not 514 independent
ones.  That is exactly why `ledger_scope` files them into one shared `mined` bucket.  Charging by row
count penalises searching MORE THOROUGHLY as though it were choosing more often, which is backwards.

The DSR denominator is untouched: it still counts every row in the mined bucket, because the deflation
question really is "best of how many", and that number really is 514.
"""


@dataclass(frozen=True)
class Policy:
    """R0-R10.  Frozen: a rule set that can be mutated after a decision is not a rule set."""

    # --- the calendar every other rule counts in (Q3) ---
    window_days: int = 30

    # R0: which trials the D-028 quantile gate counts.  Not a number - a caliber.  The per-strategy
    # bucket (`ledger_scope`) stays the gate; the whole-library N and N_eff are reported beside it.
    # Deriving the gate from the whole library would FAIL the incumbent on an honest grid (KILL-AR-01),
    # and N_eff can only lower the bar (`multiple_testing.effective_trials` is documented as reported,
    # never substituted).  Family-level risk is controlled on the exposure side by R3/R4/R5 instead.
    gate_scope: str = "per_strategy_bucket"
    report_whole_library_n: bool = True

    # R1: new ledger rows per window, EXCLUDING the shared `mined` bucket.  ~500 a quarter, split by
    # the shorter window; the ledger's growth rate is unchanged by Q3.  E5, anchored on one `research
    # mine` round costing 514 rows - which is also why that round is no longer charged here (0.2.0).
    max_ledger_rows_per_window: int = 170
    # ...and the mine rounds themselves, counted as events.  One selection per window: enumerating the
    # whole space and taking the top k by marginal is ONE choice, however wide the space was.
    max_mine_rounds_per_window: int = 1

    # R2: `research mine` runs only when the search space changed.  Re-running the same space and
    # keeping the rows charges the family twice for one hypothesis (the 2026-09-08 incident).
    mine_requires_new_search_space: bool = True

    # R3: how much of the book unproven sleeves may move.  D-018/D-019's shape, unchanged.
    max_concurrent_probes: int = 2
    probe_budget_share: float = 1.0 / 3.0

    # R4: promotions per window.  One in, one up: the binding constraint is R3's two probe slots.
    max_queued_to_probe_per_window: int = 1
    max_probe_to_main_per_window: int = 1

    # R5: consecutive stopped probes that freeze promotion, and for how long.  Six windows is the
    # same six months two quarters used to be.
    freeze_after_consecutive_stops: int = 2
    freeze_windows: int = 6

    # R7: a candidate gets three lives.  After leaving probe the third time it is retired for good.
    max_probe_entries_lifetime: int = 3
    demotion_cooldown_windows: int = 1

    # probe -> main: nine consecutive surviving windows.  The pre-registered number was three, when a
    # window was a quarter; nine months of survival is the same filter.  Calibrated against D-019's
    # -2 sigma stop (E5, normal approximation, windows treated as independent): a Sharpe-0 sleeve
    # reaches main 81.0% of the time, a Sharpe-1.7 sleeve 94.4%.  Both numbers are weak, and that is
    # the honest description of the rule: it separates blown-up from not-blown-up, not alpha from noise.
    windows_to_main: int = 9

    # The clean-record requirement a promotion must find already satisfied, and which it then resets.
    min_clean_days_before_promotion: int = 30

    # R8: the drawdown ladder, on attributed P&L rather than venue equity - 52% of this account's
    # equity is non-USDT collateral, so an equity drawdown can be bitcoin moving and nothing else.
    # The first crossing alerts and waits two cycles before acting, so one bad print cannot halve
    # the risk budget.
    drawdown_ladder: tuple[tuple[float, float], ...] = ((-0.35, 0.225), (-0.50, 0.15))
    drawdown_grace_cycles: int = 2

    # R9 / R10: the digest is recorded every cycle, and it is what makes an edit here visible.
    record_digest_every_cycle: bool = True

    # Cycles that produce no governance decision at all (KILL-AR-20).  A demo account reset arrives
    # as a TRANSFER row with no fills; the path to the venue crosses a proxy that returns 503 in
    # bursts.  Counting either as evidence would let the venue's plumbing retire a strategy.
    no_decision_phases: tuple[str, ...] = ("ERROR",)
    no_decision_on_rebaseline: bool = True

    version: str = field(default=POLICY_VERSION)

    def digest(self) -> str:
        """R9's number: what the live loop records so a threshold change cannot be silent."""
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]

    def throttle_scalar(self, drawdown: float) -> float | None:
        """The vol target the ladder asks for at this drawdown, or None above the first rung.

        `drawdown` is negative.  Rungs are checked deepest-first so -0.60 gets 0.15, not 0.225.
        """
        for level, target in sorted(self.drawdown_ladder, key=lambda rung: rung[0]):
            if drawdown <= level:
                return target
        return None


#: Where each rule comes from, so nobody reads the table above as measurement (KILL-AR-13).
PROVENANCE: dict[str, str] = {
    "R0": "推导 - whole-library N fails the incumbent on an honest grid; N_eff only lowers the bar",
    "R1": "E5 - anchored on one mine round costing 514 rows, no evidence that 170 is the right ceiling",
    "R2": "先例 - K-EX07's shape: whether a replay's rows stay is a ruling, not the command's choice",
    "R3": "先例 - D-018/D-019, and flow already holds one of the two slots",
    "R4": "E5 - one promotion a window is a pace, not a measurement",
    "R5": "E5 - k=2/n=6; the false-stop rate is what EXP-G6' is for",
    "R6": "推导 - KILL-Q15: the engine builds its model once at startup, so a write needs a rollback",
    "R7": "E5 - three lives is a judgement call",
    "R8": "先例 (P13's ladder) + 推导 (KILL-AR-05: attributed P&L, not equity)",
    "R9": "推导 - the registry-digest instrument that closed KILL-Q15, applied to the policy",
    "R10": "推导 - a machine that can edit its own thresholds has no thresholds",
}


def policy_digest(policy: Policy | None = None) -> str:
    return (policy or Policy()).digest()
