"""T-D4-2 / M-011: a candidate that reads a metrics column may not reach the queue on an unproven column.

Research eats a T+1 daily archive; the live loop can only read a 30-day REST window.  The two carry
the same numbers under different stamps, and the whole same-source contract is one question - do they
agree on the buckets they share?  `metrics_parity` has answered it since DL-D2 and, until DL-D4,
nothing called it.

The obligation is to have SHOWN parity.  A missing report, an unmeasurable one, and a symbol with no
overlapping buckets are all "no" - not "not yet disproved" - because the failure mode they share is a
snapshot stream that stopped recording, which looks healthiest exactly when it is most broken.

The same failure one store over is an ARCHIVE that stopped, and it happened: nothing refreshed the
archive after its one ingest, so from 2026-09-09 every daily report re-compared the buckets of
2026-09-07 and said "agree".  An agreement is only as recent as the newest bucket it compared.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from beidou_governance.lifecycle import Book, Candidate, Event, Facts, State, apply
from beidou_governance.policy import Policy
from beidou_governance.scheduler import PARITY_MAX_AGE, parity_satisfied

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CLEAN = {
    "enforced": True,
    "symbols_compared": 18,
    "unmeasurable": [],
    "worst_differing_rate": 0.0,
    "compared_through": "2026-09-26T23:55:00+00:00",
}


def test_a_clean_parity_report_lets_the_candidate_through() -> None:
    ok, reason = parity_satisfied(CLEAN, now=NOW)
    assert ok and "18 symbols agree" in reason and "2026-09-26T23:55Z" in reason


def test_no_report_is_no_rather_than_not_yet_disproved() -> None:
    assert parity_satisfied(None, now=NOW) == (False, "M-011: no metrics parity report")
    assert not parity_satisfied({}, now=NOW)[0]


def test_an_unmeasurable_report_is_not_a_pass() -> None:
    """Zero disagreements out of zero comparisons is not agreement."""
    ok, reason = parity_satisfied(
        {"enforced": False, "reason": "no overlapping buckets for any of 18 symbols"}, now=NOW
    )
    assert not ok and "not measurable" in reason


def test_one_symbol_without_overlap_blocks_the_whole_universe() -> None:
    """A book trades a universe: parity the thinnest symbol does not have is not parity."""
    ok, reason = parity_satisfied({**CLEAN, "unmeasurable": ["CYSUSDT"]}, now=NOW)
    assert not ok and "1 symbols have no overlapping buckets" in reason


def test_any_disagreement_at_all_blocks() -> None:
    assert not parity_satisfied({**CLEAN, "worst_differing_rate": 0.001}, now=NOW)[0]
    assert not parity_satisfied({**CLEAN, "worst_differing_rate": None}, now=NOW)[0]


def test_a_report_that_does_not_say_how_recent_its_comparison_is_is_not_parity() -> None:
    """Every daily report before 2026-09-27 is this shape, including the seven that said "met"."""
    old_shape = {key: value for key, value in CLEAN.items() if key != "compared_through"}
    ok, reason = parity_satisfied(old_shape, now=NOW)
    assert not ok and "how recent" in reason


def test_an_agreement_older_than_the_limit_is_not_parity() -> None:
    """The 2026-09-26 reading: fifteen symbols agreeing about 2026-09-07, nineteen days on."""
    frozen = {**CLEAN, "compared_through": "2026-09-07T23:55:00+00:00"}
    ok, reason = parity_satisfied(frozen, now=datetime(2026, 9, 26, 20, 0, tzinfo=UTC))
    assert not ok and "2026-09-07T23:55Z" in reason

    edge = datetime.fromisoformat(CLEAN["compared_through"]) + PARITY_MAX_AGE
    assert parity_satisfied(CLEAN, now=edge)[0], "exactly the limit still counts"
    assert not parity_satisfied(CLEAN, now=edge + timedelta(minutes=1))[0]


def test_t_d4_2_the_candidate_stays_in_booked_until_parity_is_shown() -> None:
    """The state machine end of it: `booked -> queued` is the transition M-011 guards."""
    policy = Policy()
    book = Book(candidates={"mined_x": Candidate("mined_x", State.BOOKED)})

    blocked, decision = apply(book, "mined_x", Event.PARITY, Facts(parity_met=False), policy)
    assert blocked.candidates["mined_x"].state is State.BOOKED
    assert decision.reasons == ("M-011: the panel parity obligation is unmet",)

    met = parity_satisfied(CLEAN, now=NOW)[0]
    allowed, decision = apply(book, "mined_x", Event.PARITY, Facts(parity_met=met), policy)
    assert allowed.candidates["mined_x"].state is State.QUEUED
    assert decision.allowed
