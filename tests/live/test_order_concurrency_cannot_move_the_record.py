"""Serial order sending inflates the very slippage it is measured by (2026-09-13 perf item P3).

`execute_order` sleeps 1.0s up to `poll_attempts` times whenever an ack is not terminal, and the loop
sends one order at a time, so the LAST order of a bar can leave tens of seconds after the first.  The
further from the decision the fill is, the further the price is from `decision_close` - and
`decision_close` is exactly what M-Q08 measures slippage against, recorded on every trade row
(`engine._record_fill`).  So the sending strategy pushes the number that judges the sending strategy.

`order_concurrency` is the lever, and it is off (1) by default.  Two properties have to hold before it
can ever be turned on, and they are what this module pins:

* at 1 the path is the serial loop it has always been - same sends, same order, same rows;
* above 1 the ARRIVAL order changes and the RECORD does not.  `store.append_trade` and
  `record["orders"]` are the append-only trade log and the cycle row that every later reader
  reconstructs a bar from, and neither may follow whichever venue answered first.

Double-sending is not among the risks: `client_order_id` is derived from the bar and `execute_order`
queries before it submits (D-032 / T-L01).  What concurrency changes is latency and arrival order,
which is why those two are the things under test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from beidou_alpha.panel import Panel
from beidou_live.engine import LiveEngine
from beidou_live.state import StateStore
from beidou_shared.types import OrderAck, OrderRequest
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import _config, _model, _prices


class _StaggeredVenue(FakeVenue):
    """Answers in an order of its own choosing, so "arrival order" is a real variable and not luck."""

    def __init__(self, *, yields: dict[str, int] | None = None, fail_symbol: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.yields = dict(yields or {})
        self.fail_symbol = fail_symbol
        self.completions: list[str] = []

    async def place_order(self, request: OrderRequest) -> OrderAck:
        for _ in range(self.yields.get(request.symbol, 0)):
            await asyncio.sleep(0)
        if request.symbol == self.fail_symbol:
            raise RuntimeError("the transport went away mid-batch")
        ack = await super().place_order(request)
        self.completions.append(request.symbol)
        return ack


async def _cycle(
    august_panel: Panel,
    directory: Path,
    *,
    concurrency: int,
    yields: dict[str, int] | None = None,
    fail_symbol: str | None = None,
) -> dict[str, Any]:
    cursor = 400
    market = FakeMarketData(august_panel, cursor)
    venue = _StaggeredVenue(
        balance=10_000.0, prices=_prices(august_panel, cursor), yields=yields, fail_symbol=fail_symbol
    )
    store = StateStore(directory / "live")
    engine = LiveEngine(
        _config(directory),
        model=_model(),
        market=market,
        venue=venue,
        clock=FakeClock(market.bar_open_ms(cursor) + 5_000),
        store=store,
        order_concurrency=concurrency,
    )
    await engine.startup()
    record: dict[str, Any] = {"orders": []}
    failure: BaseException | None = None
    try:
        record = await engine.run_cycle(market.bar_open_ms(cursor - 1))
    except BaseException as exc:  # only the injected one; re-raised by the caller that asked for it
        failure = exc
    return {
        "record": record,
        "venue": venue,
        "store": store,
        "failure": failure,
        "planned": [order["symbol"] for order in record["orders"]],
        "traded": [row["symbol"] for row in store.read_jsonl(store.trades_path)],
    }


def _comparable(record: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Everything about an order row that is not a clock reading."""
    return [
        (o["symbol"], o["side"], o["quantity"], o["status"], o["reduce_only"], o["client_order_id"])
        for o in record["orders"]
    ]


async def test_the_default_path_is_the_serial_loop(august_panel: Panel, tmp_path: Path) -> None:
    """Concurrency 1 sends one at a time: the venue answers in exactly the planned order."""
    run = await _cycle(august_panel, tmp_path, concurrency=1)

    assert len(run["planned"]) >= 2, "the fixture needs more than one order for any of this to mean anything"
    assert run["venue"].completions == run["planned"]
    assert run["traded"] == run["planned"]


async def test_concurrency_reorders_the_venue_and_not_the_record(august_panel: Panel, tmp_path: Path) -> None:
    """The arrival order is deliberately reversed.  The trade log and the cycle row do not notice."""
    serial = await _cycle(august_panel, tmp_path / "serial", concurrency=1)
    planned = serial["planned"]
    # The first order planned yields the most, so it is the last to come back.
    reversing = {symbol: len(planned) - 1 - index for index, symbol in enumerate(planned)}

    parallel = await _cycle(august_panel, tmp_path / "parallel", concurrency=4, yields=reversing)

    assert parallel["venue"].completions == list(reversed(planned)), "the fixture really did overlap them"
    assert parallel["planned"] == planned, "the cycle row stays in planned order"
    assert parallel["traded"] == planned, "and so does the append-only trade log"


async def test_the_two_paths_produce_the_same_bar(august_panel: Panel, tmp_path: Path) -> None:
    """Same orders, same statuses, same book.  Concurrency is a latency change and nothing else."""
    serial = await _cycle(august_panel, tmp_path / "serial", concurrency=1)
    reversing = {symbol: len(serial["planned"]) - 1 - index for index, symbol in enumerate(serial["planned"])}
    parallel = await _cycle(august_panel, tmp_path / "parallel", concurrency=4, yields=reversing)

    assert _comparable(parallel["record"]) == _comparable(serial["record"])
    assert parallel["venue"].qty == serial["venue"].qty
    assert parallel["record"]["targets"] == serial["record"]["targets"]
    assert parallel["record"]["summary"] == serial["record"]["summary"]


async def test_one_order_blowing_up_still_records_the_ones_that_came_back(august_panel: Panel, tmp_path: Path) -> None:
    """What the serial loop does too: everything already sent is written down, then the failure propagates.

    The batch is gathered with `return_exceptions=True` for this: without it the first raise reaches
    the caller while the other sends are still in flight, and orders that DID reach the venue would
    finish as orphans outside the cycle that owns them - fills with no row anywhere.
    """
    serial = await _cycle(august_panel, tmp_path / "serial", concurrency=1)
    planned = serial["planned"]
    victim = planned[0]
    reversing = {symbol: len(planned) - 1 - index for index, symbol in enumerate(planned)}

    run = await _cycle(august_panel, tmp_path / "parallel", concurrency=4, yields=reversing, fail_symbol=victim)

    assert isinstance(run["failure"], RuntimeError), "the cycle fails, as it does today"
    assert run["traded"] == [symbol for symbol in planned if symbol != victim]
    assert run["venue"].completions == list(reversed(run["traded"])), "they really were in flight together"
