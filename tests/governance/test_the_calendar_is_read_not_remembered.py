"""The calendar reads its dates off the files every time.  Nothing in it remembers one.

The files that hold switches are copied into `tmp_path` and edited there, and the calendar over the copy
has to follow each edit - a date added, a date moved, a file gone - while the calendar over the real
checkout stays as it was.  A row kept anywhere else (a constant, a cache, the running code's import)
would show up here as a copy the calendar did not follow.  The same holds one level out: the daily
report's week and `beidou governance calendar` both read through it, so both are driven on a copy too.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from beidou_cli import governance_cmd, main
from beidou_governance.calendar import FREEZE, LAUNCHER, SOURCES, dated_switches
from beidou_governance.reopen import LIST, MET, NOT_MET, evaluate, load
from beidou_live.report_governance import dated_switch_block
from beidou_live.reports import daily_markdown, daily_payload
from beidou_live.state import StateStore
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, 9, tzinfo=UTC)
NOVEMBER_1 = datetime(2026, 11, 1, tzinfo=UTC)

#: Shaped like the file's own `date_after` entries: an instant under `args`, which is what `reopen.evaluate`
#: parses - quoted and zoned like every real one, unless a test writes another.  The `date:` line is the last.
ADDED = """  - id: calendar-reads-its-dates
    subject: a condition written after the calendar was
    ruled: 2026-09-28
    verdict: none
    condition: none
    check: {check}
    args:
      date: {date}
"""


def _copy(tmp_path: Path) -> Path:
    for relative in SOURCES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, tmp_path / relative)
    return tmp_path


def _add_a_condition(checkout: Path, check: str = "date_after", date: str = "'2026-11-01T00:00:00+00:00'") -> int:
    listing = checkout / "governance" / "reopen.yaml"
    text = listing.read_text(encoding="utf-8")
    added = ADDED.format(check=check, date=date)
    listing.write_text(text + ("" if text.endswith("\n") else "\n") + added, encoding="utf-8")
    return len(listing.read_text(encoding="utf-8").splitlines())


def _set_bridge(checkout: Path, day: str) -> int:
    launcher = checkout / LAUNCHER
    text = re.sub(r'^BRIDGE_UNTIL="[^"]*"', f'BRIDGE_UNTIL="{day}"', launcher.read_text(encoding="utf-8"), flags=re.M)
    launcher.write_text(text, encoding="utf-8")
    return next(n for n, line in enumerate(text.splitlines(), 1) if line.startswith("BRIDGE_UNTIL="))


def test_a_date_after_written_into_the_list_is_on_the_calendar(tmp_path: Path) -> None:
    checkout = _copy(tmp_path)
    line = _add_a_condition(checkout)
    (row,) = [row for row in dated_switches(repo=checkout, now=NOW) if row.at == NOVEMBER_1]
    assert (row.source, row.reader, row.status) == (
        f"governance/reopen.yaml:{line}",
        "beidou governance reopen",
        "pending",
    )
    assert row.consequence.startswith("calendar-reads-its-dates ")
    assert not [row for row in dated_switches(repo=ROOT, now=NOW) if row.at == NOVEMBER_1], "the real list has none"


def test_a_date_under_a_check_that_never_reads_it_is_not_a_switch(tmp_path: Path) -> None:
    """`operator` is answered by a person; `reopen.evaluate` never looks at `args.date` for it."""
    checkout = _copy(tmp_path)
    _add_a_condition(checkout, check="operator")
    assert not [row for row in dated_switches(repo=checkout, now=NOW) if row.at == NOVEMBER_1]


def test_a_bare_date_flips_its_reader_at_the_midnight_utc_the_calendar_lists(tmp_path: Path) -> None:
    """`date: 2026-11-01`, no zone.  The calendar always listed it at 00:00Z; the reader crashed on it.

    `fromisoformat` reads a bare date as a naive datetime, `now` is aware, and the subtraction raised
    TypeError out of `reopen.evaluate` - taking all of `beidou governance reopen` down over one entry.
    Nobody had written one yet: every date in the real list carries `+00:00`.  Both now read the date
    through `reopen.instant`, so the reader flips at the instant the calendar lists, and not a microsecond
    before it.
    """
    checkout = _copy(tmp_path)
    line = _add_a_condition(checkout, date="2026-11-01")  # unquoted, so yaml hands `load` a date, not a str
    (row,) = [row for row in dated_switches(repo=checkout, now=NOW) if row.source == f"{LIST}:{line}"]
    assert row.at == NOVEMBER_1
    (entry,) = [entry for entry in load(checkout / LIST) if entry.id == "calendar-reads-its-dates"]
    moments = (NOVEMBER_1 - timedelta(microseconds=1), NOVEMBER_1)
    assert [evaluate(entry, {"now": moment}).state for moment in moments] == [NOT_MET, MET]
    result = CliRunner().invoke(governance_cmd.reopen_cmd, ["--root", str(checkout)])
    assert result.exit_code == 0, result.output
    assert "calendar-reads-its-dates" in result.output


@pytest.mark.parametrize("source", sorted({row.source for row in dated_switches(repo=ROOT, now=NOW)}))
def test_each_row_moves_with_the_date_on_its_line(tmp_path: Path, source: str) -> None:
    """Three days later on the line, three days later on the calendar - and no other row moves.

    Derived rows too: a probe review is `accepted_on` plus a period, and the window rollover is the
    anchor plus whole windows, so shifting the literal shifts each by exactly the same three days.
    """
    checkout = _copy(tmp_path)
    relative, number = source.rsplit(":", 1)
    lines = (checkout / relative).read_text(encoding="utf-8").splitlines(keepends=True)
    match = re.search(r"20\d\d-\d\d-\d\d", lines[int(number) - 1])
    assert match, f"{source} is a row, so its line holds a date"
    later = (datetime.fromisoformat(match.group(0)) + timedelta(days=3)).date().isoformat()
    lines[int(number) - 1] = lines[int(number) - 1].replace(match.group(0), later, 1)
    (checkout / relative).write_text("".join(lines), encoding="utf-8")

    before = {row.source: row.at for row in dated_switches(repo=ROOT, now=NOW)}
    after = {row.source: row.at for row in dated_switches(repo=checkout, now=NOW)}
    assert after.pop(source) - before.pop(source) == timedelta(days=3)
    assert after == before


def test_a_switch_deleted_from_its_file_leaves_the_calendar(tmp_path: Path) -> None:
    checkout = _copy(tmp_path)
    (checkout / FREEZE).unlink()
    launcher = checkout / LAUNCHER
    launcher.write_text(re.sub(r"^BRIDGE_UNTIL=.*$", "", launcher.read_text(encoding="utf-8"), flags=re.M), "utf-8")
    left = {row.source.split(":")[0] for row in dated_switches(repo=checkout, now=NOW)}
    assert FREEZE not in left and LAUNCHER not in left
    assert {row.source.split(":")[0] for row in dated_switches(repo=ROOT, now=NOW)} >= {FREEZE, LAUNCHER}


def test_the_daily_report_carries_a_flip_on_the_seven_reports_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the start of each report's day: the day before is the last to carry it, the day itself does not."""
    monkeypatch.chdir(_copy(tmp_path))
    _add_a_condition(Path("."))
    days = ("2026-10-24", "2026-10-25", "2026-10-31", "2026-11-01")
    carried = [day for day in days if NOVEMBER_1.isoformat() in {row["at"] for row in dated_switch_block(day)["flips"]}]
    assert carried == ["2026-10-25", "2026-10-31"]


@pytest.mark.parametrize(("gate", "status"), [(None, "pending"), (lambda _path: [], "inert")])
def test_the_daily_bridge_row_is_the_gates_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: Callable[[Path], list[str]] | None, status: str
) -> None:
    monkeypatch.chdir(_copy(tmp_path))
    line = _set_bridge(Path("."), "2026-11-01")
    payload = daily_payload(StateStore(tmp_path / "live"), "2026-10-29", data_root=tmp_path / "no-archive", gate=gate)
    (row,) = [row for row in payload["dated_switches"]["flips"] if row["source"] == f"{LAUNCHER}:{line}"]
    assert row["status"] == status
    assert f"2026-11-01T00:00Z {status} · {LAUNCHER}:{line}" in daily_markdown(payload)


def test_a_week_with_nothing_in_it_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # a checkout holding no switch at all
    payload = daily_payload(StateStore(tmp_path / "live"), "2026-10-06", data_root=tmp_path / "no-archive")
    assert payload["dated_switches"]["flips"] == []
    assert "## 未来 7 天日期翻转\n\n无\n" in daily_markdown(payload)


def test_governance_calendar_asks_the_real_startup_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped profile over an empty data root: the dataset half blocks, so the bridge still bites."""
    checkout = _copy(tmp_path / "checkout")
    line = _set_bridge(checkout, (datetime.now(UTC) + timedelta(days=10)).date().isoformat())
    monkeypatch.chdir(ROOT)  # the registry cites its evidence by paths relative to the checkout it runs from
    arguments = ["--root", str(checkout), "--days", "30", "--data-root", str(tmp_path / "no-archive")]
    result = CliRunner().invoke(governance_cmd.calendar_cmd, arguments)
    assert result.exit_code == 0, result.output
    (bridge,) = [text for text in result.output.splitlines() if f"| {LAUNCHER}:{line} |" in text]
    assert bridge.endswith("| pending") and "startup gate 今天挡" in bridge
    assert "只读：本命令不动任何开关" in result.output


def test_governance_calendar_lists_the_horizon_it_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _copy(tmp_path)
    line = _set_bridge(checkout, (datetime.now(UTC) + timedelta(days=10)).date().isoformat())
    monkeypatch.setattr(governance_cmd, "_gate", lambda _profile, _data_root: lambda _path: [])

    def rows(days: int) -> list[dict[str, str]]:
        result = CliRunner().invoke(
            governance_cmd.calendar_cmd, ["--root", str(checkout), "--days", str(days), "--json"]
        )
        assert result.exit_code == 0, result.output
        payload: list[dict[str, str]] = json.loads(result.output)
        return [row for row in payload if row["source"] == f"{LAUNCHER}:{line}"]

    assert [row["status"] for row in rows(30)] == ["inert"]
    assert rows(5) == []


def test_report_daily_hands_the_startup_gate_to_the_week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired, the bridge row carries the gate's answer.  Unwired it would say the gate was never asked."""
    profile = load_yaml(ROOT / "config/live.demo.yaml")
    profile["paths"] = {"state_dir": str(tmp_path / "live"), "reports_dir": str(tmp_path / "reports")}
    profile["alerts"] = {}  # a test never pages
    profile["costs"] = str(ROOT / "config/costs.yaml")
    profile["registry"] = str(ROOT / "config/alpha_registry.yaml")
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    checkout = _copy(tmp_path / "checkout")
    _set_bridge(checkout, "2026-10-09")
    monkeypatch.chdir(checkout)
    command = ["report", "daily", "--profile", str(tmp_path / "profile.yaml"), "--date", "2026-10-06"]
    result = CliRunner().invoke(
        main, [*command, "--data-root", str(tmp_path / "no-archive"), "--out", str(tmp_path / "out")]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "out" / "2026-10-06.json").read_text(encoding="utf-8"))
    (bridge,) = [row for row in payload["dated_switches"]["flips"] if row["source"].startswith(LAUNCHER)]
    assert bridge["status"] == "pending" and "startup gate 今天挡" in bridge["consequence"], bridge
