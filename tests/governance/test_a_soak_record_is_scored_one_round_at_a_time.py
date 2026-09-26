"""L4: one shadow record can hold more than one soak, and the canary scores only the latest.

Found 2026-09-26.  `deploy/run_shadow.sh` execs `live run --cycles 168`, and `live run` exits 1 when any
of its cycles failed (`done < cycles` in `live_cmd.py`).  The plist's `KeepAlive {SuccessfulExit: false}`
relaunches every non-zero exit.  So the soak begun 2026-09-16 - 168 cycles, three of them proxy 503s -
ended at 2026-09-23T08:00:26Z with exit 1, and launchd began a second 168 in the same `cycles.jsonl` one
second later, under a new construction (ccd7bb9764b5 -> b8f215ab706c).

`governance canary` then read all 249 rows as one soak: `soak 249/168` PASS, `construction_stable` FAIL
on two digests, UNHEALTHY.  A verdict about neither soak - each one's construction was unique.

One definition, two readers (`canary.rounds`): the canary scores the latest round of 168 attempted cycles
and lists the earlier ones, and the launcher asks the same cut how many cycles are still owed before it
starts (`tests/cli/test_the_shadow_launcher_stops_at_the_end_of_its_soak.py`).

The rows below have the shape the engine writes, checked against the real record on 2026-09-26: an OK
row carries no `phase` at all, an ERROR row carries `phase` and no `construction`, and a SKIPPED row is a
bar the loop slept through.  The last test takes its rows from the real `run()` loop instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from beidou_cli.governance_cmd import canary_cmd
from beidou_governance.admission import canary_health
from beidou_governance.canary import SOAK_CYCLES, attempted, evaluate, remaining, rounds
from beidou_live.construction import CONSTRUCTION_ALIASES

FIRST_BAR = datetime(2026, 9, 16, 8, tzinfo=UTC)  # the first bar of the soak begun 2026-09-16


def _bar(index: int) -> dict[str, Any]:
    bar = FIRST_BAR + timedelta(hours=index)
    return {
        "at": (bar + timedelta(hours=1, seconds=25)).isoformat(),
        "bar": bar.isoformat(),
        "bar_open_ms": int(bar.timestamp() * 1000),
        "dry_run": True,
    }


def _ok(index: int, construction: str) -> dict[str, Any]:
    """No `phase` key: `_finish_cycle` puts OK on the heartbeat, not on the row."""
    return _bar(index) | {
        "construction": construction,
        "guard_reasons": [],
        "skipped": [],
        "targets": {"BTCUSDT": 0.1},
        "universe": ["BTCUSDT"],
    }


def _error(index: int) -> dict[str, Any]:
    return _bar(index) | {
        "phase": "ERROR",
        "error": "ProxyError: 503 Service Unavailable",
        "consecutive_errors": 1,
        "targets": {"BTCUSDT": 0.1},
        "orders": [],
    }


def _skipped(index: int) -> dict[str, Any]:
    return _bar(index) | {"phase": "SKIPPED", "reason": "backoff", "orders": [], "targets": {}}


def _soak(first: int, cycles: int, construction: str, errors: tuple[int, ...] = ()) -> list[dict[str, Any]]:
    return [_error(first + i) if i in errors else _ok(first + i, construction) for i in range(cycles)]


def _record_as_of_2026_09_26() -> list[dict[str, Any]]:
    """168 cycles under ccd7bb9764b5 with the three 503s where they fell, then 82 of the second soak."""
    return _soak(0, 168, "ccd7bb9764b5", errors=(3, 129, 132)) + _soak(168, 82, "b8f215ab706c", errors=(7,))


def _write(directory: Path, rows: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cycles.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_the_record_of_2026_09_26_is_two_soaks_and_only_the_second_is_scored() -> None:
    record = _record_as_of_2026_09_26()
    assert [len(soak) for soak in rounds(record)] == [168, 82]

    result = evaluate(record, _soak(0, 250, "armed"))
    checks = {check.name: check for check in result.checks}
    assert checks["construction_stable"].passed, "the two digests belong to two soaks, each stable on its own"
    assert checks["soak"].detail == "82/168 cycles in round 2 of 2"
    assert [check.name for check in result.failures] == ["soak"]
    assert result.soaked == 82


def test_a_soak_with_failed_cycles_is_still_a_finished_soak() -> None:
    """`live run` said "3 of 168 cycle(s) failed"; the canary is where those three get judged."""
    first = _soak(0, 168, "ccd7bb9764b5", errors=(3, 129, 132))
    assert remaining(first) == 0
    result = evaluate(first, _soak(0, 168, "armed"))
    assert result.healthy, [f"{check.name}: {check.detail}" for check in result.failures]


def test_the_next_round_opens_on_the_first_cycle_after_the_last_one() -> None:
    record = _soak(0, SOAK_CYCLES + 1, "a")
    assert [len(soak) for soak in rounds(record)] == [SOAK_CYCLES, 1]
    assert remaining(record) == SOAK_CYCLES - 1


def test_a_bar_the_loop_slept_through_is_not_a_cycle_and_opens_no_round() -> None:
    """`engine.run` does not count a SKIPPED bar against `--cycles`, so neither does the soak.

    And one written after a round's last cycle - the failure backoff sleeping past a bar close - stays in
    that round.  Opening a new round there would make the launcher read a finished soak as 168 to go.
    """
    record = _soak(0, 100, "a")
    record[50:50] = [_skipped(1000), _skipped(1001)]
    assert remaining(record) == SOAK_CYCLES - 100
    assert evaluate(record, record).soaked == 100

    finished = [*_soak(0, SOAK_CYCLES, "a", errors=(SOAK_CYCLES - 1,)), _skipped(SOAK_CYCLES)]
    assert len(rounds(finished)) == 1
    assert remaining(finished) == 0


def test_an_empty_record_owes_the_whole_soak() -> None:
    assert rounds([]) == []
    assert remaining([]) == SOAK_CYCLES


def test_plan_and_apply_read_the_same_round_as_the_command() -> None:
    """`canary_health` is the path `plan` and `apply` take, and it cuts through the same `evaluate`."""
    healthy, why = canary_health(_record_as_of_2026_09_26(), _soak(0, 250, "armed"), aliases=CONSTRUCTION_ALIASES)
    assert not healthy
    assert why == "L4: soak (82/168 cycles in round 2 of 2)"


def test_the_command_lists_every_round_and_scores_the_latest(tmp_path: Path) -> None:
    _write(tmp_path / "live-shadow-dry-run", _record_as_of_2026_09_26())
    _write(tmp_path / "live", _soak(0, 250, "armed"))

    args = ["--shadow-dir", str(tmp_path / "live-shadow"), "--state-dir", str(tmp_path / "live")]
    result = CliRunner().invoke(canary_cmd, args)

    assert result.exit_code == 1, result.output
    assert result.output.splitlines()[:2] == [
        "round 1: rows 1-168, bars 2026-09-16T08:00..2026-09-23T07:00, 168 cycles, 3 ERROR, not scored",
        "round 2: rows 169-250, bars 2026-09-23T08:00..2026-09-26T17:00, 82 cycles, 1 ERROR, scored below",
    ]
    assert "PASS  construction_stable" in result.output
    assert "FAIL  soak" in result.output and "82/168 cycles in round 2 of 2" in result.output


def test_remaining_answers_from_the_directory_the_dry_run_writes(tmp_path: Path) -> None:
    """The launcher names `.beidou/live-shadow`; the loop writes `-dry-run` beside it (`store_directory`)."""
    ask = ["--shadow-dir", str(tmp_path / "live-shadow"), "--remaining"]
    assert CliRunner().invoke(canary_cmd, ask).output == f"{SOAK_CYCLES}\n"

    _write(tmp_path / "live-shadow-dry-run", _record_as_of_2026_09_26())
    assert CliRunner().invoke(canary_cmd, ask).output == "86\n"

    _write(tmp_path / "live-shadow-dry-run", [*_record_as_of_2026_09_26(), *_soak(250, 86, "b8f215ab706c")])
    assert CliRunner().invoke(canary_cmd, ask).output == "0\n"


async def test_the_cut_counts_what_the_engine_counts(tmp_path: Path, august_panel: Any) -> None:
    """Rows from the real `run()` loop.  Its failure backoff writes SKIPPED rows between the ERROR rows,
    and a soak that counted rows instead of cycles would end early by exactly that many."""
    from tests.live.helpers_liquidation import breaker_engine

    engine, _alerts, _venue, store = breaker_engine(tmp_path, august_panel, limit=12, succeed_at=6)
    await engine.run(cycles=17, immediate=True)
    rows = [json.loads(line) for line in store.cycles_path.read_text(encoding="utf-8").splitlines()]

    assert any(row.get("phase") == "SKIPPED" for row in rows), "no SKIPPED row: this no longer tests one"
    assert sum(map(attempted, rows)) == 17
    assert remaining(rows, soak_cycles=17) == 0
    assert remaining(rows, soak_cycles=18) == 1
