"""A webhook URL is a credential, so no log line may carry one.

Until 2026-09-30 a failed delivery logged `alert delivery failed (<the whole URL>): <exc>`.  A Lark bot's
token IS the URL's last path segment, and this repository copies log lines into `docs/RESEARCH_LOG.md`,
which is public.  Failed deliveries on 2026-09-29 put the URL into 39 lines of four local logs - one of
them `check.stdout.log`, because `run_check.sh` captures `report daily`'s stderr and passes it on as the
text of its own alert.

Every URL here is fake, and every negative assertion is on a string the code under test cannot write by
itself: `MARKER` appears nowhere in `beidou_live/alerts.py` or `beidou_cli/live_cmd.py`, so a log line can
only contain it by having been handed the URL.  Each one is paired with a positive assertion, so a log
line that disappeared altogether cannot pass for one that was cleaned.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
import pytest

from beidou_cli.live_cmd import _logging
from beidou_live.alerts import WebhookAlerts, redacted

MARKER = "zzfake0hook0marker0test0only0c3f"
URL = f"https://open.larksuite.com/open-apis/bot/v2/hook/{MARKER}"
DIGEST = hashlib.sha256(URL.encode("utf-8")).hexdigest()[:8]


def _raising(make: type[httpx.TransportError]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise make("", request=request)

    return httpx.MockTransport(handler)


def _written(caplog: pytest.LogCaptureFixture) -> str:
    """What `beidou_live.alerts` wrote.  httpx's own request line is the verbose test's business."""
    return "\n".join(record.getMessage() for record in caplog.records if record.name == "beidou_live.alerts")


async def test_a_failed_delivery_names_the_channel_and_the_error_but_not_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each warning of 2026-09-29 ended in `): `: a ReadError's or ReadTimeout's message is empty."""
    caplog.set_level(logging.DEBUG, logger="beidou_live.alerts")
    alerts = WebhookAlerts(URL, transport=_raising(httpx.ReadError))

    assert await alerts.send("演练") is False

    assert MARKER not in _written(caplog)
    assert DIGEST in _written(caplog)
    assert "ReadError" in _written(caplog)


@pytest.mark.parametrize("url", [URL, URL.replace("open.larksuite.com", "OPEN.Larksuite.com")])
async def test_an_error_whose_message_spells_the_url_is_scrubbed(url: str, caplog: pytest.LogCaptureFixture) -> None:
    """httpx 0.28's `HTTPStatusError` puts the whole URL in its message, host case normalised by httpx."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, request=request).raise_for_status()

    caplog.set_level(logging.DEBUG, logger="beidou_live.alerts")
    alerts = WebhookAlerts(url, transport=httpx.MockTransport(handler))

    assert await alerts.send("演练") is False

    assert MARKER not in _written(caplog)
    assert "HTTPStatusError" in _written(caplog)
    assert "502 Bad Gateway" in _written(caplog)


async def test_a_rejection_that_quotes_the_token_back_is_scrubbed_before_it_is_cut(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reply is cut to 200 characters; cut first and a token straddling the cut keeps its head."""
    body = "x" * 190 + MARKER
    caplog.set_level(logging.DEBUG, logger="beidou_live.alerts")
    alerts = WebhookAlerts(URL, transport=httpx.MockTransport(lambda request: httpx.Response(403, text=body)))

    assert await alerts.send("演练") is False

    assert MARKER[:10] not in _written(caplog)
    assert "HTTP 403 " + "x" * 190 in _written(caplog)
    assert DIGEST in _written(caplog)


async def test_verbose_logging_keeps_httpx_request_lines_out(caplog: pytest.LogCaptureFixture) -> None:
    """httpx logs each request at INFO with its full URL; `live run --verbose` used to let that through."""
    httpx_logger = logging.getLogger("httpx")
    before = httpx_logger.level
    alerts = WebhookAlerts(URL, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})))
    caplog.set_level(logging.DEBUG)
    try:
        # A fresh process: another test's `_logging(False)` must not stand in for this one's.
        httpx_logger.setLevel(logging.NOTSET)
        assert await alerts.send("演练", force=True) is True
        assert MARKER in caplog.text, "control: with httpx left alone its request line carries the URL"
        caplog.clear()

        _logging(verbose=True)
        assert await alerts.send("演练", force=True) is True
    finally:
        httpx_logger.setLevel(before)

    assert MARKER not in caplog.text


def test_two_channels_on_one_host_are_told_apart() -> None:
    """Both channels can be Lark bots, so the host alone cannot say which one failed."""
    other = URL.replace(MARKER, MARKER[::-1])

    assert redacted(URL) != redacted(other)
    assert "open.larksuite.com" in redacted(URL)
    assert MARKER not in redacted(URL) and MARKER[::-1] not in redacted(other)


def test_credentials_in_the_authority_are_not_shown_either() -> None:
    label = redacted(f"https://user:{MARKER}@relay.example/hook")

    assert MARKER not in label
    assert "relay.example" in label
