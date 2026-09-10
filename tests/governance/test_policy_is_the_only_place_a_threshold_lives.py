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
PINNED_DIGEST = "08dcceaa6bee"


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
    [(0.0, None), (-0.34, None), (-0.35, 0.225), (-0.49, 0.225), (-0.50, 0.15), (-0.80, 0.15)],
)
def test_the_ladder_takes_the_deepest_rung_that_applies(drawdown: float, expected: float | None) -> None:
    """A -60% drawdown must get 0.15, not the 0.225 it also qualifies for."""
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
