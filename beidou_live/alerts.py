"""Optional webhook alerts (Slack/Telegram-style JSON POST).  Empty URL = disabled."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class WebhookAlerts:
    def __init__(self, url: str = "", timeout: float = 10.0) -> None:
        self.url = url.strip()
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self.url, json={"text": text})
                return response.status_code < 300
        except httpx.HTTPError as exc:
            logger.warning("alert delivery failed: %s", exc)
            return False
