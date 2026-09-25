"""R10: a governance threshold may not move without somebody saying so.

The pin below is the whole mechanism.  Every number the machine promotes and demotes on lives in one
frozen dataclass, and `policy_digest()` hashes all of them; changing any one changes the digest, and
this test fails until the new digest is written here in the commit that justifies it.  Without the
pin, "the thresholds are fixed in advance" is a sentence in a docstring rather than a property.

It also holds the two facts a reader would otherwise have to take on trust: that every rule says
where it came from, and that three of the eleven are admitted judgement calls rather than
measurements.  A table of constants with no provenance column reads as though somebody measured them.
"""

from __future__ import annotations

import pytest

from beidou_governance.policy import POLICY_VERSION, PROVENANCE, Policy, policy_digest

# Phase 0, 2026-09-08 -> 0.2.0 the same day.  Raise this in the commit that changes a rule, never to
# make the test pass.  0.1.0 was "5787506aecdf"; it moved because R1 stopped charging a mine round by
# its row count, which is the rule change the version bump is for.
# 0.2.0 was "753638a519ac" -> 0.3.0 (2026-09-09): R1's budget opened by OPERATOR ruling, 170 -> 1700
# rows and 1 -> 4 mine rounds per window.  Legitimate on R1's own terms: its provenance is E5 (a
# judgement call anchored on "one mine round cost 514 rows"), and it is a RATE limit, not the
# multiple-testing control - R0's quantile gate is that, and it rises monotonically with N, so
# searching more raises the bar by itself.  R2/R3/R4/R5 untouched.
# 0.3.0 was "35e749f7fc0c" -> 0.3.1 (2026-09-10): 4 -> 5 mine rounds, for the window ending
# 2026-10-03 ONLY, by operator ruling.  The 09-09 round raced `com.beidou.data` and loaded its panel
# before any spot parquet landed, so `searched_basis` read False and the 18 basis shapes were never
# enumerated; this buys the round that enumerates them, priced at 243 candidates and a `mined` bucket
# of 2,488 -> 2,731 (D-028 gate 1.7990 -> 1.8085).  What makes "one window" more than a sentence is
# `test_the_single_window_mine_opening_is_returned`, which fails from that date until it goes back.
# Moved 2026-09-12 (0.3.1 -> 0.3.2): `queue_order = "fifo"`.  Not a threshold - it is the ordering
# §3's `queued -> probe` means by "队首", which the state did not record at all, so `queue_head`
# refused outright whenever two candidates were queued.  FIFO needs no further judgement; every
# alternative is a selection rule needing its own pre-registration.  Recorded in policy so that
# changing it later is a rule version change rather than an edit.
# 2026-09-14: R8's two rungs moved (POLICY_VERSION 0.3.2 -> 0.3.3).  The old digest was
# 5200c9c98136.
# 0.3.4 (2026-09-14): R2b, `mine_requires_gate_below_best`.  `research mine` now also stops when
# another round could not produce an admissible candidate - the D-028 gate rises monotonically in the
# bucket's N, so once it has passed the best out-of-sample Sharpe the space ever produced, a further
# round can only raise it.  That crossing happened on 2026-09-09 and two more rounds ran after it.
# It carries no threshold of its own (both sides are read off artefacts, `gate_has_passed_the_space`)
# and it refuses SPENDING only - no verdict, no promotion, nothing that reaches the book.
# 0.3.5 (2026-09-14): caliber ④, `trial_range_end_granularity_days = 7`.  Operator ruling on Q4c.  Two
# ledger rows that differ only by a `range_end` a few days apart are one trial: a signal reads only
# data up to bar t, so the same expression over the same start, symbols and construction gives an
# IDENTICAL stream on the shared index, and the added independence is exactly zero - arithmetic, not an
# estimate.  Not a reason to drop the field, because the same holds for two years later and that IS a
# second look, so the fold needs a granularity.  7 folds least among the ones that fold the observed
# case, and the choice is not load-bearing here: 7 / 14 / 30 all give `mined` 1,559, `tsmom` 101,
# `flow` 43.  The measured half of Q4c went the other way - the second universe is worth 1.90x, so
# cross-universe re-charges stay charged.  The old digest was 75764f646ca6.
# 0.3.6（2026-10-13 的切换）：R8 的两档随 k 0.60 -> 0.175 按可动用口径重推，
# ((-0.49, 0.45), (-0.70, 0.30)) -> ((-0.2803, 0.13125), (-0.4005, 0.0875))。理由写在 POLICY_VERSION。
# 旧摘要 d62ac59fa95c。#148 关闭时带走的那个 0.3.6（89e19b1706b4）是另一份规则，从没进过 main。
PINNED_DIGEST = "9cc96461276f"


def test_the_digest_is_pinned_so_a_threshold_cannot_move_quietly() -> None:
    assert policy_digest() == PINNED_DIGEST, (
        "a governance threshold changed.  That is allowed - R10 asks only that it be deliberate: "
        "bump POLICY_VERSION, write the new digest here, and say in the commit message which rule "
        "moved and why."
    )


def test_every_rule_says_where_it_came_from() -> None:
    assert set(PROVENANCE) == {f"R{i}" for i in range(11)}
    for rule, source in PROVENANCE.items():
        assert source.split(" ")[0] in {"推导", "先例", "E5"}, f"{rule} has no source class"


def test_the_judgement_calls_are_labelled_as_such() -> None:
    """Four rules have no evidence behind them, and the table has to keep saying so."""
    e5 = {rule for rule, source in PROVENANCE.items() if source.startswith("E5")}
    assert e5 == {"R1", "R4", "R5", "R7"}, f"the set of admitted judgement calls changed: {sorted(e5)}"


@pytest.mark.parametrize(
    ("drawdown", "expected"),
    [(0.0, None), (-0.28, None), (-0.2803, 0.13125), (-0.40, 0.13125), (-0.4005, 0.0875), (-0.90, 0.0875)],
)
def test_the_ladder_takes_the_deepest_rung_that_applies(drawdown: float, expected: float | None) -> None:
    """A -90% drawdown must get the second rung's target, not the first one it also qualifies for.

    2026-09-14: the rungs moved from (-0.35, 0.225) / (-0.50, 0.15) to (-0.49, 0.45) / (-0.70, 0.30),
    re-derived by the shipped rule for the -70% budget declared with `vol_target` 0.60.  The cases
    below moved with them; what this test is about - deepest rung wins, and nothing above the first -
    did not.

    2026-10-13（policy 0.3.6）：两档随 k 0.175 与可动用口径的预算移到 (-0.2803, 0.13125) / (-0.4005, 0.0875)，
    各个样例跟着移，测的还是同一件事。
    """
    assert Policy().throttle_scalar(drawdown) == expected


def test_the_window_and_the_clean_record_are_the_same_length() -> None:
    """K-EX14 has zero slack against a monthly window, and that is why it is an admission condition.

    `construction_fingerprint` includes `strategy_weights`, so every promotion resets M-010's clock.
    If these two numbers ever differ, the reasoning in `lifecycle` about promotion being an
    opportunity rather than an obligation needs rewriting rather than adjusting.
    """
    policy = Policy()
    assert policy.window_days == policy.min_clean_days_before_promotion
    assert policy.version == POLICY_VERSION
