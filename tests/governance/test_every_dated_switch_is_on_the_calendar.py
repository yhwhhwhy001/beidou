"""Every date that switches something is on the calendar, with the status its own reader would give it.

E-PR16: on 2026-09-25 a 63 KB analysis was what it took to see three switches flip in the same instant on
10-13, because they lived in a launcher, a test and a test-side exemption and nothing listed them.
`beidou_governance.calendar` lists them.  This file holds it to two things.

**It misses none.**  The files that hold switches - `deploy/*.sh`, `beidou_governance/policy.py`,
`governance/*.yaml` - are scanned for a date in a value position, and each one found must be a row.  A
date in a comment is not in a value position, and neither is prose that merely starts with a date, so
writing "2026-10-13" into a note turns nothing red.  In YAML a key decides it, and the keys are named
below with their reasons: a date nobody compares with the clock (`ruled`, `queued`) is a record, not a
switch.  A date under a key named in neither list fails until somebody decides which it is - that
decision is the whole point, and a guess here would be the calendar missing a switch by default.

**It says what the reader will do.**  Each row's status is checked against the reader itself at a pinned
clock: `reopen.evaluate`, `window_changes.evaluate`, `probe_status`, the single-window test's own
condition, the bridge's startup gate.  A calendar that computed "past" by its own arithmetic could drift
from the reader it describes, and then it would be one more document to reconcile by hand.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from beidou_governance import reopen, window_changes
from beidou_governance.admission import window_index
from beidou_governance.calendar import ADMISSION, FREEZE, LAUNCHER, REGISTRY, dated_switches
from beidou_governance.policy import SINGLE_WINDOW_MINE_OPENING_ENDS, STANDING_MINE_ROUNDS, Policy
from beidou_live.composition import load_registry
from beidou_live.probe import probe_status, probes_from_registry

ROOT = Path(__file__).resolve().parents[2]
OCTOBER_6 = datetime(2026, 10, 6, tzinfo=UTC)
OCTOBER_13 = datetime(2026, 10, 13, tzinfo=UTC)
#: Before every date in today's files, exactly on two of them, between them, and after them all.
CLOCKS = (
    datetime(2026, 9, 28, 9, tzinfo=UTC),
    datetime(2026, 10, 3, tzinfo=UTC),
    OCTOBER_6,
    OCTOBER_13,
    datetime(2026, 10, 14, tzinfo=UTC),
)

#: A value that IS a date or an instant.  "2026-09-17 Q-SF3 诊断" is prose that starts with one.
DATE_VALUE = re.compile(r"20\d\d-\d\d-\d\d([T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)?)?")

#: A date under one of these keys is WHEN something switches, so it must be a row on the calendar.
SWITCH_KEYS = {
    "earliest_window": "window_changes.yaml: `governance window` turns WAITING into DUE on it",
    "date": "reopen.yaml, in the `args` of a `date_after` check: `governance reopen` turns NOT MET into MET on it",
}
#: A date under one of these keys records when something HAPPENED.  No reader compares it with the clock.
RECORD_KEYS = {
    "ruled": "reopen.yaml: the day a hypothesis was ruled on",
    "queued": "window_changes.yaml: the day a change was decided and queued",
    "applied": "window_changes.yaml: the transaction that carried a change; it makes `earliest_window` inert",
    "log": "reopen.yaml: where in RESEARCH_LOG the ruling is written",
}
#: `args.date` of a check other than `date_after`: `reopen.evaluate` never reads it.
DORMANT_DATE = "date"


def _shell_dates(path: Path) -> list[int]:
    """Lines whose code holds a date after `=` or right inside a quote.  Comments are cut off first."""
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = "" if line.lstrip().startswith("#") else re.split(r"\s#", line, maxsplit=1)[0]
        if re.search(r"""(=\s*["']?|["'])20\d\d-\d\d-\d\d""", code):
            lines.append(number)
    return lines


def _python_dates(path: Path) -> list[int]:
    """Lines where a date string is assigned, compared, defaulted or passed by keyword.

    Read off the syntax tree, so a comment is not there at all and a docstring is an expression statement,
    which is none of those four positions.
    """
    values: list[ast.expr] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            values.append(node.value)
        elif isinstance(node, ast.Compare):
            values.extend([node.left, *node.comparators])
        elif isinstance(node, ast.keyword):
            values.append(node.value)
        elif isinstance(node, ast.arguments):
            values.extend([*node.defaults, *(d for d in node.kw_defaults if d is not None)])
    return sorted(
        {
            v.lineno
            for v in values
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and DATE_VALUE.fullmatch(v.value)
        }
    )


def _yaml_dates(node: yaml.Node, key: str = "", check: str = "") -> Iterator[tuple[int, str, str]]:
    """(line, key, the enclosing entry's `check`) for every scalar that is a date.  YAML drops comments."""
    if isinstance(node, yaml.MappingNode):
        pairs = {k.value: v for k, v in node.value}
        inner = pairs["check"].value if isinstance(pairs.get("check"), yaml.ScalarNode) else check
        for k, v in node.value:
            yield from _yaml_dates(v, str(k.value), inner)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _yaml_dates(item, key, check)
    elif isinstance(node, yaml.ScalarNode) and DATE_VALUE.fullmatch(str(node.value)):
        yield node.start_mark.line + 1, key, check


def _switch_lines(path: Path) -> tuple[list[int], list[str]]:
    """The lines of `path` that must be rows, and the date-valued YAML keys nobody has classified yet."""
    if path.suffix == ".sh":
        return _shell_dates(path), []
    if path.suffix == ".py":
        return _python_dates(path), []
    lines, unclassified = [], []
    for line, key, check in _yaml_dates(yaml.compose(path.read_text(encoding="utf-8"))):
        if key in RECORD_KEYS or (key == DORMANT_DATE and check != "date_after"):
            continue
        if key in SWITCH_KEYS:
            lines.append(line)
        else:
            unclassified.append(f"{line} `{key}:`")
    return lines, unclassified


def _scanned() -> list[Path]:
    return [
        *sorted((ROOT / "deploy").glob("*.sh")),
        ROOT / "beidou_governance" / "policy.py",
        *sorted((ROOT / "governance").glob("*.yaml")),
    ]


def test_every_date_in_a_value_position_is_on_the_calendar() -> None:
    sources = {row.source for row in dated_switches(repo=ROOT, now=OCTOBER_6)}
    wanted, unclassified = [], []
    for path in _scanned():
        lines, unknown = _switch_lines(path)
        wanted += [f"{path.relative_to(ROOT)}:{line}" for line in lines]
        unclassified += [f"{path.relative_to(ROOT)}:{where}" for where in unknown]
    assert not unclassified, (
        f"{unclassified}: a date under a key this test has not classified.  If a reader compares it with the "
        "clock, it is a switch - put the key in SWITCH_KEYS and teach `beidou_governance.calendar` to read it; "
        "if it records when something happened, put it in RECORD_KEYS.  Either way write the reason beside it."
    )
    assert wanted, "the scan found no date at all, so it is reading nothing"
    missing = sorted(set(wanted) - sources)
    assert not missing, f"{missing} hold a date that switches something, and the calendar does not list them"


def test_a_date_in_a_comment_prose_or_a_record_is_not_a_switch(tmp_path: Path) -> None:
    """The scanner's own contract, on text built to tempt it: only the value-position dates count."""
    script = tmp_path / "launcher.sh"
    script.write_text(
        '# bridged until 2026-10-13\nUNTIL="2026-10-13"   # since 2026-09-18\necho "until 2026-10-13"\n',
        encoding="utf-8",
    )
    module = tmp_path / "rules.py"
    module.write_text(
        '"""Until 2026-10-13."""\n# 2026-10-13\nENDS = "2026-10-13T00:00:00+00:00"\nNOTE = "2026-09-18 ruled"\n',
        encoding="utf-8",
    )
    listing = tmp_path / "list.yaml"
    listing.write_text(
        "entries:  # 2026-10-13\n"
        "  - id: a\n    ruled: 2026-09-19\n    check: date_after\n"
        "    args:\n      date: '2026-10-13T00:00:00+00:00'\n    log: 2026-09-19 某一节\n"
        "  - id: b\n    check: operator\n    args:\n      date: 2026-10-13\n",
        encoding="utf-8",
    )
    assert _switch_lines(script) == ([2], [])
    assert _switch_lines(module) == ([3], [])
    seen = [line for line, _key, _check in _yaml_dates(yaml.compose(listing.read_text(encoding="utf-8")))]
    assert seen == [3, 6, 11], "the scanner sees the record and the dormant date; the key lists drop them"
    assert _switch_lines(listing) == ([6], [])
    # A new date-valued key is neither: it fails until somebody writes down which it is.
    listing.write_text(listing.read_text(encoding="utf-8") + "    decided_on: 2026-10-01\n", encoding="utf-8")
    assert _switch_lines(listing) == ([6], ["12 `decided_on:`"])


def test_on_october_6_the_calendar_holds_the_two_october_13_reopen_dates() -> None:
    """Today's file: `risk-g11-denominator` and `drawdown-budget-denominator`.  Rule on them, move this pin."""
    rows = [row for row in dated_switches(repo=ROOT, now=OCTOBER_6) if row.source.startswith(reopen.LIST)]
    october_13 = [row for row in rows if row.at == OCTOBER_13]
    assert len(october_13) == 2, [row.source for row in rows]
    assert {row.status for row in october_13} == {"pending"}
    assert {row.reader for row in october_13} == {"beidou governance reopen"}


@pytest.mark.parametrize("now", CLOCKS)
def test_each_reopen_row_is_what_governance_reopen_would_say(now: datetime) -> None:
    rows = [row for row in dated_switches(repo=ROOT, now=now) if row.source.startswith(reopen.LIST)]
    entries = [entry for entry in reopen.load(ROOT / reopen.LIST) if entry.check == "date_after"]
    assert len(rows) == len(entries)
    for entry in entries:
        (row,) = [row for row in rows if row.consequence.startswith(f"{entry.id} ")]
        assert row.at == reopen.instant(str(entry.args["date"]))  # the reader's parse: naive == aware is False
        assert (row.status == "past") is (reopen.evaluate(entry, {"now": now}).state == reopen.MET)


@pytest.mark.parametrize("now", CLOCKS)
def test_each_window_row_is_what_governance_window_would_say(now: datetime) -> None:
    rows = [row for row in dated_switches(repo=ROOT, now=now) if row.source.startswith(window_changes.LIST)]
    changes = [change for change in window_changes.load(ROOT / window_changes.LIST) if change.earliest_window]
    assert len(rows) == len(changes)
    for change in changes:
        (row,) = [row for row in rows if row.consequence.startswith(f"{change.id} ")]
        state = window_changes.evaluate(change, now).state
        assert (row.status == "pending") is (state == window_changes.WAITING)
        assert (row.status == "past" and not change.applied) is (state == window_changes.DUE)
        assert (row.status == "inert") is (state == window_changes.APPLIED and row.at > now)


@pytest.mark.parametrize("now", CLOCKS)
def test_each_probe_review_is_the_one_the_daily_report_raises(now: datetime) -> None:
    """The calendar copies `probes_from_registry`'s filter and its 90-day default; this holds the copy."""
    rows = [row for row in dated_switches(repo=ROOT, now=now) if row.source.startswith(REGISTRY)]
    probes = [p for p in probes_from_registry(load_registry(ROOT / REGISTRY)) if p.accepted_on]
    assert sorted(row.consequence.split("（")[0] for row in rows) == sorted(p.strategy for p in probes)
    for params in probes:
        (row,) = [row for row in rows if row.consequence.startswith(f"{params.strategy}（")]
        due = probe_status(params, [], equity=None, now_ms=int(now.timestamp() * 1000))["review_due"]
        assert (row.status == "past") is due


def test_a_probe_without_a_review_period_is_reviewed_at_the_readers_default(tmp_path: Path) -> None:
    registry = tmp_path / REGISTRY
    registry.parent.mkdir(parents=True)
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "books": {"side": {"fraction": 0.5}},
                "strategies": [
                    {"id": "s", "enabled": True, "book": "side", "probe": {"accepted_on": "2026-09-03", "stop": {}}}
                ],
            }
        ),
        encoding="utf-8",
    )
    (params,) = probes_from_registry(load_registry(registry))
    (row,) = dated_switches(repo=tmp_path, now=OCTOBER_6)
    assert (row.at - datetime(2026, 9, 3, tzinfo=UTC)).days == params.review_after_days == 90


def _bridge_until() -> datetime:
    """Read the way `tests/cli/test_the_bridge_expiry_survives_the_bash_launchd_runs.py` reads it."""
    match = re.search(r'^BRIDGE_UNTIL="(\d{4}-\d{2}-\d{2})"', (ROOT / LAUNCHER).read_text(encoding="utf-8"), re.M)
    assert match, "run_live.sh no longer sets BRIDGE_UNTIL"
    return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("problems", "status"), [(None, "pending"), ([], "inert"), (["tsmom: evidence verdict FAIL"], "pending")]
)
def test_the_bridge_is_inert_only_when_the_startup_gate_is_clean(problems: list[str] | None, status: str) -> None:
    """Not when the registry's verdict reads WEAK_PASS: the flag waives the whole gate, dataset half included."""
    asked: list[Path] = []

    def gate(path: Path) -> list[str]:
        asked.append(path)
        return list(problems or [])

    before = _bridge_until() - timedelta(days=1)
    (row,) = [
        r
        for r in dated_switches(repo=ROOT, now=before, gate=None if problems is None else gate)
        if r.source.startswith(LAUNCHER)
    ]
    assert (row.at, row.status) == (_bridge_until(), status)
    assert asked == ([] if problems is None else [ROOT / REGISTRY])
    (after,) = [r for r in dated_switches(repo=ROOT, now=_bridge_until(), gate=gate) if r.source.startswith(LAUNCHER)]
    assert after.status == "past" and len(asked) == (0 if problems is None else 1), "a past bridge asks nothing"


def test_the_gate_is_not_asked_about_a_bridge_past_the_horizon() -> None:
    def gate(_path: Path) -> list[str]:
        raise AssertionError("asked about a date the caller will not print")

    now = _bridge_until() - timedelta(days=30)
    rows = dated_switches(repo=ROOT, now=now, gate=gate, until=now + timedelta(days=7))
    assert not [row for row in rows if row.source.startswith(LAUNCHER)]


@pytest.mark.parametrize("now", CLOCKS)
def test_the_single_window_and_the_freeze_rows_answer_their_tests_own_conditions(now: datetime) -> None:
    rows = {row.source.split(":")[0]: row for row in dated_switches(repo=ROOT, now=now)}
    opening = rows["beidou_governance/policy.py"]
    assert opening.at == datetime.fromisoformat(SINGLE_WINDOW_MINE_OPENING_ENDS)
    if now >= opening.at:
        assert opening.status == "past"
    else:
        # The test returns early before the date and asserts this equality from it on.
        assert (opening.status == "inert") is (Policy().max_mine_rounds_per_window == STANDING_MINE_ROUNDS)
    freeze = rows[FREEZE]
    assert (freeze.status == "past") is (now >= freeze.at)


@pytest.mark.parametrize("now", CLOCKS)
def test_the_rollover_row_is_where_the_admission_calendar_turns_the_page(now: datetime) -> None:
    """The first instant after `now` at which `admission.window_index` - the count R1 and R4 read - moves."""
    (row,) = [
        row for row in dated_switches(repo=ROOT, now=now) if row.source.startswith("beidou_governance/admission.py")
    ]
    policy = Policy()
    assert window_index(policy, now=row.at) == window_index(policy, now=now) + 1
    assert window_index(policy, now=row.at - timedelta(microseconds=1)) == window_index(policy, now=now)
    assert row.status == "pending"


def test_every_row_is_past_from_its_own_instant_on() -> None:
    """Every reader here flips AT its date (`>=`, or the shell's `<` for "still before"), not after it."""
    for row in dated_switches(repo=ROOT, now=CLOCKS[0]):
        if row.source.startswith(ADMISSION):
            continue  # the rollover is the NEXT boundary after `now`, so it moves on with the clock by design
        (again,) = [r for r in dated_switches(repo=ROOT, now=row.at) if r.source == row.source]
        assert again.status == "past", again
