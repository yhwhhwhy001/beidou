"""Phase 0 item B: R0-R10 over synthetic event sequences, not over the one history we have.

The replay (item A) answers "do the rules reproduce what happened".  It cannot answer "can the rules
be made to do something forbidden", because history only contains the sequences a careful person
actually produced.  These properties answer that: hypothesis drives arbitrary events at the state
machine and asserts the invariants that make autonomous promotion survivable.

The invariants are the ones whose failure is expensive rather than the ones that are easy to state.
R3's two probe slots and 1/3 budget bound what a wrong rule can cost; R7's three lives bound how
long it can keep costing it; RETIRED being absorbing is what stops a demotion loop, which is the
failure the operator explicitly asked to be protected against.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from beidou_governance.lifecycle import Book, Candidate, Event, Facts, State, apply, evaluate
from beidou_governance.policy import Policy

POLICY = Policy()
IDS = ("a", "b", "c")

facts = st.builds(
    Facts,
    verdict_pass=st.booleans(),
    prereg_before_report=st.booleans(),
    quantile_gate_pass=st.booleans(),
    evidence_construction_matches_live=st.booleans(),
    book_checks_pass=st.booleans(),
    slippage_stress_pass=st.booleans(),
    max_correlation_with_running=st.floats(0.0, 1.0),
    turnover_ratio_to_main=st.floats(0.0, 10.0),
    parity_met=st.booleans(),
    window_open=st.booleans(),
    is_queue_head=st.booleans(),
    canary_healthy=st.booleans(),
    clean_days_under_current_construction=st.integers(0, 60),
    no_decision=st.booleans(),
)
steps = st.lists(
    st.tuples(st.sampled_from(IDS), st.sampled_from(list(Event)), facts, st.booleans()),
    min_size=1,
    max_size=60,
)


def _run(sequence: list[tuple[str, Event, Facts, bool]]) -> list[Book]:
    """Drive the machine and keep every intermediate book, because the invariants are per-step."""
    book = Book(candidates={i: Candidate(id=i, fraction=POLICY.probe_budget_share / 2) for i in IDS})
    seen = [book]
    for candidate_id, event, fact, roll_window in sequence:
        book, _ = apply(book, candidate_id, event, fact, POLICY)
        seen.append(book)
        if roll_window:
            book = book.open_next_window()
            seen.append(book)
    return seen


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_r3_bounds_what_a_wrong_rule_can_cost(sequence: list[tuple[str, Event, Facts, bool]]) -> None:
    for book in _run(sequence):
        assert len(book.probes) <= POLICY.max_concurrent_probes
        assert book.probe_fraction <= POLICY.probe_budget_share + 1e-9


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_r4_lets_at_most_one_candidate_in_and_one_up_per_window(
    sequence: list[tuple[str, Event, Facts, bool]],
) -> None:
    for book in _run(sequence):
        assert book.promotions_this_window <= POLICY.max_queued_to_probe_per_window
        assert book.to_main_this_window <= POLICY.max_probe_to_main_per_window


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_r7_gives_three_lives_and_no_more(sequence: list[tuple[str, Event, Facts, bool]]) -> None:
    for book in _run(sequence):
        for candidate in book.candidates.values():
            assert candidate.probe_entries <= POLICY.max_probe_entries_lifetime


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_retired_is_absorbing_so_a_demotion_loop_cannot_form(
    sequence: list[tuple[str, Event, Facts, bool]],
) -> None:
    retired: set[str] = set()
    for book in _run(sequence):
        for name, candidate in book.candidates.items():
            if candidate.state is State.RETIRED:
                retired.add(name)
            else:
                assert name not in retired, f"{name} left RETIRED"


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_a_no_decision_cycle_changes_nothing(sequence: list[tuple[str, Event, Facts, bool]]) -> None:
    """KILL-AR-20: an ERROR phase or a rebaselined cycle must not be able to move any state."""
    book = Book(candidates={i: Candidate(id=i) for i in IDS})
    for candidate_id, event, fact, _ in sequence:
        blind = Facts(**{**fact.__dict__, "no_decision": True})
        after, decision = apply(book, candidate_id, event, blind, POLICY)
        assert after == book
        assert not decision.allowed


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_every_refusal_names_something(sequence: list[tuple[str, Event, Facts, bool]]) -> None:
    """T-G0-2's property, at the source: a refusal with no rule attached cannot be attributed."""
    book = Book(candidates={i: Candidate(id=i) for i in IDS})
    for candidate_id, event, fact, _ in sequence:
        decision = evaluate(book, book.candidates[candidate_id], event, fact, POLICY)
        if not decision.allowed:
            assert decision.reasons, "a refusal produced no reason"
            for reason in decision.reasons:
                assert reason.split(":")[0].strip(), reason


def test_a_probe_reaching_main_needs_nine_uninterrupted_windows() -> None:
    """The one path worth spelling out by hand: eight windows and a stop is not eight-ninths there."""
    policy = POLICY
    book = Book(candidates={"a": Candidate("a", State.PROBE, fraction=policy.probe_budget_share)})
    for _ in range(policy.windows_to_main - 1):
        book = book.open_next_window()
        book, _ = apply(book, "a", Event.WINDOW_SURVIVED, Facts(), policy)
    assert book.candidates["a"].state is State.PROBE
    book, _ = apply(book, "a", Event.PNL_STOP, Facts(), policy)
    assert book.candidates["a"].windows_survived == 0
    assert book.candidates["a"].state is State.QUEUED
