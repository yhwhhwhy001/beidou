"""The research loop had no reason to ever stop mining, and by 2026-09-09 it should have.

`next_action` returns MINE whenever R2's condition holds (the space digest moved) and R1 still has a
round.  Nothing in it looks at whether another round could produce an admissible candidate.  It could
not, and had not been able to for days: the `mined` bucket's D-028 threshold rises monotonically with
N, and by 2026-09-09 it had risen past the out-of-sample Sharpe of the best candidate the space has
ever produced.  Two more rounds ran after that.

The condition here is DERIVED, not chosen - which is the only reason it may live in a scheduler that
otherwise holds no thresholds of its own (`test_policy_is_the_only_place_a_threshold_lives`).  Both
sides of it are measurements already on disk:

* the best mined candidate's `oos_selection.oos_sharpe_annual`, the `variance` of that same estimate,
  and the `threshold_annual`/`n_trials` pair its own report recorded - which together pin the
  annualisation exactly, so nothing here has to assume a bars-per-year;
* the bucket's N today.

Recomputing that candidate's own gate at today's N answers "would the best thing this space has ever
produced still be admissible?".  When the answer is no, another round cannot help - it can only raise
the bar further - so WAIT, with that sentence as the reason.

It refuses SPENDING, never promotion.  Nothing here touches a verdict, `n_trials`, or what may reach
the book; an append-only ledger makes not-spending the reversible direction, and mining resumes by
itself the moment a wider space or a better candidate moves either number.
"""

from __future__ import annotations

from beidou_governance.assemble import gate_has_passed_the_space
from beidou_governance.policy import Policy
from beidou_governance.scheduler import MINE, WAIT, Context, next_action


def _mineable(**overrides: object) -> Context:
    """A context whose only available action is MINE, so a WAIT can only come from the new condition."""
    return Context(search_space_digest="new", last_mined_space_digest="old", **overrides)  # type: ignore[arg-type]


def test_the_loop_still_mines_while_the_gate_is_below_the_spaces_best() -> None:
    assert next_action(_mineable(mined_gate_passed_best=0)).kind == MINE


def test_the_loop_stops_mining_once_the_gate_passes_the_spaces_best() -> None:
    action = next_action(_mineable(mined_gate_passed_best=1))

    assert action.kind == WAIT
    assert action.reasons, "scheduler's own rule: a WAIT always carries its reason"
    assert "gate" in " ".join(action.reasons).lower()


def test_the_stop_can_be_turned_off_by_policy() -> None:
    """A threshold-free rule is still a rule, and every rule here has to be switchable from `Policy`."""
    off = Policy(mine_requires_gate_below_best=False)

    assert next_action(_mineable(mined_gate_passed_best=1), off).kind == MINE


def test_the_stop_does_not_block_the_work_already_in_hand() -> None:
    """VALIDATE/BOOK/PARITY/QUEUE rank above MINE; a candidate already found must not be stranded."""
    stranded = _mineable(mined_gate_passed_best=1, validated_unbooked=1)

    assert next_action(stranded).kind == "book"


def test_a_space_with_no_validated_candidate_yet_is_not_stopped() -> None:
    """Nothing to compare against is not the same fact as "the bar has passed it"."""
    passed, why = gate_has_passed_the_space([], n_trials=2731)

    assert passed is False
    assert "no" in why.lower()


def test_the_best_candidate_of_2026_09_06_would_not_clear_todays_bucket() -> None:
    """The real numbers: `mined_594a12f9307a15d9` passed at N=575 and the bucket is now 2,731."""
    report = {
        "verdict": "PASS",
        "oos_selection": {
            "oos_sharpe_annual": 1.786223252863809,
            "variance": 2.200184145624143e-05,
            "threshold_annual": 1.645341936067292,
            "n_trials": 575,
        },
    }

    passed_then, _ = gate_has_passed_the_space([report], n_trials=575)
    passed_now, why = gate_has_passed_the_space([report], n_trials=2731)

    assert passed_then is False, "it cleared its own gate on the day it was validated"
    assert passed_now is True, "and would not clear the same gate at today's N"
    assert "1.78" in why and "2731" in why, f"the reason has to carry both numbers: {why}"


def test_the_comparison_uses_the_best_candidate_not_the_newest() -> None:
    """A later, worse candidate must not make the space look exhausted."""
    pinned = {"variance": 2.200184145624143e-05, "threshold_annual": 1.645341936067292, "n_trials": 575}
    good = {"oos_selection": {"oos_sharpe_annual": 2.5, **pinned}}
    poor = {"oos_selection": {"oos_sharpe_annual": 0.4, **pinned}}

    passed, _ = gate_has_passed_the_space([poor, good], n_trials=2731)

    assert passed is False, "2.5 still clears the bar at N=2731; the space is not exhausted"


def test_a_report_without_the_block_is_skipped_not_guessed() -> None:
    """Reports written before `oos_selection` existed carry no opinion about this."""
    passed, why = gate_has_passed_the_space([{"verdict": "PASS"}], n_trials=2731)

    assert passed is False
    assert "no" in why.lower()
