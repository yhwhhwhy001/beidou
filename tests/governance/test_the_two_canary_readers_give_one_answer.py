"""L4 has two readers, and until 2026-09-26 they read one soak two ways.

`governance canary` prints the verdict; `plan` and `apply` act on it, through `_admission` -> `admit`
-> `canary_health`.  The command resolved `CONSTRUCTION_ALIASES` before counting digests and the write
path did not.  `canary_health` was written at 23:15 on 2026-09-09 (3ffe58c6), `evaluate` gained its
`aliases` parameter thirteen minutes later (048204f7), and only the command was updated - while `admit`
held the table the whole time and handed it to `clean_days` one line earlier.

So a soak that crossed a declared rename read HEALTHY at the command and was REFUSED by `apply`, whose
one reason was `L4: the canary soak did not pass`.  Replayed over the armed record's digest history as
a proxy, 81 of 419 possible soak starts (19.3%) fell in that gap, all of them between 09-04 and 09-18.
That is an upper bound: a digest moves only at a restart, and the shadow restarts far less often than
the armed loop.  The real shadow record never hit it, and nothing can while the construction is frozen.

The falsifier is agreement, asked of both readers on the same files: a declared rename passes both,
and a real construction change fails both.

Since 2026-09-27 `_admission` also asks `registry_soaked`, which the command cannot ask: it holds no
proposal.  So the soak here is stamped the way the engine stamps a loop running the promotion
(`_stamped`), and agreement stays the falsifier for everything both readers can ask.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from beidou_alpha.registry import parse_registry
from beidou_cli.governance_cmd import STATE, _admission, canary_cmd
from beidou_governance.admission import admit, window_index
from beidou_governance.canary import SOAK_CYCLES
from beidou_governance.lifecycle import Book, Candidate, State
from beidou_governance.policy import Policy
from beidou_governance.state import write as write_state
from beidou_live.composition import build_model
from beidou_live.construction import CONSTRUCTION_ALIASES
from beidou_live.engine import registry_digest
from beidou_shared.config import load_yaml

RENAMED, ORIGINAL = next(iter(CONSTRUCTION_ALIASES.items()))
MOVED = "f" * 64  # declared nowhere: a construction that really changed
PROFILE = Path(__file__).resolve().parents[2] / "config" / "live.demo.yaml"


def _registry(*, sleeve: bool) -> dict[str, Any]:
    """The main book alone, or with `flow` asking for a third of it - the promotion under test."""
    payload: dict[str, Any] = {
        "version": 1,
        "ensemble": {"method": "mean", "turnover_penalty": 0.0},
        "books": {},
        "strategies": [{"id": "tsmom", "enabled": True, "weight": 1.0}],
    }
    if sleeve:
        payload["books"] = {"flow_short": {"fraction": 1 / 3}}
        payload["strategies"].append({"id": "flow", "enabled": True, "weight": 1.0, "book": "flow_short"})
    return payload


def _stamped() -> str:
    """The `registry` digest a loop running the promotion writes on every cycle: the engine's recipe."""
    return registry_digest(build_model(parse_registry(_registry(sleeve=True)), load_yaml(PROFILE)))


def _hourly(
    end: datetime, hours: int, construction: Callable[[int], str], registry: str = "rrrr"
) -> list[dict[str, Any]]:
    """Cycles as the engine writes them: one per closed bar, stamped with the bar, construction and registry."""
    rows = []
    for hour in range(hours):
        bar = end - timedelta(hours=hours - hour)
        rows.append(
            {
                "at": (bar + timedelta(hours=1, seconds=25)).isoformat(),
                "bar_open_ms": int(bar.timestamp() * 1000),
                "construction": construction(hour),
                "registry": registry,
                "guard_reasons": [],
                "universe": ["BTCUSDT"],
                "targets": {"BTCUSDT": 0.1},
                "skipped": [],
            }
        )
    return rows


def _soak(end: datetime, second: str) -> list[dict[str, Any]]:
    """One finished soak whose construction digest moves to ``second`` two thirds of the way through."""
    return _hourly(end, SOAK_CYCLES, lambda hour: ORIGINAL if hour < 112 else second, _stamped())


def _write(directory: Path, rows: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True)
    (directory / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.parametrize(
    ("second", "healthy"), [(RENAMED, True), (MOVED, False)], ids=["declared-rename", "real-change"]
)
def test_the_canary_command_and_the_registry_write_give_one_answer(tmp_path: Path, second: str, healthy: bool) -> None:
    """Both readers, driven the way the operator drives them, on one set of files.

    `_admission` is the function `plan` and `apply` share, so asking it is asking both.  The promotion
    is otherwise clean - one queued candidate, a third of the book, 45 unbroken days on the armed
    record - so the canary is the only rule left that can refuse it.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    shadow, armed, checkout = tmp_path / "live-shadow", tmp_path / "live", tmp_path / "checkout"
    _write(tmp_path / "live-shadow-dry-run", _soak(now, second))
    _write(armed, _hourly(now, 45 * 24, lambda _hour: ORIGINAL))
    registry = tmp_path / "alpha_registry.yaml"
    registry.write_text(yaml.safe_dump(_registry(sleeve=False)), encoding="utf-8")
    write_state(checkout / STATE, Book(candidates={"flow": Candidate("flow", State.QUEUED, fraction=1 / 3)}))

    command = CliRunner().invoke(canary_cmd, ["--shadow-dir", str(shadow), "--state-dir", str(armed)])
    proposal = yaml.safe_dump(_registry(sleeve=True))
    admission = _admission(str(registry), proposal, checkout, str(armed), str(shadow), str(PROFILE))

    assert (command.exit_code == 0) is healthy, command.output
    assert admission.allowed is healthy, (admission.reasons, admission.measured["canary"])
    if not healthy:
        # The canary and nothing else: a refusal by any other rule would satisfy `not allowed` too.
        assert admission.reasons == ("flow: L4: the canary soak did not pass",), admission.reasons


def test_admit_reads_the_canary_on_the_construction_its_clock_reads() -> None:
    """The package half, on a table of its own: the aliases `admit` hands `clean_days` reach the canary.

    Without them the same promotion is refused on the canary alone, which is what `apply` did.
    """
    now = datetime(2026, 11, 20, tzinfo=UTC)
    before, after = parse_registry(_registry(sleeve=False)), parse_registry(_registry(sleeve=True))
    book = Book(
        window=window_index(Policy(), now=now),
        candidates={"flow": Candidate("flow", State.QUEUED, fraction=1 / 3)},
    )
    shadow = _hourly(now, SOAK_CYCLES, lambda hour: "old" if hour < 112 else "new")
    armed = _hourly(now, 45 * 24, lambda _hour: "old")

    declared = admit(
        before, after, book=book, cycles=armed, shadow=shadow, aliases={"new": "old"}, registry="rrrr", now=now
    )
    undeclared = admit(before, after, book=book, cycles=armed, shadow=shadow, registry="rrrr", now=now)

    assert declared.allowed, (declared.reasons, declared.measured["canary"])
    assert undeclared.reasons == ("flow: L4: the canary soak did not pass",), undeclared.reasons
