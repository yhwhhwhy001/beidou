"""The loop pings the off-host dead-man after a cycle that got through execution, and after no other (WP-R1).

The service pages when the pings STOP, so every ping is a claim that a cycle ran.  An ERROR cycle that
pinged would hold the page back through exactly the outage it exists for; a guard-skipped cycle traded
nothing and checked no exit, so it is not that claim either.  And a ping sent before the orders would
spend up to its whole timeout inside the ~90 s rebalance window, so the local server below also
records how many orders were already out when each ping arrived.

The URL reaches the engine the way `live run` hands it over for the process trading the account:
exported, read by `deadman.loop_url()`, passed to the constructor.  `tests/conftest.py` deletes every
`BEIDOU_DEADMAN_*` before each test, so it is set here, to a server on 127.0.0.1.
"""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals.tsmom import TsmomParams
from beidou_live import deadman
from beidou_live.cycle_record import undeclared
from beidou_live.engine import LiveConfig, LiveEngine
from beidou_live.guards import GuardParams
from beidou_live.rebalancer import RebalanceParams
from beidou_live.state import StateStore
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
HOUR_MS = 3_600_000


@dataclass
class DeadMan:
    """A stand-in for the service: it answers `status` and notes how many orders were out at each ping."""

    url: str
    venue: FakeVenue
    status: int = 200
    orders_out_at_each_ping: list[int] = field(default_factory=list)


class _LocalServer(HTTPServer):
    def server_bind(self) -> None:
        """`HTTPServer.server_bind` calls `socket.getfqdn`, a reverse lookup measured at 35 s on the Mac."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", int(self.server_address[1])


def _model() -> AlphaModel:
    params = TsmomParams(vol_window=100).__dict__ | {
        "horizons": [5, 20, 50],
        "horizon_weights": [0.2, 0.3, 0.5],
        "entry_threshold": 0.05,
    }
    return AlphaModel(
        entries=(StrategyEntry("tsmom", params=params),),
        portfolio=PortfolioParams(covariance_halflife=48, vol_halflife=24, max_weight=0.15, max_gross=0.6),
        interval="1h",
        min_history_bars=0,
    )


@pytest.fixture
def world(august_panel: Panel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    venue = FakeVenue(balance=10_000.0, prices={s: float(august_panel.close[s].iloc[cursor - 1]) for s in SYMBOLS})

    class Service(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            dead_man.orders_out_at_each_ping.append(len(venue.order_log))
            self.send_response(dead_man.status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _LocalServer(("127.0.0.1", 0), Service)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    dead_man = DeadMan(url=f"http://127.0.0.1:{server.server_address[1]}/ping/beidou-live", venue=venue)
    # `httpx` reads proxies from the environment, and on the Mac from the system settings whenever the
    # environment names none - which would send a request for 127.0.0.1 to the proxy.  NO_PROXY alone is
    # an environment entry, so it both exempts the host and stops the fallback.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv(deadman.LOOP_URL_ENV, dead_man.url)

    store = StateStore(tmp_path / "live")
    config = LiveConfig(  # type: ignore[arg-type]
        interval="1h",
        history_bars=300,
        universe=tuple(SYMBOLS),
        leverage=2,
        rebalance=RebalanceParams(no_trade_band=0.002),
        guards=GuardParams(),
        kill_switch_path=tmp_path / "KILL_SWITCH",
        strategy_weights={"tsmom": 1.0},
        poll_interval_seconds=0.0,
        grace_seconds=1.0,
    )
    engine = LiveEngine(
        config,
        model=_model(),
        market=market,
        venue=venue,
        clock=FakeClock(market.bar_open_ms(cursor) + 5_000),
        store=store,
        deadman_url=deadman.loop_url(),
    )
    try:
        yield {
            "engine": engine,
            "store": store,
            "market": market,
            "dead_man": dead_man,
            "bar": market.bar_open_ms(cursor - 1),
        }
    finally:
        server.shutdown()
        server.server_close()


async def test_the_loop_pings_only_after_an_ok_cycle(world: dict[str, Any]) -> None:
    engine, store, market, dead_man = world["engine"], world["store"], world["market"], world["dead_man"]
    await engine.startup()

    # OK: the cycle trades, and the ping arrives with every one of its orders already out.
    assert await engine.guarded_cycle(world["bar"]) is not None
    orders_after_ok = len(dead_man.venue.order_log)

    # ERROR after the decisions: orders may be on the venue, and the cycle still did not complete.
    async def boom(_reports: Any) -> list[str]:
        raise RuntimeError("after the orders")

    engine._quarantine = boom  # type: ignore[method-assign]
    market.cursor += 1
    assert await engine.guarded_cycle(world["bar"] + HOUR_MS) is None

    # Guard-skipped: the feed is three bars behind, so nothing is traded and no exit is checked.
    market.cursor += 1
    market.lag_bars = 3
    skipped = await engine.guarded_cycle(world["bar"] + 2 * HOUR_MS)
    assert skipped is not None and skipped["skip"] is True

    ok, failed, stale = store.read_jsonl(store.cycles_path)[-3:]
    assert "phase" not in ok and ok["deadman"] is True
    assert not undeclared(ok), "a key the six reading modules have no declaration to read against"
    assert failed["phase"] == "ERROR" and "deadman" not in failed
    assert stale["skip"] is True and "deadman" not in stale
    assert orders_after_ok > 0, "precondition: the OK cycle placed orders, so their order against the ping is visible"
    assert dead_man.orders_out_at_each_ping == [orders_after_ok], (
        "one ping, and only once the OK cycle's orders were out"
    )


async def test_a_failed_ping_is_written_down_and_costs_the_cycle_nothing(world: dict[str, Any]) -> None:
    """The service said no: the row says so, and the cycle is as OK as it would have been."""
    engine, store, dead_man = world["engine"], world["store"], world["dead_man"]
    dead_man.status = 503
    await engine.startup()

    record = await engine.guarded_cycle(world["bar"])

    assert record is not None and engine.consecutive_errors == 0
    row = store.read_jsonl(store.cycles_path)[-1]
    assert "phase" not in row and row["deadman"] is False
    assert len(dead_man.orders_out_at_each_ping) == 1


async def test_no_url_means_no_ping_and_no_key(world: dict[str, Any]) -> None:
    """The default: every row written before WP-R1, and every process but the armed loop, looks like this."""
    engine, store, dead_man = world["engine"], world["store"], world["dead_man"]
    engine.deadman_url = ""
    await engine.startup()

    assert await engine.guarded_cycle(world["bar"]) is not None

    assert "deadman" not in store.read_jsonl(store.cycles_path)[-1]
    assert dead_man.orders_out_at_each_ping == []
