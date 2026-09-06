"""Optional webhook alerts (Slack/Telegram-style JSON POST).  Empty URL = disabled.

Two properties an unattended loop needs from its only output channel, added 2026-09-06 (DL-L3):

*Deduplication.*  A repeated alert is how the one that matters gets missed - measured in this repo's
own logs as 36 identical FAIL lines over 36 hours, unhandled (KILL-R7).  The same alert is sent once
per ``dedup_window_seconds``; a standing problem still re-announces itself hourly rather than never,
and ``clear`` re-arms a key when the condition it describes goes away.

*A second channel.*  One URL is a single point of silence.  Both are tried, and delivery to either
counts - which matters because the breaker (DL-L2) now stops the loop cleanly, and it is only
allowed to do that if it can first say so out loud.  ``force`` bypasses dedup for exactly that case,
and the return value is what lets the caller refuse to exit quietly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)


class WebhookAlerts:
    def __init__(
        self,
        url: str = "",
        timeout: float = 10.0,
        *,
        secondary_url: str = "",
        dedup_window_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.strip()
        self.secondary_url = secondary_url.strip()
        self._timeout = timeout
        self._window = float(dedup_window_seconds)
        self._clock = clock
        self._transport = transport
        self._last_sent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.url or self.secondary_url)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(u for u in (self.url, self.secondary_url) if u)

    def clear(self, key: str) -> None:
        """Forget a key so the next alert under it fires: the condition it described has cleared."""
        self._last_sent.pop(key, None)

    def _suppressed(self, key: str) -> bool:
        last = self._last_sent.get(key)
        return last is not None and (self._clock() - last) < self._window

    async def send(self, text: str, *, key: str | None = None, force: bool = False) -> bool:
        """Deliver ``text``; return True only if a channel accepted it.

        A suppressed duplicate returns False - it was not delivered, and a caller that needs to know
        its message got out (the breaker) can tell the difference.
        """
        if not self.enabled:
            return False
        fingerprint = key if key is not None else text
        if not force and self._suppressed(fingerprint):
            logger.debug("alert suppressed as a duplicate: %s", fingerprint)
            return False
        delivered = False
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for url in self.urls:
                try:
                    response = await client.post(url, json={"text": text})
                except httpx.HTTPError as exc:
                    logger.warning("alert delivery failed (%s): %s", url, exc)
                    continue
                if response.status_code < 300:
                    delivered = True
                else:
                    logger.warning("alert rejected by %s: HTTP %s", url, response.status_code)
        if delivered:
            self._last_sent[fingerprint] = self._clock()
        return delivered
