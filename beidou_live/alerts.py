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
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# Lark/Feishu custom bots take `{"msg_type": "text", "content": {"text": ...}}`; Slack and most
# generic relays take the flat `{"text": ...}`.  Sending one shape to both is how a channel dies
# quietly: Slack accepts the flat form, and Lark answers HTTP 200 and drops it.
LARK_HOSTS = ("open.larksuite.com", "open.feishu.cn")


def payload_for(url: str, text: str) -> dict[str, object]:
    """The body this provider actually reads."""
    host = urlparse(url).netloc.lower()
    if any(host == known or host.endswith(f".{known}") for known in LARK_HOSTS):
        return {"msg_type": "text", "content": {"text": text}}
    return {"text": text}


def accepted(response: httpx.Response) -> bool:
    """Did the provider say it took the message?

    A status code is the weakest possible evidence here, and for this repository's one channel it is
    the wrong evidence: a Lark custom bot answers HTTP 200 with `{"code": 19001}` when it cannot read
    the payload, so `status_code < 300` reads a dropped message as a delivery - and the breaker is
    allowed to exit quietly on exactly that boolean (RISK-P1).

    The rule is narrow on purpose: when the provider states its own result, believe the provider; when
    it does not, keep trusting the status.  It can turn a false success into a failure, never the
    reverse.
    """
    if response.status_code >= 300:
        return False
    try:
        body = response.json()
    except ValueError:
        return True
    if not isinstance(body, dict):
        return True
    for field in ("code", "StatusCode"):
        value = body.get(field)
        if isinstance(value, int):
            return value == 0
    return True


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
                    response = await client.post(url, json=payload_for(url, text))
                except httpx.HTTPError as exc:
                    logger.warning("alert delivery failed (%s): %s", url, exc)
                    continue
                if accepted(response):
                    delivered = True
                else:
                    logger.warning("alert rejected by %s: HTTP %s %s", url, response.status_code, response.text[:200])
        if delivered:
            self._last_sent[fingerprint] = self._clock()
        return delivered
