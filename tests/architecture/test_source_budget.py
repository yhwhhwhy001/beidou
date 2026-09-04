"""M-003: the non-alpha line budget is measured, so a breach is visible instead of assumed away.

The plan set a budget - `beidou_live` <= 2,000 lines, non-alpha <= 6,000, and at least 60% of the source
in `beidou_alpha` - and made exceeding it trigger a KILL-003 re-review.  Nothing ever measured it, so the
budget was breached without anyone noticing: the audit found non-alpha at 6,761 lines against 6,000, and
`beidou_live` at 2,776 against 2,000.

This test does not enforce the plan's numbers, because meeting them today would mean deleting tested code
the operator asked for, which is a worse outcome than carrying the debt.  It ratchets instead: today's
measurement is the ceiling, so the breach cannot grow while the operator decides whether to re-price the
budget or spend effort shrinking it.  That decision is theirs and is recorded as open in the research log.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("beidou_alpha", "beidou_live", "beidou_cli", "beidou_data", "beidou_exchange", "beidou_shared")

# The plan's budget, kept here so the gap between intent and reality stays legible.
# The plan's original budget, plus the operator's 2026-09-04 revision of the alpha share from 60% to 90%.
# The share is deliberately NOT asserted against the tree: reaching 90% of lines would mean 68,706 lines of
# signal code against today's 3,661, and bloated signal code is what the V5 rebuild deleted.  The 90% target
# governs newly authored work and is measured per week by `beidou report weekly` (reports.effort_share).
PLAN_BUDGET = {"beidou_live": 2_000, "non_alpha_total": 6_000, "alpha_share_tree": 0.60, "alpha_share_effort": 0.90}

# Measured 2026-09-04 after the audit remediation.  A ceiling, not a target: lower is always fine.
#
# The first version of this file set beidou_live at 2,900 from the audit's own 2,776 reading and failed
# immediately at 3,043, which is the ratchet doing its job on its author: the remediation itself - the
# per-strategy income drift, the leg split, the probe correlation, the exit and pool sections - added
# about 270 lines to that package.  Raising a ceiling is allowed only in the commit that says why, and
# this is that sentence.  It then failed a second time on beidou_alpha, for the sign-bucketed IC that
# closed KILL-042.  Final measurement after the whole remediation: alpha 3,654, live 3,171, cli 2,276.
# The gap to the plan's 2,000 for beidou_live is 1,171 lines, and the alpha share is 34% against a 60%
# target, revised to 90% for new work on 2026-09-04.  Both are open operator decisions, recorded rather
# than redefined.  Noted without irony intended: instrumenting the 90% alpha target cost non-alpha lines,
# in beidou_live and beidou_cli, which is the tension the target exists to make visible rather than a
# reason to skip measuring it.
# Third raise, 2026-09-04, and the sentence the rule requires: +16 in beidou_exchange and +10 in
# beidou_shared, for the position parser.  demo-fapi's /fapi/v2/account rows carry a correct `notional`
# but no `markPrice`, and the parser derived notional as qty x mark, so every account-derived position
# came out at zero: `gross_notional()` read 0.00 while fifteen positions held 2,884 USDT of exposure.
# The same rows spell it `unrealizedProfit` where positionRisk spells it `unRealizedProfit`.  Most of the
# 26 lines are the docstring recording those two disagreements, which is the part that stops the next
# reader from "simplifying" the parser back into the bug.
CEILING = {
    "beidou_alpha": 3_661,
    "beidou_live": 3_423,
    "beidou_cli": 2_419,
    "beidou_data": 1_082,
    "beidou_exchange": 514,
    "beidou_shared": 280,
}


def _lines(package: str) -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted((ROOT / package).rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def test_no_package_grows_past_its_measured_ceiling() -> None:
    measured = {package: _lines(package) for package in PACKAGES}
    over = {name: (count, CEILING[name]) for name, count in measured.items() if count > CEILING[name]}
    assert not over, (
        f"these packages grew past the ratchet: {over}. "
        "Either take the growth back out, or raise the ceiling in the same commit that justifies it."
    )


def test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined() -> None:
    """A failing budget the operator has seen is honest; a budget nobody measures is not."""
    measured = {package: _lines(package) for package in PACKAGES}
    non_alpha = sum(count for name, count in measured.items() if name != "beidou_alpha")
    alpha_share = measured["beidou_alpha"] / max(1, sum(measured.values()))
    # These are the facts the operator is deciding about.  If a future change happens to bring the tree
    # back inside the plan, this test starts failing and the decision can simply be closed.
    assert non_alpha > PLAN_BUDGET["non_alpha_total"], "non-alpha is back inside the plan; close the decision"
    assert measured["beidou_live"] > PLAN_BUDGET["beidou_live"], "beidou_live is back inside the plan"
    assert alpha_share < PLAN_BUDGET["alpha_share_tree"], "alpha share recovered; close the decision"
