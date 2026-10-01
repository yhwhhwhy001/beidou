"""D-048: operator rulings the state machine folds - written, append-only, kept apart from the machine's verdicts.

`governance/verdicts.jsonl` records what the MACHINE ruled, so a person can review it afterwards (M-G05's
divergence).  `governance/transactions.jsonl` records every write to the registry as one closed chain (AC-G4).
An operator ruling is neither.  A person decided and the machine did not, so in the first file it would count
toward the machine's disagreement rate.  It writes no registry by itself, so in the second its digest would name
a registry a PR had changed, and the chain would read as broken.  Hence a file of its own, which
`governance advance` reads the way it reads the gate's refusals.

One kind today, `operator_retire`: the operator retires a book outright (ruling 2026-10-01, on flow's retirement
ruled 2026-09-30).  `governance retire` writes the row; `governance advance --commit` folds it into the state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from beidou_governance.lifecycle import Event
from beidou_governance.tenure import Derived

LEDGER = "governance/rulings.jsonl"
RETIRE = "operator_retire"


@dataclass(frozen=True)
class Ruling:
    """One written operator ruling: when it took effect, what it ruled on, and where it is written."""

    at: str
    kind: str
    subject: str
    reasons: tuple[str, ...] = ()
    actor: str = "operator"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Ruling | None:
        try:
            payload = json.loads(line)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not payload.get("kind") or not payload.get("subject"):
            return None
        return cls(
            at=str(payload.get("at", "")),
            kind=str(payload["kind"]),
            subject=str(payload["subject"]),
            reasons=tuple(str(r) for r in (payload.get("reasons") or ())),
            actor=str(payload.get("actor", "operator")),
        )


def read(path: Path) -> list[Ruling]:
    if not path.exists():
        return []
    rows = (Ruling.from_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return [row for row in rows if row is not None]


def record(path: Path, ruling: Ruling) -> None:
    """Append-only, like the verdicts: a ruling once written is history, and a reversal is a new row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(ruling.to_json() + "\n")


def retirements(rulings: Iterable[Ruling], strategy: str) -> tuple[Derived, ...]:
    """This strategy's retirements as `OPERATOR_RETIRE` events, oldest first, each naming where it is written."""
    events = [
        Derived(ruling.at, Event.OPERATOR_RETIRE, "D-048: " + "; ".join(ruling.reasons))
        for ruling in rulings
        if ruling.kind == RETIRE and ruling.subject == strategy
    ]
    return tuple(sorted(events, key=lambda event: datetime.fromisoformat(event.at)))
