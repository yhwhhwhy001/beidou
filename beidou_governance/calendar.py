"""Every date in the checkout that changes what something does, read off the file that holds it.

On 2026-09-25 it took a 63 KB analysis (`docs/analysis/2026-09-25-october-13-readiness.md`) to see that
three switches would flip in the same instant on 10-13 (E-PR16).  They lived in a shell launcher, a
test's constant and a test-side exemption, and nothing listed them side by side - each was written by
the session that needed it, next to the code it gated, which is the right place for the switch and the
wrong place for the only question an operator asks about them: what flips this week?

So this module lists them.  It does not remember them: every row is re-read from its file on every call,
from the line that holds it, so a date that moves in its file moves here, and a switch deleted from its
file leaves the list.  It acts on none of them either - a list that could fire the thing it lists is the
shape R10 forbids, and the readers already do what their dates tell them.

**Three statuses, and what each claims.**

* `past` - the date is behind `now`, so its reader has already flipped (or would have, had it run).
* `inert` - the date is ahead, but reaching it changes nothing, because what it gates was settled some
  other way.  Only claimed where the reader's OWN condition can be asked today: the startup gate for the
  bridge, the equality the test asserts for the single-window opening, `applied` for a window change.
* `pending` - the date is ahead and reaching it changes what its reader does.  Also the answer whenever
  inertness could not be asked: a switch that might bite is reported as one that will.

The rows are those of `docs/analysis/2026-09-28-production-refactor-execution-plan.md` §3.6, plus the
batch-window rollover, which a full-tree grep for dates in assignment position turned up and the plan
did not list: `WINDOW_ANCHOR` changes what `research mine` and the admission gate allow every 30 days.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from beidou_alpha.registry import MAIN_BOOK, parse_registry
from beidou_governance import reopen, window_changes
from beidou_governance.admission import window_start
from beidou_governance.policy import STANDING_MINE_ROUNDS, Policy
from beidou_governance.promote import Gate
from beidou_shared.config import load_yaml

LAUNCHER = "deploy/run_live.sh"
POLICY = "beidou_governance/policy.py"
ADMISSION = "beidou_governance/admission.py"
REGISTRY = "config/alpha_registry.yaml"
FREEZE = "tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py"
#: Every file a row can come from, for whoever needs a checkout the calendar can read.
SOURCES = (LAUNCHER, POLICY, ADMISSION, reopen.LIST, window_changes.LIST, REGISTRY, FREEZE)

Status = Literal["pending", "inert", "past"]


@dataclass(frozen=True)
class DatedSwitch:
    """One date and what happens on it.  `source` is `file:line`, relative to the checkout."""

    at: datetime
    source: str
    reader: str  # who acts on the date: a launcher, a test, a command, the daily report
    consequence: str  # what that reader does differently from `at` on, and why the status is what it is
    status: Status

    def as_dict(self) -> dict[str, str]:
        return {
            "at": self.at.astimezone(UTC).isoformat(),
            "source": self.source,
            "reader": self.reader,
            "consequence": self.consequence,
            "status": self.status,
        }


def dated_switches(
    *,
    repo: Path,
    registry_path: str = REGISTRY,
    now: datetime,
    gate: Gate | None = None,
    until: datetime | None = None,
) -> list[DatedSwitch]:
    """The checkout's dated switches, soonest first.  Reads files; writes nothing, runs nothing.

    `gate` is the armed startup gate (`governance_cmd._gate`), asked of `repo / registry_path` to learn
    whether the bridge's expiry would bite.  It is injected because the gate lives in `beidou_live` and
    this package may not import it - the same reason `promote.apply` takes it.  `until` drops the rows
    after it, and is checked before the gate is asked, so a report looking a week ahead does not pay for a
    question about a date it will not print.
    """
    rows = [
        *_bridge(repo, registry_path, now, gate, until),
        *_opening(repo, now),
        *_window_rollover(repo, now),
        *_reopen(repo, now),
        *_window_changes(repo, now),
        *_probe_reviews(repo, registry_path, now),
        *_freeze(repo, now),
    ]
    return sorted((row for row in rows if until is None or row.at <= until), key=lambda row: (row.at, row.source))


def _instant(text: str) -> datetime | None:
    """A date or an ISO instant; a bare date is 00:00Z, as every reader here treats one."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None  # the reader says UNREADABLE and never flips, so neither does the calendar
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _status(at: datetime, now: datetime, *, settled: bool = False) -> Status:
    """`past` from the instant itself on: every reader here compares with `>=` or its shell equivalent."""
    if at <= now:
        return "past"
    return "inert" if settled else "pending"


def _lines(repo: Path, relative: str) -> list[str]:
    """An absent file holds no switch: deleting the file is how its switch leaves the checkout."""
    path = repo / relative
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _dated_constant(repo: Path, relative: str, name: str) -> tuple[datetime, str] | None:
    """A top-level `NAME = "..."` (python) or `NAME="..."` (shell): its instant and its `file:line`.

    Read as text, not imported: the test file is not importable from a package, the launcher is not
    python, and an import would read the running code rather than the checkout at `repo`.
    """
    pattern = re.compile(rf'^{re.escape(name)}\s*=\s*"([^"]+)"')
    for number, line in enumerate(_lines(repo, relative), 1):
        if (match := pattern.match(line)) and (at := _instant(match.group(1))):
            return at, f"{relative}:{number}"
    return None


def _where(relative: str, lines: Sequence[str], entry_id: str, key: str) -> str:
    """`file:line` of `key:` inside the list item `- id: <entry_id>`; a yaml path if the text will not say.

    The YAML loader keeps no line numbers, so the line is found in the text: the item's `- id:` line opens
    it and the next `- id:` closes it.  A yaml path in place of a line fails
    `test_every_dated_switch_is_on_the_calendar`, which is the point - a row nobody can open is not a row.
    """
    head = re.compile(rf"""^\s*-\s+id:\s*["']?{re.escape(entry_id)}["']?\s*(#.*)?$""")
    item = re.compile(r"^\s*-\s+id:")
    field = re.compile(rf"^\s*{re.escape(key)}:")
    inside = False
    for number, line in enumerate(lines, 1):
        if item.match(line):
            inside = bool(head.match(line))
        elif inside and field.match(line):
            return f"{relative}:{number}"
    return f"{relative} {entry_id}.{key}"


def _bridge(
    repo: Path, registry_path: str, now: datetime, gate: Gate | None, until: datetime | None
) -> list[DatedSwitch]:
    """D-041: `run_live.sh` passes `--allow-unvalidated` to every armed start before `BRIDGE_UNTIL`.

    Inert exactly when the startup gate is clean - not when tsmom's `evidence.verdict` reads PASS or
    WEAK_PASS, the proxy the plan proposed.  The flag waives `live_cmd`'s `if problems or
    dataset.blocking`, and the verdict is one input to the first half (beside the report's existence,
    digest, params and construction) and none of the second.  The bridge was built on 2026-09-18 for a
    membership rebuild as much as for a FAIL, and a rebuild before the date would leave the verdict
    reading WEAK_PASS over a start that cannot happen.
    """
    found = _dated_constant(repo, LAUNCHER, "BRIDGE_UNTIL")
    if found is None:
        return []
    at, source = found
    why = ""
    status: Status = "past"
    if at > now:
        if until is not None and at > until:
            return []
        if gate is None:
            status, why = "pending", "；本次没问 startup gate，挡不挡不知道"
        else:
            problems = list(gate(repo / registry_path))
            status = "pending" if problems else "inert"
            why = (
                f"；startup gate 今天挡 {len(problems)} 条：{problems[0]}"
                if problems
                else "；startup gate 今天干净，到期不改任何事"
            )
    return [
        DatedSwitch(
            at,
            source,
            f"{LAUNCHER}（launchd 每次 armed 启动）",
            f"armed 启动不再带 --allow-unvalidated，startup gate 挡住就起不来{why}",
            status,
        )
    ]


def _opening(repo: Path, now: datetime) -> list[DatedSwitch]:
    """0.3.1's one-window fifth mine round.  Inert while the standing value already equals the number.

    The imported rule set, not the text at `repo`: the reader is a test that imports it, and the equality
    is between a dataclass default and a module constant, which only an import evaluates faithfully.
    """
    found = _dated_constant(repo, POLICY, "SINGLE_WINDOW_MINE_OPENING_ENDS")
    if found is None:
        return []
    at, source = found
    rounds = Policy().max_mine_rounds_per_window
    settled = rounds == STANDING_MINE_ROUNDS
    return [
        DatedSwitch(
            at,
            source,
            "tests/governance/test_the_single_window_mine_opening_is_returned.py",
            f"到期起这条测试断言 max_mine_rounds_per_window == STANDING_MINE_ROUNDS；今天 {rounds} "
            + (f"== {STANDING_MINE_ROUNDS}，已相等" if settled else f"!= {STANDING_MINE_ROUNDS}，到期当天 CI 变红"),
            _status(at, now, settled=settled),
        )
    ]


def _window_rollover(repo: Path, now: datetime) -> list[DatedSwitch]:
    """The next batch-window boundary: R1's and R4's per-window counts start again from it.

    Recurring, so only the next one is listed; the one after appears once this one has passed.  Off
    `admission.window_start`, the one calendar R1 and R4 both count in.  Always `pending`: whether the
    reset frees anything depends on the window's spend, which is the ledger's to say, not a file's.
    """
    found = _dated_constant(repo, ADMISSION, "WINDOW_ANCHOR")
    if found is None:
        return []
    anchor, source = found
    policy = Policy()
    at = window_start(policy, anchor=anchor.isoformat(), now=now) + timedelta(days=policy.window_days)
    return [
        DatedSwitch(
            at,
            source,
            "`research mine`、`governance next`（R1），`governance plan|apply|advance`（R4）",
            f"批次窗口翻页（每 {policy.window_days} 天）：R1 的每窗口 ledger 行数与 mine round 数、R4 的每窗口晋升数按日历清零",
            "pending",
        )
    ]


def _reopen(repo: Path, now: datetime) -> list[DatedSwitch]:
    lines = _lines(repo, reopen.LIST)
    rows: list[DatedSwitch] = []
    for entry in reopen.load(repo / reopen.LIST) if lines else []:
        at = _instant(str(entry.args.get("date", ""))) if entry.check == "date_after" else None
        if at is not None:
            rows.append(
                DatedSwitch(
                    at,
                    _where(reopen.LIST, lines, entry.id, "date"),
                    "beidou governance reopen",
                    f"{entry.id} 从 NOT MET 变 MET：日期到了只说明可以重新裁定，裁定仍要人来做",
                    _status(at, now),
                )
            )
    return rows


def _window_changes(repo: Path, now: datetime) -> list[DatedSwitch]:
    lines = _lines(repo, window_changes.LIST)
    rows: list[DatedSwitch] = []
    for change in window_changes.load(repo / window_changes.LIST) if lines else []:
        at = _instant(change.earliest_window)
        if at is None:
            continue
        if change.applied:
            consequence = f"{change.id} 已 APPLIED，这个日期不再改 `governance window` 的读数"
        else:
            consequence = f"{change.id} 从 WAITING 变 DUE：窗口开了，应用仍是一次 `governance apply` 事务"
        where = _where(window_changes.LIST, lines, change.id, "earliest_window")
        rows.append(
            DatedSwitch(
                at, where, "beidou governance window", consequence, _status(at, now, settled=bool(change.applied))
            )
        )
    return rows


def _probe_reviews(repo: Path, registry_path: str, now: datetime) -> list[DatedSwitch]:
    """`accepted_on + review_after_days` of each probe the daily report reads (`probe.probes_from_registry`).

    Its filter and its default of 90 days are copied here because this package may not import
    `beidou_live`; `test_every_dated_switch_is_on_the_calendar` holds the copy to the original.  A probe
    with no `accepted_on` counts from its first attributed row, a fact of the live record rather than of a
    file, and is not listed.
    """
    lines = _lines(repo, registry_path)
    rows: list[DatedSwitch] = []
    for entry in parse_registry(load_yaml(repo / registry_path)).enabled if lines else ():
        probe = entry.probe or {}
        if not probe or not (probe.get("stop") or entry.book != MAIN_BOOK):
            continue
        accepted = _instant(str(probe.get("accepted_on", "") or ""))
        if accepted is None:
            continue
        days = int(probe.get("review_after_days", 90))
        at = accepted + timedelta(days=days)
        rows.append(
            DatedSwitch(
                at,
                _where(registry_path, lines, entry.id, "accepted_on"),
                "日报 Probe books 段（`beidou_live/probe.py` 的 review_due）",
                f"{entry.id}（{entry.book}）的 probe 复核到期：日报从 OK 报成 REVIEW_DUE（accepted_on + {days} 天）",
                _status(at, now),
            )
        )
    return rows


def _freeze(repo: Path, now: datetime) -> list[DatedSwitch]:
    found = _dated_constant(repo, FREEZE, "FREEZE_ENDS")
    if found is None:
        return []
    at, source = found
    consequence = "到期起这条测试自动变惰：不再断言 shipped 构造等于 FROZEN_CONSTRUCTION"
    return [DatedSwitch(at, source, FREEZE, consequence, _status(at, now))]
