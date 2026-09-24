"""Whether each bar was traded on time and as planned.

Checklist area #10 of `docs/analysis/2026-09-23-external-prompt-checklist-vs-beidou.md`, the part
`execution_fidelity.py` (M-Q08) does not hold: restart cost and failed bars (M-Q03), the no-trade
band's plan gaps, and the host clock (D-025).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from beidou_live.report_common import _day_of, _fmt_num
from beidou_live.risk_budget import RiskBudgetParams
from beidou_live.scheduler import ALREADY_REBALANCED_REASON, BACKOFF_REASON, MISSED_REBALANCE_REASON
from beidou_live.soak import _decided
from beidou_live.state import StateStore


def plan_gaps(store: StateStore, day: str) -> dict[str, Any]:
    """Symbols the planner could not act on, and why - including the band's own live falsifier.

    ``blocked_entry`` and ``blocked_exit`` are the structural cases: a target smaller than the absolute
    band can never open from flat, and a position smaller than the band can never be closed to zero, so
    those symbols are stuck for as long as the target stays that size.  They are worth naming because
    they look identical, in every other instrument, to a symbol that simply did not need trading.
    ``band_held`` counts the ordinary suppressed resize, which is what P10 cell B registered as its live
    falsifier when it widened ``no_trade_rel_band`` to 0.40 and predicted roughly 12% less turnover.
    """
    rows = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    gaps = [gap for row in rows for gap in (row.get("skipped") or [])]
    counted: dict[str, int] = {}
    for gap in gaps:
        counted[str(gap.get("reason"))] = counted.get(str(gap.get("reason")), 0) + 1
    return {
        "by_reason": counted,
        "band_held": counted.get("NO_TRADE_BAND", 0),
        "blocked_entry": sorted({str(g.get("symbol")) for g in gaps if g.get("reason") == "BAND_BLOCKS_ENTRY"}),
        "blocked_exit": sorted({str(g.get("symbol")) for g in gaps if g.get("reason") == "BAND_BLOCKS_EXIT"}),
    }


def clock_health(store: StateStore, day: str, *, interval_ms: int = 3_600_000) -> dict[str, Any]:
    """D-025: how far this day's cycles sat from the venue clock, and whether the report's own labels are wrong.

    The host clock is the trading reference and a whole-interval offset is an accepted state, so this is
    not an alert.  It is here because every timestamp in this report - including ``day`` itself - is the
    host's, and on 2026-09-04 the host was a full hour behind the venue while the report said nothing.
    ``label_skew_hours`` is how much to add to a label in this file to get venue time.
    """
    rows = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    skews = [
        float(c["skew_ms"]) for row in rows if isinstance((c := row.get("clock") or {}).get("skew_ms"), int | float)
    ]
    if not skews:
        return {"cycles_probed": 0, "labels_reliable": None}
    last = skews[-1]
    alignments = [((s + interval_ms / 2) % interval_ms) - interval_ms / 2 for s in skews]
    worst = max(alignments, key=abs)
    return {
        "cycles_probed": len(skews),
        "venue_minus_host_seconds": round(last / 1000.0, 1),
        "label_skew_hours": round(last / interval_ms) * (interval_ms / 3_600_000.0),
        "worst_distance_from_bar_boundary_seconds": round(worst / 1000.0, 1),
        "jumped_cycles": sum(1 for row in rows if (row.get("clock") or {}).get("jumped")),
        # a whole-bar offset does not endanger trading; it does make every timestamp written here wrong
        "labels_reliable": abs(last) < interval_ms / 2,
        "trading_reference": "host clock (D-025); the offset is accepted, the remainder is what is guarded",
    }


def _restart_cost_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """M-Q03 against its own bar.  Before 2026-09-09 these numbers were rendered and judged by nothing."""
    if not block:
        return {"missed_rebalances": 0}
    limits = block.get("limits") or {}
    share = block.get("late_cycle_share")
    return {
        "status": block.get("status"),
        "missed_rebalances": f"{block.get('missed_rebalances')} (M-Q03 阈值 {limits.get('missed_rebalances')})",
        # Split out from 2026-09-16, because the two halves send a reader to different places: a skip is
        # a restart or a backoff and is read in the deploy log, a failed bar is a request that did not
        # come back and is read on the path to the venue.
        "of_which_cycles_failed": block.get("failed_bars", 0),
        "late_cycle_share": (
            "无可判周期（没有周期记下它当时的窗口）"
            if share is None
            else f"{share:.1%} of {block.get('scheduled_cycles_measurable')} 个已排定周期 "
            f"(M-Q03 阈值 {float(limits.get('late_cycle_share', 0.0)):.0%}；重启不计)"
        ),
        "worst_late_seconds": _fmt_num(block.get("worst_late_seconds")),
        # Restarts are reported under their own name rather than inside the lateness they used to be
        # the whole of.  Both facts stay visible; only the arithmetic stopped mixing them.
        "restarts": block.get("restarts"),
        "worst_restart_late_seconds": _fmt_num(block.get("worst_restart_late_seconds")),
        "unreadable_wakes": block.get("unreadable_wakes"),
        "widest_rebalance_window_seconds": _fmt_num(block.get("widest_window_seconds")),
        # the half M-Q03 is named for, reported without a bar - see `restart_cost`
        "worst_late_fill_seconds": _fmt_num(block.get("worst_late_fill_seconds")),
        "fills_measured": block.get("fills_measured"),
        "reasons": block.get("reasons") or [],
    }


def _woke_seconds_after_close(row: Mapping[str, Any], interval_ms: int) -> float | None:
    """How late a scheduled cycle woke, from what the row already carries.

    No new field was needed for this: every cycle row has written `at` and `bar_open_ms` from the
    beginning (202 of 202 on the live record).  The lateness of every cycle was recorded and only the
    reader was missing, which is this repository's most-repeated defect in its most literal form.

    `None` when the row cannot say - a torn write, or a row from before these fields existed.  An
    unreadable row is a cycle whose punctuality is unknown, not a punctual cycle, so it is counted
    apart rather than folded into either side.
    """
    bar = row.get("bar_open_ms")
    at = row.get("at")
    if not isinstance(bar, int | float) or not isinstance(at, str):
        return None
    try:
        woke_ms = datetime.fromisoformat(at).timestamp() * 1000.0
    except ValueError:
        return None
    return max(0.0, (woke_ms - (float(bar) + interval_ms)) / 1000.0)


def restart_cost(
    rows: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]] = (),
    params: RiskBudgetParams | None = None,
    *,
    interval_ms: int = 3_600_000,
) -> dict[str, Any]:
    """M-Q03 / AC-L4 / RISK-P2: what the day's restarts cost, counted AND judged rather than assumed.

    DL-L4 wrote both facts into every cycle row - how late the wake-up was relative to the bar close,
    and whether that lateness cost a rebalance - and nothing read them.  The plan assumed every
    deployment restart costs late fills and 7bps of gross; this is the number that would show whether
    the assumption is generous or mean.

    The thresholds are M-Q03's own, from the 2026-09-06 remediation plan ("<= 5% / 0", baseline
    "稳态 0/8"), transcribed into ``risk_budget`` in the profile so they have a reader.  Until
    2026-09-09 they were compared to nothing: the four numbers below were rendered and left there.

    Two things a reader should not have to discover.  First, ``missed_rebalances``, ``skipped_bars`` and
    ``late_bars`` are the SAME rows today, not three facts that happen to agree: the engine writes
    ``late_seconds`` into a cycle row only in ``_record_missed_rebalance``, which also sets the phase and
    the reason.  They are kept apart because they are different questions and a future engine could
    separate them, but nobody should read agreement between them as corroboration.

    Second, this counts miss ROWS and no longer reconstructs the engine's running total.  That counter
    is reset by every restart, so three consecutive processes that each missed once write 1, 1, 1 and a
    drop-detector reports one miss - which is exactly what happened on 2026-09-09, where the record
    holds three miss rows.  It was also wrong the other way, charging a process that spanned midnight
    with yesterday's misses.  Every miss appends exactly one row, so the rows are the count.

    The FILL half is reported and deliberately not judged.  M-Q03 is named for late fills, the fills
    carry their own ``late_seconds``, and the worst one ever written is 26.3s against the 72-98s
    rebalance windows the engine actually used - so this half has never been the binding one, and
    picking a seconds bar for it would be the invented number KILL-R6 refuted.  The plan's preferred
    bar-hour weighting is not computable either: nothing records how long a late-entered position was
    held.  KILL-R6 named the per-cycle share as the acceptable reading and that is what is judged.
    """
    params = params or RiskBudgetParams()
    restarts = unreadable = 0
    # The bar a cycle FAILED on, charged here from 2026-09-16.  The 2026-09-13 work charged the bars
    # the backoff SLEPT THROUGH and left this one open in its own docstring; four bars since
    # 2026-09-08 have no successful cycle and M-Q03 read zero on every one of those days.  Charged by
    # BAR rather than by row, because a bar is what a rebalance belongs to: `lost` collects the bars
    # that failed deciding nothing, `reached` the bars that got there anyway (a later retry, or a
    # restart that found the book already set), and the difference is what is owed.
    lost: set[int] = set()
    reached: set[int] = set()
    # The bar each charging skip row names, settled against the failed bars at the end.  Until
    # 2026-09-24 a skip row's bar went into `reached` as it was read, so a restart that found a bar a
    # failed cycle had already lost took the charge over: restart #56 turned the 16:01Z proxy 503 on
    # 2026-09-23's 15:00 bar into "查重启原因" and withdrew the failed-bar page.  The miss was one bar
    # either way; which row owns it is what sends the reader somewhere.
    skip_charges: list[object] = []
    failures: list[str] = []
    windows: list[float] = []
    # How late each SCHEDULED cycle woke, from its own row, paired with the bar that row allowed it.
    # The annotation is the fix for a contradiction, not decoration: this was `list[float]` while the
    # append below put a `(woke, window)` pair in it and the line under `widest` unpacked one back
    # out.  It ran correctly - the tuple went in and came out - which is exactly why nobody reread it
    # when the pairing was added.  A declared element type is what makes the next such edit fail loudly.
    wakes: list[tuple[float, float | None]] = []
    restart_late: list[float] = []
    for row in rows:
        reason = row.get("reason")
        skipped = row.get("phase") == "SKIPPED" or reason == MISSED_REBALANCE_REASON
        if skipped:
            # A backoff is not a restart.  These rows arrived on 2026-09-13, when the failure backoff
            # began writing one SKIPPED row per bar it slept through - without which M-Q03's threshold
            # of zero could not see them at all (the loop stayed up, so nothing else recorded the gap).
            # They are genuine MISSES and are charged below; counting them as restarts too would make a
            # single six-hour outage read as six process restarts and inflate `worst_restart_late`,
            # which is the number the failure action "查重启原因" sends someone to look at.
            if reason != BACKOFF_REASON:
                restarts += 1
            # A bar that was already rebalanced cannot have had its rebalance missed.  The row still
            # carries the window the engine allowed, so `widest_window_seconds` keeps it; only the miss
            # COUNT declines to charge it, and a failure on the same bar is excused: the book did get set.
            if reason != ALREADY_REBALANCED_REASON:
                skip_charges.append(row.get("bar_open_ms"))
            elif isinstance(bar := row.get("bar_open_ms"), int):
                reached.add(bar)
            if isinstance(window := row.get("window_seconds"), int | float):
                windows.append(float(window))
            if reason != BACKOFF_REASON and isinstance(value := row.get("late_seconds"), int | float):
                restart_late.append(float(value))
            continue
        # The bar is per row when the row carries it (every completed cycle does, from 2026-09-10) and
        # falls back to the widest the engine allowed that day, for rows written before it did.
        if isinstance(window := row.get("window_seconds"), int | float):
            windows.append(float(window))
        # A cycle can fail AFTER placing orders - `run_cycle` places them and only then quarantines,
        # summarizes and finishes - and that bar WAS rebalanced.  The five keys that answer it are
        # already written onto the ERROR row for L3, and are read here through the same function, so a
        # sixth cannot be added to one side alone.  Failed rows go on to the lateness accounting below
        # unchanged: the cycle did wake, and it is only the rebalance that was lost.
        if isinstance(bar := row.get("bar_open_ms"), int):
            if row.get("phase") == "ERROR" and not _decided(row):
                lost.add(bar)
                failures.append(str(row.get("error") or "未记录"))
            else:
                reached.add(bar)
        woke = _woke_seconds_after_close(row, interval_ms)
        if woke is None:
            unreadable += 1
        else:
            wakes.append((woke, float(window) if isinstance(window, int | float) else None))
    fills = [float(t["late_seconds"]) for t in trades if isinstance(t.get("late_seconds"), int | float)]
    # The bar is the window the ENGINE allowed, read off the rows rather than recomputed here - the
    # same refusal `widest_window_seconds` already makes.  With no window in the record there is
    # nothing to measure against, and `None` says so; a day with nothing to compare is not a day
    # nothing was late on, and a zero here would read as a pass.
    widest = max(windows) if windows else None
    judged = [(value, bar if bar is not None else widest) for value, bar in wakes]
    late = [value for value, bar in judged if bar is not None and value > bar]
    measurable = [value for value, bar in judged if bar is not None]
    share = (len(late) / len(measurable)) if measurable else None
    failed = lost - reached
    # A skip row on a failed bar is the restart finding that bar already lost: not a second miss.
    skips = sum(1 for bar in skip_charges if bar not in failed)
    failed_bars = len(failed)
    missed = skips + failed_bars
    reasons: list[str] = []
    if missed > params.max_missed_rebalances:
        # The failure action names what actually happened.  "查重启原因" was the only one offered, and
        # on the four bars this reporter could not see until 2026-09-16 there was no restart to read:
        # the loop stayed up and a request to the venue failed.
        worst_restart = max(restart_late) if restart_late else 0.0
        where = (
            f"其中 {failed_bars} 根是周期失败（{failures[-1]}）；失败动作：查这条路径，不是重启"
            if failed_bars
            else f"最迟的一次在 bar 收盘后 {worst_restart:.0f} 秒；失败动作：查重启原因"
        )
        reasons.append(f"漏掉 {missed} 次再平衡（M-Q03 阈值 {params.max_missed_rebalances}）；{where}")
    if share is not None and share > params.max_late_cycle_share:
        reasons.append(
            f"迟到周期占比 {share:.1%} 高于 M-Q03 的 {params.max_late_cycle_share:.0%}"
            f"（{len(late)}/{len(measurable)} 个已排定周期在再平衡窗口外醒来；重启不计）"
        )
    return {
        "cycles": len(rows),
        "missed_rebalances": missed,
        # Kept apart from 2026-09-16.  These were the same rows and the docstring above said a future
        # engine could separate them; a failed bar is a miss and is not a skip, so they now differ.
        "skipped_bars": skips,
        "failed_bars": failed_bars,
        # Named separately from the reason line because `daily_alerts` pages on this half and an alert
        # that says only "1 根 bar" sends the reader to open the report to find out what broke.
        "failed_bar_error": failures[-1] if failures else None,
        # Restarts are counted and named rather than folded into the lateness they used to inflate.
        "restarts": restarts,
        "worst_restart_late_seconds": max(restart_late) if restart_late else None,
        "unreadable_wakes": unreadable,
        "scheduled_cycles": len(wakes),
        "scheduled_cycles_measurable": len(measurable),
        "worst_late_seconds": max(late) if late else None,
        "late_bars": len(late),
        "late_cycle_share": share,
        # What the engine ACTUALLY allowed, read off the rows rather than re-derived here: the window is
        # grace + launchd's ThrottleInterval + this process's measured startup, so it differs per restart
        # and a copy of the formula in the reporter would drift away from the one that decided.
        "widest_window_seconds": max(windows) if windows else None,
        "worst_late_fill_seconds": max(fills) if fills else None,
        "fills_measured": len(fills),
        "limits": {
            "late_cycle_share": params.max_late_cycle_share,
            "missed_rebalances": params.max_missed_rebalances,
        },
        "status": "ALERT" if reasons else "OK",
        "reasons": reasons,
    }
