"""A restart charged the one bar it woke on, and nothing that closed while no process ran (2026-09-30).

Measured on the live record.  The host slept on battery and shut down after the 09-29 13:00 bar's cycle
(14:46Z), booted at 01:36Z, and launchd restarted the loop at 01:37Z.  Eleven bars had closed in between -
14:00 through 00:00 - and the record got one row, the restart's own SKIPPED row for 00:00, carrying
`missed_rebalances: 1`.  M-Q03 counts rows, so the ten before it were charged to nobody.

Nothing else could have written them.  `_record_missed_rebalance` had two callers: the restart, which
looks at the last closed bar only, and the failure backoff, which runs inside a live process.  A dead
process is the one state neither sees, and it is the state a laptop on battery reaches every night.

The same day showed the backoff's own bound undercounting by one.  The 06:00 cycle ran late (the host
was asleep) and failed at 08:58Z with the 07:00 bar already closed; the 4.78h sleep allowed five rows for
the six bars 07-12, and 12:00 got none.  The last test here is that case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beidou_alpha.panel import Panel
from beidou_live.engine import LiveEngine
from beidou_live.health import cycle_health
from beidou_live.reports import restart_cost
from beidou_live.scheduler import (
    BACKOFF_REASON,
    DOWNTIME_MAX_BARS,
    DOWNTIME_REASON,
    MISSED_REBALANCE_REASON,
)
from beidou_live.state import StateStore
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import _config, _model, _prices

HOUR_MS = 3_600_000


class SleepingHostClock(FakeClock):
    """A host that suspends: every sleep takes ``wall_seconds`` of wall clock, whatever was asked for."""

    def __init__(self, now_ms: int, wall_seconds: float) -> None:
        super().__init__(now_ms)
        self.wall_seconds = wall_seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += int(self.wall_seconds * 1000)


def _world(august_panel: Panel, tmp_path: Path, clock: FakeClock | None = None) -> dict[str, Any]:
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    # Half an hour after the newest close: outside any rebalance window, so `immediate` records a skip.
    clock = clock or FakeClock(market.bar_open_ms(cursor) + 1_800_000)
    store = StateStore(tmp_path / "live")
    engine = LiveEngine(
        _config(tmp_path),
        model=_model(),
        market=market,
        venue=FakeVenue(balance=10_000.0, prices=_prices(august_panel, cursor)),
        clock=clock,
        store=store,
    )
    return {"engine": engine, "market": market, "clock": clock, "store": store, "bar": market.bar_open_ms(cursor - 1)}


def _completed(store: StateStore, bar: int) -> None:
    """A row as a completed cycle leaves it: the bar and a phase, nothing this module reads besides."""
    store.append_cycle({"bar_open_ms": bar, "phase": "OK", "skip": False, "dry_run": False})


def _skipped(store: StateStore) -> list[dict[str, Any]]:
    return [row for row in store.read_jsonl(store.cycles_path) if row.get("phase") == "SKIPPED"]


def _downtime_bars(store: StateStore) -> list[int]:
    return [row["bar_open_ms"] for row in _skipped(store) if row["reason"] == DOWNTIME_REASON]


async def test_the_bars_between_the_record_and_the_restart_each_get_a_row(august_panel: Panel, tmp_path: Path) -> None:
    """The 2026-09-30 shape: last cycle eleven bars back, restart on the newest close."""
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 11 * HOUR_MS)

    await engine.run(cycles=0, immediate=True)

    assert _downtime_bars(store) == [bar - k * HOUR_MS for k in range(10, 0, -1)]
    restart = [row for row in _skipped(store) if row["reason"] == MISSED_REBALANCE_REASON]
    assert [row["bar_open_ms"] for row in restart] == [bar], "the restart still owns the bar it woke on"
    assert engine.missed_rebalances == 11
    assert restart[0]["missed_rebalances"] == 11, "the running total on the last row is the whole outage"


async def test_the_reporter_charges_the_outage_as_misses_and_one_restart(august_panel: Panel, tmp_path: Path) -> None:
    """Through `restart_cost`: eleven misses, one restart, and the restart's lateness is its own."""
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 11 * HOUR_MS)

    await engine.run(cycles=0, immediate=True)

    cost = restart_cost(store.read_jsonl(store.cycles_path))
    assert cost["missed_rebalances"] == 11
    assert cost["skipped_bars"] == 11
    assert cost["restarts"] == 1, "one outage is one restart, not eleven"
    assert cost["worst_restart_late_seconds"] == 1_800.0, "the restart row's lateness, not ten hours"
    assert cost["status"] == "ALERT"


async def test_the_success_rate_does_not_count_bars_nothing_attempted(august_panel: Panel, tmp_path: Path) -> None:
    """As SKIPPED rows they would each read as a success and push M-001 up during an outage."""
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 11 * HOUR_MS)

    await engine.run(cycles=0, immediate=True)

    now = datetime.fromtimestamp(world["clock"].now_ms() / 1000, tz=UTC)
    health = cycle_health(store.read_jsonl(store.cycles_path), now=now)
    assert health.attempts == 2, "the completed cycle and the restart's own row; the ten downtime rows are not attempts"


async def test_bars_the_dead_process_already_wrote_are_not_charged_twice(august_panel: Panel, tmp_path: Path) -> None:
    """The gap starts after the newest bar in the record, not after the last COMPLETED cycle.

    A process that failed and backed off before it died wrote its own ERROR and SKIPPED rows for the
    bars after `state.last_bar_ms`; charging them again would count one miss twice.
    """
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 5 * HOUR_MS)
    store.append_cycle({"bar_open_ms": bar - 4 * HOUR_MS, "phase": "ERROR", "error": "ReadError: ", "dry_run": False})
    store.append_cycle(
        {"bar_open_ms": bar - 3 * HOUR_MS, "phase": "SKIPPED", "reason": BACKOFF_REASON, "dry_run": False}
    )

    await engine.run(cycles=0, immediate=True)

    assert _downtime_bars(store) == [bar - 2 * HOUR_MS, bar - HOUR_MS]


async def test_a_restart_with_no_gap_writes_no_downtime_row(august_panel: Panel, tmp_path: Path) -> None:
    """The control: the loop traded the bar before the one it woke on."""
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - HOUR_MS)

    await engine.run(cycles=0, immediate=True)

    assert _downtime_bars(store) == []
    assert engine.missed_rebalances == 1


async def test_a_first_start_owes_nothing(august_panel: Panel, tmp_path: Path) -> None:
    """No row of this process's kind in the record: there is no gap to measure from."""
    world = _world(august_panel, tmp_path)
    store = world["store"]
    store.append_cycle({"bar_open_ms": world["bar"] - 30 * HOUR_MS, "phase": "OK", "dry_run": True})

    await world["engine"].run(cycles=0, immediate=True)

    assert _downtime_bars(store) == [], "a dry run's row is a rehearsal, not this loop's history"


async def test_a_gap_longer_than_a_week_charges_the_newest_week(august_panel: Panel, tmp_path: Path) -> None:
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 300 * HOUR_MS)

    await engine.run(cycles=0, immediate=True)

    bars = _downtime_bars(store)
    assert len(bars) == DOWNTIME_MAX_BARS
    assert bars[0] == bar - DOWNTIME_MAX_BARS * HOUR_MS and bars[-1] == bar - HOUR_MS


async def test_without_immediate_the_last_closed_bar_is_part_of_the_gap(august_panel: Panel, tmp_path: Path) -> None:
    """Nothing after startup will trade it: the loop waits for the NEXT close."""
    world = _world(august_panel, tmp_path)
    engine, store, bar = world["engine"], world["store"], world["bar"]
    _completed(store, bar - 3 * HOUR_MS)

    await engine.run(cycles=0)

    assert _downtime_bars(store) == [bar - 2 * HOUR_MS, bar - HOUR_MS, bar]
    assert [row for row in _skipped(store) if row["reason"] == MISSED_REBALANCE_REASON] == []


async def test_a_late_failure_charges_the_bars_that_closed_before_its_backoff(
    august_panel: Panel, tmp_path: Path
) -> None:
    """2026-09-29: the 06:00 cycle failed at 08:58Z, then the host slept 4.78h through a 120s backoff."""
    failed_bar = FakeMarketData(august_panel, 400).bar_open_ms(399)
    # 1h58m after the failed bar's close: the next bar has already closed when the backoff starts.
    clock = SleepingHostClock(failed_bar + HOUR_MS + 7_080_000, wall_seconds=17_220.0)
    world = _world(august_panel, tmp_path, clock=clock)
    engine, market, store = world["engine"], world["market"], world["store"]
    engine.rebalance_window = 73.7
    await engine.startup()
    engine.consecutive_errors = 1
    market.fail_next = 1

    assert await engine.guarded_cycle(failed_bar) is None

    rows = [row for row in _skipped(store) if row["reason"] == BACKOFF_REASON]
    assert [row["bar_open_ms"] for row in rows] == [failed_bar + k * HOUR_MS for k in range(1, 7)], (
        "07:00 through 12:00: six bars, the first of them closed before the sleep began"
    )
