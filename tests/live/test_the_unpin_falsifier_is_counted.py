"""2026-09-15 取消钉住时写下的证伪判据，此前没有任何代码去数。

`config/alpha_registry.yaml` 取消钉住那段（操作者裁定）写着：「若任一标的连续 5 天记 BAND_BLOCKS_ENTRY 且没被重排
踢出池子，这条说法即被证伪，届时重议」。日报的 Plan gaps 一节只列当天被挡的名字，不数连续天数，所以判据写下之后
没有东西能告诉操作者它什么时候满足。

2026-09-30 的审查读实盘记录：k 切到 0.175 之后，AKEUSDT、LSKUSDT、NEARUSDT 的目标不到权益的 1%，D3 不让开仓。
从 09-27T15:00Z 起，51 个完整周期里三者全部记 BAND_BLOCKS_ENTRY，而且一直在池里。按日历约 10-02T15:00Z 满五天。

这里钉住计数的口径：只有做过计划的周期算证据；在池里却没被挡、或者出了池，都算断；没有计划的周期（跳过、失败、
护栏停书、主机睡着的那几个小时）既不续也不断，但最宽的空档要报出来，因为空档算不算「连续」由操作者读。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beidou_live.report_execution import UNPIN_FALSIFIER_DAYS, unpin_falsifier_line
from beidou_live.reports import daily_alerts, plan_gaps
from beidou_live.state import StateStore

START = datetime(2026, 9, 27, 15, tzinfo=UTC)


def _row(at: datetime, blocked: list[str], pool: list[str], **extra: Any) -> dict[str, Any]:
    """A planned cycle as the loop writes it: `skipped` and `universe` are lists, `phase` is absent."""
    ms = int(at.timestamp() * 1000)
    return {
        "bar": at.isoformat(),
        "bar_open_ms": ms,
        "as_of_ms": ms,
        "equity": 10_000.0,
        "universe": pool,
        "skipped": [
            {"symbol": symbol, "reason": "BAND_BLOCKS_ENTRY", "delta_notional": 40.0, "threshold": 100.0}
            for symbol in blocked
        ]
        + [{"symbol": "BTCUSDT", "reason": "NO_TRADE_BAND", "delta_notional": 6.0, "threshold": 145.9}],
        **extra,
    }


def _hourly(store: StateStore, start: datetime, hours: int, blocked: list[str], pool: list[str]) -> datetime:
    for hour in range(hours):
        store.append_cycle(_row(start + timedelta(hours=hour), blocked, pool))
    return start + timedelta(hours=hours)


def test_five_days_blocked_inside_the_pool_meets_the_falsifier(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    pool = ["BTCUSDT", "AKEUSDT", "LSKUSDT"]
    _hourly(store, START, 5 * 24 + 1, ["AKEUSDT", "LSKUSDT"], pool)  # 09-27T15:00 .. 10-02T15:00

    before = plan_gaps(store, "2026-10-01")
    assert before["blocked_entry_streaks"]["AKEUSDT"]["days"] < UNPIN_FALSIFIER_DAYS
    assert before["unpin_falsifier"]["met"] == [], "four days and eight hours is not five"

    gaps = plan_gaps(store, "2026-10-02")
    streak = gaps["blocked_entry_streaks"]["AKEUSDT"]
    assert streak["since"] == "2026-09-27T15:00:00+00:00"
    assert streak["days"] == 5.0 and streak["cycles"] == 121 and streak["widest_gap_hours"] == 1.0
    assert gaps["unpin_falsifier"] == {"days": 5.0, "met": ["AKEUSDT", "LSKUSDT"]}
    assert gaps["blocked_entry"] == ["AKEUSDT", "LSKUSDT"], "the day's own list is unchanged"


def test_a_planned_cycle_that_did_not_block_it_or_dropped_it_from_the_pool_breaks_the_streak(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    pool = ["BTCUSDT", "AKEUSDT", "NEARUSDT"]
    at = _hourly(store, START, 3 * 24, ["AKEUSDT", "NEARUSDT"], pool)
    store.append_cycle(_row(at, ["AKEUSDT"], ["BTCUSDT", "AKEUSDT"]))  # 15:00: NEARUSDT left the pool
    store.append_cycle(_row(at + timedelta(hours=1), ["NEARUSDT"], pool))  # 16:00: back; AKEUSDT traded
    _hourly(store, at + timedelta(hours=2), 2 * 24 - 1, ["AKEUSDT", "NEARUSDT"], pool)  # 17:00 .. 10-02T15:00

    gaps = plan_gaps(store, "2026-10-02")
    assert gaps["blocked_entry_streaks"]["AKEUSDT"]["since"] == "2026-09-30T17:00:00+00:00", "traded in the pool"
    assert gaps["blocked_entry_streaks"]["NEARUSDT"]["since"] == "2026-09-30T16:00:00+00:00", "left the pool"
    assert gaps["unpin_falsifier"]["met"] == [], "a streak starts again after the break, it does not resume"


def test_cycles_that_planned_nothing_neither_extend_nor_break_it(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    pool = ["BTCUSDT", "AKEUSDT"]
    at = _hourly(store, START, 24, ["AKEUSDT"], pool)
    ms = int(at.timestamp() * 1000)
    # What the loop writes when it did not plan: a restart's SKIPPED row, a failed cycle, a guard stop.
    store.append_cycle({"bar": at.isoformat(), "bar_open_ms": ms, "phase": "SKIPPED", "reason": "restart"})
    store.append_cycle({"bar": at.isoformat(), "bar_open_ms": ms, "phase": "ERROR", "error": "ReadError"})
    store.append_cycle({**_row(at, [], pool), "skip": True, "skipped": []})
    # ...and then ten hours with no row at all, the host asleep, before the book resumes, still blocked.
    _hourly(store, at + timedelta(hours=11), 4 * 24 - 11 + 1, ["AKEUSDT"], pool)

    streak = plan_gaps(store, "2026-10-02")["blocked_entry_streaks"]["AKEUSDT"]
    assert streak["since"] == "2026-09-27T15:00:00+00:00", "nothing that planned nothing reset it"
    assert streak["days"] == 5.0
    assert streak["cycles"] == 24 + 4 * 24 - 11 + 1, "only planned cycles are counted"
    assert streak["widest_gap_hours"] == 12.0, "the hole is reported, since whether it breaks 'consecutive' is read"


def test_it_is_a_notice_for_review_and_never_an_alert(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live")
    _hourly(store, START, 5 * 24 + 1, ["AKEUSDT"], ["BTCUSDT", "AKEUSDT"])
    met = {"plan_gaps": plan_gaps(store, "2026-10-02")}
    alerts, notices = daily_alerts(met)
    assert alerts == [], "what the falsifier asks for is a discussion, not an action inside the hour"
    [notice] = [n for n in notices if "证伪" in n]
    assert "AKEUSDT 5.0 天" in notice and "届时重议" in notice

    quiet = {"plan_gaps": plan_gaps(store, "2026-09-30")}
    assert not [n for n in daily_alerts(quiet)[1] if "证伪" in n], "not met: only the Plan gaps block says how far"
    assert unpin_falsifier_line(quiet["plan_gaps"]) == "未满足：最长 AKEUSDT 3.3 天，要 5 天"
    assert unpin_falsifier_line({}) == "无：没有标的连续被挡", "a payload written before the counter still renders"
