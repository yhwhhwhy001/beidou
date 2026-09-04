"""Plan gaps closed on 2026-09-04: T-L04 partial fills, the guard alert and the failed-cycle record.

The first two are Test Contract items from the refactor plan that had no assertion
behind them; the third is the asymmetry that made the -1023 outage on 2026-09-04
leave no durable trace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from beidou_alpha.panel import Panel
from beidou_live.engine import LiveEngine
from beidou_live.state import StateStore
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import SYMBOLS, _config, _model, _prices


class RecordingAlerts:
    """A webhook that records instead of posting."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def _world(august_panel: Panel, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    venue = FakeVenue(balance=10_000.0, prices=_prices(august_panel, cursor))
    clock = FakeClock(market.bar_open_ms(cursor) + 5_000)
    store = StateStore(tmp_path / "live")
    alerts = RecordingAlerts()
    engine = LiveEngine(
        _config(tmp_path, **overrides),
        model=_model(),
        market=market,
        venue=venue,
        clock=clock,
        store=store,
        alerts=alerts,
    )
    return {
        "market": market,
        "venue": venue,
        "clock": clock,
        "store": store,
        "engine": engine,
        "alerts": alerts,
        "cursor": cursor,
    }


async def test_partial_fill_is_re_derived_from_the_real_position(august_panel: Panel, tmp_path: Path) -> None:
    """T-L04: the next cycle sizes from the position the venue actually holds, not from what was requested."""
    world = _world(august_panel, tmp_path)
    engine, venue, market = world["engine"], world["venue"], world["market"]
    await engine.startup()
    bar = market.bar_open_ms(world["cursor"] - 1)
    venue.fill_ratios = dict.fromkeys(SYMBOLS, 0.5)  # every venue fill comes back half done
    first = await engine.run_cycle(bar)
    opened = [o for o in first["orders"] if o["status"] == "PARTIAL"]
    assert opened, "the crafted venue should report partial fills"
    victim = opened[0]["symbol"]
    requested = float(opened[0]["quantity"])
    held = venue.qty[victim]
    assert 0 < abs(held) < requested, "the venue holds only part of what was asked for"

    venue.fill_ratios = {}  # the book can fill normally again
    market.cursor += 1
    second = await engine.run_cycle(bar + 3_600_000)
    follow_up = [o for o in second["orders"] if o["symbol"] == victim]
    assert follow_up, "the shortfall must be re-ordered, not forgotten"
    # the follow-up closes the gap between the real position and the target, not the original request
    target_weight = second["targets"][victim]
    price = venue.prices[victim]
    target_qty = target_weight * second["equity"] / price
    assert abs(venue.qty[victim] - target_qty) < abs(held - target_qty), "the position moved towards its target"
    assert float(follow_up[0]["quantity"]) < requested, "it re-derives the delta rather than repeating the order"


async def test_guard_state_change_alerts_once_in_each_direction(august_panel: Panel, tmp_path: Path) -> None:
    """M-001: a guard that stops the book must be announced, and so must its clearing; neither may repeat."""
    world = _world(august_panel, tmp_path)
    engine, market, alerts = world["engine"], world["market"], world["alerts"]
    await engine.startup()
    bar = market.bar_open_ms(world["cursor"] - 1)
    await engine.run_cycle(bar)
    assert alerts.sent == [], "a clean cycle says nothing"

    engine.config.kill_switch_path.write_text("engaged\n", encoding="utf-8")
    market.cursor += 1
    await engine.run_cycle(bar + 3_600_000)
    assert len(alerts.sent) == 1 and "KILL_SWITCH" in alerts.sent[0]
    assert engine.state.last_guard_reasons == ["KILL_SWITCH"]

    market.cursor += 1
    await engine.run_cycle(bar + 2 * 3_600_000)
    assert len(alerts.sent) == 1, "the same guard must not alert every hour"

    engine.config.kill_switch_path.unlink()
    market.cursor += 1
    await engine.run_cycle(bar + 3 * 3_600_000)
    assert len(alerts.sent) == 2 and "cleared" in alerts.sent[1]
    assert engine.state.last_guard_reasons == []


async def test_a_failed_cycle_leaves_a_durable_row(august_panel: Panel, tmp_path: Path) -> None:
    """The heartbeat is overwritten by the next cycle, so an outage has to be recorded in cycles.jsonl."""
    world = _world(august_panel, tmp_path)
    engine, market, store, alerts = world["engine"], world["market"], world["store"], world["alerts"]
    await engine.startup()
    bar = market.bar_open_ms(world["cursor"] - 1)
    ok = await engine.run_cycle(bar)
    engine._finish_cycle(ok, {})

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("venue unreachable")

    engine.run_cycle = boom  # type: ignore[method-assign]
    market.cursor += 1
    assert await engine.guarded_cycle(bar + 3_600_000) is None

    rows = [json.loads(line) for line in store.cycles_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [r for r in rows if r.get("phase") == "ERROR"]
    assert len(failed) == 1, "the failure must be visible in cycles.jsonl, not only in the log"
    assert failed[0]["bar_open_ms"] == bar + 3_600_000  # buckets into the right day
    assert "venue unreachable" in failed[0]["error"] and failed[0]["consecutive_errors"] == 1
    assert "equity" not in failed[0], "no equity, so the drift check keeps ignoring it"
    assert failed[0]["targets"] == ok["targets"], "the targets still in force are carried"
    assert any("cycle failed" in text for text in alerts.sent)
