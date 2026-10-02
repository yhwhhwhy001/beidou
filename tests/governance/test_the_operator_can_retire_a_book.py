"""D-048 (operator ruling 2026-10-01): a written operator ruling retires a book, and the record says so.

Flow was ruled retired on 2026-09-30, effective with the 10-03 restart.  The state machine had one way into
RETIRED - a P&L stop on R7's last probe entry - so the record would have kept flow a probe, holding a probe
slot, for as long as nobody hand-edited the state; and a hand edit does not survive the next
`advance --commit`.  These pin the three halves: what the rule does, where the event comes from
(`governance/rulings.jsonl`, not the machine's verdicts and not the registry's transaction chain), and that
`advance` folds it once, in time order.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from beidou_cli.governance_cmd import governance
from beidou_governance.lifecycle import Book, Candidate, Event, Facts, State, apply
from beidou_governance.policy import Policy
from beidou_governance.rulings import RETIRE, Ruling, read, record, retirements
from beidou_governance.state import read as read_state
from beidou_governance.state import write as write_state

POLICY = Policy()
#: Far enough back that every window below has closed against the real clock `tenure` reads.
ANCHOR = "2024-01-01T00:00:00+00:00"
CLEAN: dict[str, Any] = {"by_type": {}, "rebaselined": False, "rows": 0, "total": 0.0}
WHERE = "RESEARCH_LOG 2026-09-30 「操作者四条裁定：backtest-guard 09-30 体检的四张卡」"


def _book() -> Book:
    return Book(
        candidates={
            "tsmom": Candidate("tsmom", State.MAIN),
            "flow": Candidate("flow", State.PROBE, probe_entries=1, fraction=1 / 3),
        }
    )


# --- the rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state", [State.PROBE, State.MAIN, State.QUEUED, State.BOOKED])
def test_a_ruling_retires_from_any_live_state_and_spends_no_counter(state: State) -> None:
    book = Book(candidates={"flow": Candidate("flow", state, probe_entries=1)})
    after, decision = apply(book, "flow", Event.OPERATOR_RETIRE, Facts(), POLICY)
    assert decision.allowed and after.candidates["flow"].state is State.RETIRED, decision
    assert decision.reasons[0].startswith("D-048")
    assert after.consecutive_probe_stops == book.consecutive_probe_stops, "not a stop: R5 does not count it"
    assert after.candidates["flow"].probe_entries == 1, "not a life: R7 does not count it"


def test_retired_stays_absorbing() -> None:
    book = Book(candidates={"flow": Candidate("flow", State.RETIRED)})
    after, decision = apply(book, "flow", Event.OPERATOR_RETIRE, Facts(), POLICY)
    assert not decision.allowed and after.candidates["flow"].state is State.RETIRED
    assert decision.reasons[0].startswith("R7")


def test_a_retired_probe_frees_its_slot_and_its_share() -> None:
    after, _ = apply(_book(), "flow", Event.OPERATOR_RETIRE, Facts(), POLICY)
    assert len(after.probes) == 0 and after.probe_fraction == 0.0


# --- where the event comes from ---------------------------------------------------------------


def test_only_this_strategys_retirements_become_events_oldest_first(tmp_path: Path) -> None:
    path = tmp_path / "rulings.jsonl"
    for at, kind, subject in [
        ("2026-10-05T00:00:00+00:00", RETIRE, "flow"),
        ("2026-10-03T05:10:00+00:00", RETIRE, "flow"),
        ("2026-10-04T00:00:00+00:00", RETIRE, "tsmom"),
        ("2026-10-02T00:00:00+00:00", "some_other_kind", "flow"),
    ]:
        record(path, Ruling(at=at, kind=kind, subject=subject, reasons=(WHERE,)))
    assert [r.actor for r in read(path)] == ["operator"] * 4
    events = retirements(read(path), "flow")
    assert [event.at for event in events] == ["2026-10-03T05:10:00+00:00", "2026-10-05T00:00:00+00:00"]
    assert {event.event for event in events} == {Event.OPERATOR_RETIRE}
    assert all(WHERE in event.why for event in events), "the event names where the ruling is written"


# --- the command, and `advance` folding what it wrote ----------------------------------------------


def _cycle(day: float) -> dict[str, Any]:
    at = datetime.fromisoformat(ANCHOR) + timedelta(days=day)
    return {
        "at": at.isoformat(),
        "probes": [
            {"book": "main", "strategy": "tsmom", "stop": False, "status": "OK", "pnl_pct": 0.001},
            {"book": "flow_short", "strategy": "flow", "stop": False, "status": "OK", "pnl_pct": 0.0},
        ],
        "external_flows": dict(CLEAN),
    }


def _checkout(tmp_path: Path, *, flow_enabled: bool) -> Path:
    root = tmp_path / "checkout"
    (root / "governance").mkdir(parents=True)
    write_state(root / "governance" / "governance_state.json", _book())
    rows = [_cycle(index * POLICY.window_days + 1) for index in range(4)]
    (root / "cycles.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    registry = {
        "version": 1,
        "ensemble": {"method": "mean", "turnover_penalty": 0.0},
        "books": {"flow_short": {"fraction": 0.333333}},
        "strategies": [
            {"id": "tsmom", "enabled": True, "weight": 1.0},
            {"id": "flow", "enabled": flow_enabled, "weight": 1.0, "book": "flow_short"},
        ],
    }
    (root / "registry.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
    return root


def _retire(root: Path, *extra: str) -> Any:
    at = (datetime.fromisoformat(ANCHOR) + timedelta(days=45)).isoformat()
    args = ["retire", "flow", "--root", str(root), "--registry", str(root / "registry.yaml"), "--ruling", WHERE]
    return CliRunner().invoke(governance, [*args, "--at", at, *extra])


def _advance(root: Path, *extra: str) -> Any:
    args = ["advance", "--root", str(root), "--cycles", str(root / "cycles.jsonl")]
    return CliRunner().invoke(
        governance, [*args, "--registry", str(root / "registry.yaml"), "--anchor", ANCHOR, *extra]
    )


def test_the_command_refuses_while_the_registry_still_enables_the_book(tmp_path: Path) -> None:
    root = _checkout(tmp_path, flow_enabled=True)
    result = _retire(root, "--commit")
    assert result.exit_code != 0 and "still enables flow" in result.output, result.output
    assert not (root / "governance" / "rulings.jsonl").exists()


def test_the_command_refuses_a_book_the_state_does_not_carry(tmp_path: Path) -> None:
    root = _checkout(tmp_path, flow_enabled=False)
    args = ["retire", "carry", "--root", str(root), "--registry", str(root / "registry.yaml"), "--ruling", WHERE]
    result = CliRunner().invoke(governance, [*args, "--commit"])
    assert result.exit_code != 0 and "no candidate to retire" in result.output, result.output


def test_a_dry_run_writes_nothing_and_a_commit_writes_one_row(tmp_path: Path) -> None:
    root = _checkout(tmp_path, flow_enabled=False)
    dry = _retire(root)
    assert dry.exit_code == 0 and "dry run" in dry.output, dry.output
    assert not (root / "governance" / "rulings.jsonl").exists()
    committed = _retire(root, "--commit")
    assert committed.exit_code == 0, committed.output
    rows = read(root / "governance" / "rulings.jsonl")
    assert len(rows) == 1 and rows[0].kind == RETIRE and rows[0].subject == "flow" and rows[0].actor == "operator"
    assert rows[0].reasons == (WHERE,)
    assert read_state(root / "governance" / "governance_state.json") == _book(), "the command writes no state"


def test_advance_folds_the_retirement_once_and_frees_the_slot(tmp_path: Path) -> None:
    root = _checkout(tmp_path, flow_enabled=False)
    assert _retire(root, "--commit").exit_code == 0
    first = _advance(root, "--commit")
    assert first.exit_code == 0, first.output
    assert "operator_retire" in first.output, first.output
    book = read_state(root / "governance" / "governance_state.json")
    assert book.candidates["flow"].state is State.RETIRED, first.output
    assert book.candidates["tsmom"].state is State.MAIN
    assert len(book.probes) == 0
    once = (root / "governance" / "governance_state.json").read_bytes()
    again = _advance(root, "--commit")
    assert again.exit_code == 0, again.output
    assert (root / "governance" / "governance_state.json").read_bytes() == once, "a second fold changed the state"
