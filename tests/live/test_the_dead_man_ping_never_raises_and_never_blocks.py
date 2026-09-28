"""`deadman.ping` may cost a cycle nothing: it never raises, and it gives up on time (WP-R1).

The loop awaits it after the orders are out and before the cycle row is written.  A ping that raised
would turn a cycle that traded into an ERROR row; one that hung would hold the row, the heartbeat and
the next bar behind somebody else's server.  And the URL is a token, so no failure may print it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

import httpx
import pytest

from beidou_live import deadman

TOKEN = "token-that-must-never-reach-a-log"
URL = f"https://dead-man.invalid/ping/{TOKEN}"


async def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)


async def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(f"timed out reading {request.url}", request=request)


async def _bug(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"not an httpx error at all, and it names {request.url}")


async def _hangs(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(3600)
    return httpx.Response(200)


@pytest.mark.parametrize("transport", [_refused, _timed_out, _bug, _hangs], ids=lambda f: f.__name__.strip("_"))
async def test_the_dead_man_ping_never_raises_and_never_blocks(
    transport: Callable[[httpx.Request], Awaitable[httpx.Response]], caplog: pytest.LogCaptureFixture
) -> None:
    """False, promptly, and logged without the URL - whatever the transport does.

    The hang runs at `timeout=0.5` rather than paying the default's 5 s of suite time on every run: the
    bound is the one outer `asyncio.timeout`, the same line at either value, and the default is pinned
    below.  5 s is the most a cycle can wait, against a rebalance window of about 90 s.
    """
    caplog.set_level(logging.DEBUG, logger=deadman.__name__)
    started = time.monotonic()

    # The test's own bound, so an implementation that lost its timeout fails here in 10 s instead of
    # holding the suite for the transport's hour.
    delivered = await asyncio.wait_for(deadman.ping(URL, timeout=0.5, transport=httpx.MockTransport(transport)), 10)

    assert delivered is False
    assert time.monotonic() - started < 1.5
    own = [record.getMessage() for record in caplog.records if record.name == deadman.__name__]
    assert own, "a failed ping must leave a line in the loop's log"
    assert not [line for line in own if TOKEN in line], f"the ping URL is a token and reached the log: {own}"


def test_the_default_timeout_is_the_bound_the_cycle_is_promised() -> None:
    assert inspect.signature(deadman.ping).parameters["timeout"].default == 5.0


@pytest.mark.parametrize(("status", "delivered"), [(200, True), (204, True), (404, False), (503, False)])
async def test_only_a_2xx_counts_as_delivered(status: int, delivered: bool) -> None:
    """healthchecks.io answers 404 for a check it does not know: a mistyped URL must read as a failure."""
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(status)

    assert await deadman.ping(URL, transport=httpx.MockTransport(answer)) is delivered
    assert seen == ["GET"]


def test_the_url_is_read_from_the_environment_and_absent_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tests/conftest.py` deletes every `BEIDOU_DEADMAN_*` before each test, so absent is the start state."""
    assert deadman.loop_url() == ""
    monkeypatch.setenv(deadman.LOOP_URL_ENV, f"  {URL}\n")
    assert deadman.loop_url() == URL
