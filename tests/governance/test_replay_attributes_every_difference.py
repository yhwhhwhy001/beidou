"""AC-G0: the replay's difference list has no unattributed item, and `evidence_gap` is not a hole.

Two tests carry the acceptance and one carries the register.

T-G0-1 is the register: the seven rulings Phase 0 was told to account for are declared, each with the
rule it conflicts with and the reason it stays an exception rather than becoming a rule.  A register
whose entries only said "this happened" would let anything in.

T-G0-2 is the acceptance, and its teeth are in `Difference.attributed`: an `evidence_gap` counts as
attributed only when it names the fix that would close it.  Without that clause "we cannot tell"
becomes a wastebasket and AC-G0 passes by construction, which is the failure mode of every audit that
grades itself.

The adoptions below are real - report paths the registry has actually cited, with the dates it cited
them - written out rather than derived from `git log`, so the test says the same thing in a shallow
clone as in a full one.  The CLI derives the full set from history; this is the shape check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beidou_governance.replay import (
    EVIDENCE_GAP,
    EXCEPTIONS,
    EXCEPTIONS_BY_ID,
    SUSPENDED,
    Difference,
    load_jsonl,
    render,
    replay_adoptions,
    replay_live,
)

ROOT = Path(__file__).resolve().parents[2]

# One of each shape the archive actually contains.
ADOPTIONS = {
    "reports/research/tsmom-validation-20260903T0619Z.json": "2026-09-03",  # predates the D-028 block
    "reports/research/book-tsmom-flow-20260903T143621Z.json": "2026-09-04",  # book, ACCEPT
    "reports/research/tsmom-validation-20260906T093705Z.json": "2026-09-06",  # has the block, unnamed gate
    "reports/research/book-tsmom-flow-20260908T105322Z.json": "2026-09-08",  # book, acknowledged REJECT
    "reports/research/tsmom-validation-20260908T105259Z.json": "2026-09-08",  # names its gate
}
ACKNOWLEDGED = ("book-tsmom-flow-20260908T105322Z.json",)


def _reports() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted((ROOT / "reports" / "research").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(payload, dict):
            out[path.relative_to(ROOT).as_posix()] = payload
    return out


@pytest.mark.parametrize("ruling", ["D-019", "D-029", "K-EX07", "Q7", "P10-cellB", "P11", "P13"])
def test_t_g0_1_the_seven_rulings_are_in_the_exception_list(ruling: str) -> None:
    entry = EXCEPTIONS_BY_ID[ruling]
    assert entry.ruling and entry.rule_conflict and entry.why_not_encoded, f"{ruling} is a stub"
    assert entry.date.startswith("2026-"), f"{ruling} has no date"


def test_an_exception_entry_has_to_say_which_rule_it_breaks() -> None:
    """Otherwise the register lists history rather than the cost of encoding it."""
    for entry in EXCEPTIONS:
        assert any(token in entry.rule_conflict for token in ("R", "§", "D-", "DL-")), entry.id


def test_t_g0_2_every_difference_carries_a_named_cause() -> None:
    result = replay_adoptions(_reports(), ADOPTIONS, acknowledged_rejects=ACKNOWLEDGED)
    assert result.differences, "a replay that finds no difference is not replaying anything"
    for difference in result.differences:
        assert difference.attributed, f"unattributed: {difference.subject} / {difference.rules_say}"
    assert result.passes_ac_g0


def test_an_evidence_gap_without_a_fix_is_not_attributed() -> None:
    """The clause that stops AC-G0 from grading itself."""
    hole = Difference("x", "adopted", "rule says no", EVIDENCE_GAP, "cannot tell", fix="")
    closed = Difference("x", "adopted", "rule says no", EVIDENCE_GAP, "cannot tell", fix="write the field")
    assert not hole.attributed
    assert closed.attributed


def test_the_one_report_that_names_its_gate_is_the_one_the_rules_admit() -> None:
    """The replay's sharpest single finding, pinned so it cannot quietly become "all of them"."""
    result = replay_adoptions(_reports(), ADOPTIONS, acknowledged_rejects=ACKNOWLEDGED)
    admitted = [line.split("：")[0] for line in result.reproduced if "规则同意采纳（validate" in line]
    assert admitted == ["tsmom-validation-20260908T105259Z.json"]


def test_a_suspended_condition_says_what_would_make_it_readable() -> None:
    for condition in SUSPENDED:
        assert condition.fix.startswith("Phase "), f"{condition.condition} has no owner phase"
        assert condition.reads.startswith("Facts."), condition.condition


def test_the_live_replay_refuses_to_decide_on_an_error_cycle() -> None:
    """KILL-AR-20 against a hand-built record: one ERROR cycle, one rebaseline, one clean cycle."""
    cycles = load_jsonl(
        "\n".join(
            json.dumps(row)
            for row in (
                {"at": "2026-01-01T00:00:00+00:00", "construction": "aaaaaaaaaaaa", "probes": []},
                {"at": "2026-01-01T01:00:00+00:00", "phase": "ERROR", "error": "proxy 503"},
                {
                    "at": "2026-01-01T02:00:00+00:00",
                    "construction": "aaaaaaaaaaaa",
                    "external_flows": {"rebaselined": True},
                },
            )
        )
    )
    result = replay_live(cycles, [{"total": -1.0}])
    assert any("ERROR 1" in line and "重基 1" in line for line in result.reproduced)
    assert not [d for d in result.differences if "construction" in d.subject], "no construction changed"
    assert result.passes_ac_g0


def test_the_artefact_states_its_own_verdict() -> None:
    text = render(
        replay_adoptions(_reports(), ADOPTIONS, acknowledged_rejects=ACKNOWLEDGED),
        replay_live([], []),
    )
    assert "AC-G0：未归因项 0 条" in text
    assert "policy_digest" in text
