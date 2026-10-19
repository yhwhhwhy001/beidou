"""KILL-AR-06's grandfather, written down -- because an empty book PERMITS, it does not refuse.

`state.py` says an unreadable state is an empty book, and that this is safe because "an empty book has
no probes, so every rule that could act on it refuses for want of a candidate".  That is true only of
the candidate being evaluated.  Every rule that reads the book for CONSTRAINTS reads an empty one as
headroom: `len(book.probes)` is 0 against R3's 2, and `book.probe_fraction` is 0.0 against the 1/3
cap.  Measured 2026-09-09 with no state file on the trading machine: a second 1/3 probe was ALLOWED
on top of the flow probe that was already running live -- 2/3 of the book against a 1/3 cap, and no
rule anywhere would have said a word.

So the two sleeves the plan grandfathered have to be IN the state, not merely true of the world.
"""

from __future__ import annotations

import json
from pathlib import Path

from beidou_governance.lifecycle import Book, Candidate, Event, Facts, State, evaluate
from beidou_governance.policy import Policy
from beidou_governance.state import read as read_state

STATE = Path("governance/governance_state.json")


def test_the_shipped_state_holds_the_running_main_and_records_flow_retired() -> None:
    """KILL-AR-06, post-retirement: the running sleeve is IN the state, and the retired one is recorded.

    Until 2026-09-29 this also pinned tsmom as main, the seed's value.  That day `governance advance --commit`
    folded the 2026-09-19 family-gate refusal (10-13 ruling table #8) and tsmom went main -> probe under the
    operator's 2026-09-23 rule; the loop trades as it did.  What the seed exists for is that neither sleeve
    reads as headroom, and a demoted main is still a running sleeve holding one of R3's two slots.

    Later the same day the operator ruled that refusals count per evidence.  Both refusals had judged a
    report the registry stopped citing on 09-27, so the state went back to main.  PROBE stays allowed:
    a refusal of the evidence the registry cites today still demotes it.

    2026-10-19 the operator retired flow (D-048, `governance retire flow` + `advance --commit`): it left
    probe -> retired, released its 1/3 share, and freed its R3 slot.  tsmom stays main.  This test pins the
    post-retirement shape: the running sleeve is in the state, the retired one is recorded (not headroom),
    and R7's lifetime `probe_entries` survives the retirement.
    """
    book = read_state(STATE)
    assert set(book.candidates) >= {"tsmom", "flow"}, "the running sleeve is not in the governance state"
    assert book.candidates["tsmom"].state in {State.MAIN, State.PROBE}, "tsmom runs; it cannot read as gone"
    assert book.candidates["flow"].state is State.RETIRED
    # retirement releases the 1/3 share AND its R3 slot; §3 says a second probe no longer waits on flow.
    assert book.candidates["flow"].fraction == 0.0
    assert book.candidates["flow"].probe_entries == 1, "R7 counts entries on entry, and flow entered once"


def test_after_flows_retirement_a_probe_slot_is_free() -> None:
    """The measurement the seed existed for, updated for its 2026-10-19 retirement.

    Before the retirement flow occupied one of R3's two slots AND the whole 1/3 share, so a second probe
    was refused (the `seeded` arm asserted `not allowed` with an R3 reason).  `governance retire flow`
    freed the slot, so a second probe is now admitted -- exactly the headroom the retirement was meant to
    create.  The `empty` arm still documents the original finding (an empty book PERMITS).
    """
    policy = Policy()
    facts = Facts(window_open=True, is_queue_head=True, canary_healthy=True, clean_days_under_current_construction=30)
    newcomer = Candidate(id="newcomer", state=State.QUEUED, fraction=policy.probe_budget_share)

    empty = evaluate(Book(candidates={"newcomer": newcomer}), newcomer, Event.PROMOTE, facts, policy)
    assert empty.allowed, "if this now refuses, the seed is no longer what is holding R3 up - say so"

    seeded = evaluate(read_state(STATE), newcomer, Event.PROMOTE, facts, policy)
    assert seeded.allowed
    assert not any("R3" in reason for reason in seeded.reasons), seeded.reasons


def test_the_state_file_is_tracked_rather_than_ignored() -> None:
    """A record one disk failure from never having existed is not a record.

    The first version of the autonomy-switch ignore took the whole `governance/` directory, and with
    it the transaction chain (`closed(log)` is what proves no registry change happened outside a
    transaction) and R7's lifetime `probe_entries`, which nothing else can reconstruct.
    """
    ignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "\ngovernance/ENABLED\n" in ignore, "the switch must stay per-checkout"
    assert "\ngovernance/\n" not in ignore, "ignoring the directory takes the transaction chain with it"


def test_the_state_on_disk_is_plain_enough_to_read_during_an_incident() -> None:
    payload = json.loads(STATE.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["candidates"]) >= {"tsmom", "flow"}
