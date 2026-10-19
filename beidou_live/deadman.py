"""The loop's ping to a dead-man service off this host (WP-R1: D-P4 reopened, 2026-09-28).

Every alert this repository sends leaves from the machine the loop runs on, so a host that is off,
asleep or offline takes its own watchers down with it and the page count is zero (E-PR20/37).  A
dead-man service turns that around: the loop pings it after each cycle that gets through execution,
and the SERVICE pages when the pings stop, from a machine that is still up.

*It can never cost a cycle.*  It runs after the orders are out, it is bounded by ``timeout`` in total
rather than per phase, and every error it can meet comes back as False, written onto the cycle's row.
One lost ping costs nothing: the service's grace absorbs it.

*The URL is a token, not a knob.*  Whoever holds it can ping on the loop's behalf and keep the page
from ever firing.  So it sits beside the credentials in the environment, not in the profile or in
`LiveConfig` - rotating it must move no digest - and no log line here names it: an ``httpx`` error
message can carry the URL it was about, so failures are logged by type alone.  `live_cmd` hands it
only to the process trading the account.  The shadow soak evals the same exports, and its OK cycles
would keep the check green after the armed loop had died.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence

import httpx

logger = logging.getLogger(__name__)

LOOP_URL_ENV = "BEIDOU_DEADMAN_LOOP_URL"
#: The mainnet loop's own check (切换方案 M7, 2026-10-19).  Two loops pinging one check would keep it green while
#: either is alive, which is the silence a dead-man exists to end.
MAINNET_URL_ENV = "BEIDOU_DEADMAN_MAINNET_URL"


def loop_url(env: str = LOOP_URL_ENV) -> str:
    """The loop's ping URL, or "" when none is exported - and then the loop pings nothing."""
    return os.environ.get(env, "").strip()


def mainnet_watch_problems(alert_urls: Sequence[str], deadman_url: str) -> list[str]:
    """What a mainnet loop is missing to be watched; empty when it may start (M7, operator's card「作为硬门」).

    Two alert channels, because one URL is a single point of silence (DL-L3), and an off-host dead-man, because
    every channel is posted from this host and a host that is off posts nothing: 10-03 to 10-08 the demo loop was
    down 5.5 days and nobody was told.  Names only, never values: the URLs are tokens.
    """
    problems = []
    if len([url for url in alert_urls if url]) < 2:
        problems.append("two alert channels are required (alerts.webhook_url and alerts.webhook_url_2)")
    if not deadman_url:
        problems.append(f"no off-host dead-man ({MAINNET_URL_ENV} is not in the environment)")
    return problems


async def ping(url: str, *, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """GET ``url``; True only on a 2xx.  Never raises, and returns within ``timeout`` seconds.

    ``httpx``'s own timeout bounds each phase separately, so the outer ``asyncio.timeout`` is what bounds
    the call.  ``Exception`` rather than ``BaseException``: a cancellation is the loop stopping, not the
    ping failing, and swallowing it would turn a stop into a hang.
    """
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.get(url)
    except Exception as exc:
        logger.warning("dead-man ping failed: %s", type(exc).__name__)
        return False
    if not response.is_success:
        logger.warning("dead-man ping answered HTTP %s", response.status_code)
    return response.is_success
