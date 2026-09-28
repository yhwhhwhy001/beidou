"""`report daily` with no `--date` reports on the day of the newest cycle row, not the host clock's today.

The hourly check (`deploy/run_check.sh`, :10 past every hour) passes no date.  While the default was the
clock's today, the 00:10Z run rendered a day with no bar in it yet, and the previous day's file - last
written at 23:10Z - never received its 23:00 bar, whose cycle row lands at about 00:00:30Z.  RESEARCH_LOG
2026-09-28 counted 24 of 24 archived days short by exactly that bar, two readings of 09-11 the wrong sign,
and two pages (M-Q03 `failed_bars`, bar sanity's first-seen) blind on it.  The default is now `_day_of` of
the newest row - the rule `live status` already asked M-015 with - so the 00:10Z run closes yesterday.

The state here is what the files hold at those moments; no case depends on the clock this suite runs on,
except the last, which is about the clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from beidou_cli import main
from beidou_live.report_common import newest_day
from beidou_live.state import StateStore
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000
DAY = "2026-09-04"  # long past, so a default that fell back to the clock could not land on it by accident
BAR_23 = int(datetime(2026, 9, 4, 23, tzinfo=UTC).timestamp() * 1000)


def _cycle(bar_ms: int) -> dict[str, Any]:
    """A traded cycle for the bar opening at `bar_ms`, written 25s after that bar closed, as the loop does."""
    at = datetime.fromtimestamp((bar_ms + HOUR) / 1000 + 25, tz=UTC).isoformat()
    return {
        "bar_open_ms": bar_ms,
        "as_of_ms": bar_ms,
        "at": at,
        "equity": 10_000.0,
        "skip": False,
        "guard_reasons": [],
        "targets": {},
        "orders": [],
    }


def _store(tmp_path: Path, rows: list[dict[str, Any]]) -> StateStore:
    store = StateStore(tmp_path / "live")
    store.save(store.load())
    for row in rows:
        store.append_cycle(row)
    return store


def _run(tmp_path: Path, *args: str) -> list[str]:
    """`report daily` on that state; returns the days it wrote a report for."""
    profile = load_yaml(ROOT / "config/live.demo.yaml")
    profile["paths"] = {"state_dir": str(tmp_path / "live"), "reports_dir": str(tmp_path / "reports")}
    profile["alerts"] = {}  # a test never pages
    profile["costs"] = str(ROOT / "config/costs.yaml")
    profile["registry"] = str(ROOT / "config/alpha_registry.yaml")
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    (tmp_path / "archive" / "klines").mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out"
    command = ["report", "daily", "--profile", str(tmp_path / "profile.yaml"), "--out", str(out)]
    result = CliRunner().invoke(main, [*command, "--data-root", str(tmp_path / "archive"), *args])
    assert result.exit_code == 0, result.output
    return sorted(path.stem for path in out.glob("*.json"))


def _written(tmp_path: Path, day: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((tmp_path / "out" / f"{day}.json").read_text(encoding="utf-8"))
    return payload


def test_at_ten_past_midnight_the_default_closes_the_day_that_just_ended(tmp_path: Path) -> None:
    # The files at 00:10Z on 09-05: the 23:00 bar of 09-04 closed at 00:00Z, and its row is the newest.
    _store(tmp_path, [_cycle(BAR_23 - HOUR), _cycle(BAR_23)])
    assert _run(tmp_path) == [DAY]
    assert _written(tmp_path, DAY)["cycles"] == 2, "the 23:00 bar is in the report of the day it belongs to"


def test_the_first_bar_of_the_new_day_moves_the_default_on(tmp_path: Path) -> None:
    # At 01:10Z the 00:00 bar of 09-05 has closed: from here the hourly check renders 09-05.
    _store(tmp_path, [_cycle(BAR_23), _cycle(BAR_23 + HOUR)])
    assert _run(tmp_path) == ["2026-09-05"]
    assert _written(tmp_path, "2026-09-05")["cycles"] == 1


def test_a_failed_23_00_bar_is_counted_where_it_pages(tmp_path: Path) -> None:
    """The case the old default hid: M-Q03 pages on `failed_bars`, and no report ever held this row.

    An ERROR row carries no `as_of_ms` - the klines never arrived - so `_day_of` falls back to the host
    clock's `bar_open_ms`, which still names 09-04.
    """
    failed = {
        "bar_open_ms": BAR_23,
        "at": "2026-09-05T00:01:01+00:00",
        "phase": "ERROR",
        "error": "ProxyError: 503 Service Unavailable",
    }
    _store(tmp_path, [_cycle(BAR_23 - HOUR), failed])
    assert _run(tmp_path) == [DAY]
    assert _written(tmp_path, DAY)["restarts"]["failed_bars"] == 1


def test_an_explicit_date_still_wins(tmp_path: Path) -> None:
    _store(tmp_path, [_cycle(BAR_23)])
    assert _run(tmp_path, "--date", "2026-09-03") == ["2026-09-03"]


def test_with_nothing_recorded_the_clock_names_the_day(tmp_path: Path) -> None:
    store = _store(tmp_path, [])
    assert newest_day(store) is None
    before = datetime.now(UTC).strftime("%Y-%m-%d")
    written = _run(tmp_path)
    after = datetime.now(UTC).strftime("%Y-%m-%d")
    assert len(written) == 1 and written[0] in {before, after}, written
