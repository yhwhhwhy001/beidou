"""Two clauses of the plan that were never delivered, found by reading it against the code.

DL-L3 asks for dedup **and** `run_check.sh` 同源去重 - the same deduplication in the check script.
KILL-R7's evidence was 36 identical FAIL lines over 36 hours, and those came from the HOURLY CHECK
JOB, not from the loop: the check runs as a fresh process every hour, so an in-memory dedup dict is
empty every time and cannot suppress anything.  Dedup that only works inside one long-lived process
is dedup aimed away from the case that produced the finding.

DL-L5's last clause asks that a non-empty `foreign_positions` at startup 告警而非静默 - alert rather
than stay silent.  It logs.  A line in a file nobody reads is what "silent" means to an operator, and
foreign positions are money on the venue the loop has decided not to manage.
"""

from __future__ import annotations

from pathlib import Path

import httpx


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0})


def _counting(counter: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(1)
        return httpx.Response(200, json={"code": 0})

    return httpx.MockTransport(handler)


async def test_dedup_survives_the_process_that_did_the_deduping(tmp_path: Path) -> None:
    """The hourly check job is a new process every hour; in-memory state helps it not at all."""
    from beidou_live.alerts import WebhookAlerts

    posts: list[int] = []
    state = tmp_path / "alert-state.json"
    for _ in range(5):  # five separate "processes"
        alerts = WebhookAlerts("https://open.feishu.cn/x", transport=_counting(posts), state_path=state)
        await alerts.send("the same standing problem", key="check-status")

    assert len(posts) == 1


async def test_the_window_still_re_announces_a_standing_problem(tmp_path: Path) -> None:
    """Hourly, not never: KILL-R7 wants the fifth hour quiet and the next window loud."""
    from beidou_live.alerts import WebhookAlerts

    posts: list[int] = []
    state = tmp_path / "alert-state.json"
    clock = [0.0]
    for _ in range(3):
        alerts = WebhookAlerts(
            "https://open.feishu.cn/x",
            transport=_counting(posts),
            state_path=state,
            dedup_window_seconds=3600.0,
            clock=lambda: clock[0],
        )
        await alerts.send("standing", key="k")
    assert len(posts) == 1

    clock[0] = 3601.0
    alerts = WebhookAlerts(
        "https://open.feishu.cn/x",
        transport=_counting(posts),
        state_path=state,
        dedup_window_seconds=3600.0,
        clock=lambda: clock[0],
    )
    await alerts.send("standing", key="k")

    assert len(posts) == 2


async def test_a_corrupt_state_file_costs_a_duplicate_not_an_alert(tmp_path: Path) -> None:
    """This is a cache.  It may never be the reason a message does not go out."""
    from beidou_live.alerts import WebhookAlerts

    state = tmp_path / "alert-state.json"
    state.write_text("{not json", encoding="utf-8")
    posts: list[int] = []

    alerts = WebhookAlerts("https://open.feishu.cn/x", transport=_counting(posts), state_path=state)

    assert await alerts.send("hello", key="k") is True
    assert len(posts) == 1


async def test_only_a_delivered_message_is_remembered(tmp_path: Path) -> None:
    """A dropped message must not suppress the retry an hour later."""
    from beidou_live.alerts import WebhookAlerts

    state = tmp_path / "alert-state.json"
    rejecting = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 19002}))
    assert (
        await WebhookAlerts("https://open.feishu.cn/x", transport=rejecting, state_path=state).send("x", key="k")
        is False
    )

    posts: list[int] = []
    assert (
        await WebhookAlerts("https://open.feishu.cn/x", transport=_counting(posts), state_path=state).send("x", key="k")
        is True
    )
    assert len(posts) == 1


async def test_a_foreign_position_at_startup_alerts(tmp_path: Path) -> None:
    """DL-L5's last clause: money on the venue the loop has decided not to manage."""
    from tests.live.helpers_liquidation import startup_with_foreign_position

    sent = await startup_with_foreign_position(["DOGEUSDT"])

    assert any("DOGEUSDT" in message and "不在本循环管理" in message for message in sent), sent


async def test_a_clean_startup_says_nothing(tmp_path: Path) -> None:
    from tests.live.helpers_liquidation import startup_with_foreign_position

    sent = await startup_with_foreign_position([])

    assert not any("foreign" in message.lower() for message in sent)


def test_the_check_script_uses_the_shared_dedup_state() -> None:
    """`run_check.sh` must pass a state path, or its dedup is the in-memory one that cannot work."""
    script = Path("deploy/run_check.sh").read_text(encoding="utf-8")

    assert "state_path" in script
    assert "force=True" not in script


async def test_a_foreign_open_order_at_startup_alerts() -> None:
    """AC-L5's other half, and the same shape as DL-L5's foreign positions.

    The FILTER is right and verified against the real venue (2026-09-07: a hand-placed
    `manual-acl5-…` order survived a real `startup_reconcile` with `cancel_stale_orders=True`).  What
    is missing is telling anyone.  A resting order the loop did not place is either the operator's or
    a leftover from something that crashed, and both are things you want to hear about at startup
    rather than discover in a fill.
    """
    from tests.live.helpers_liquidation import startup_with_foreign_order

    sent = await startup_with_foreign_order("manual-acl5-1")

    assert any("manual-acl5-1" in m and "挂单" in m for m in sent), sent


async def test_our_own_resting_order_is_not_announced_as_foreign() -> None:
    """`bd-` orders are cancelled by the reconcile; announcing them would be noise every restart."""
    from tests.live.helpers_liquidation import startup_with_foreign_order

    sent = await startup_with_foreign_order("bd-BTCUSDT-123")

    assert not any("foreign" in m.lower() and "order" in m.lower() for m in sent), sent
