"""Daily report from the live state files (markdown + json)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.overlays.exits import COOLDOWN
from beidou_alpha.panel import interval_seconds
from beidou_alpha.report import render_markdown
from beidou_alpha.validation.metrics import (
    DECAY_WINDOW_DAYS,
    max_drawdown,
    newey_west_tstat,
    sharpe,
    window_sharpes,
)
from beidou_data.metrics_snapshot import metrics_parity
from beidou_data.store import KlineStore, MetricsStore
from beidou_live.health import canonical_construction
from beidou_live.probe import ProbeParams, probe_status
from beidou_live.risk_budget import RiskBudgetParams, books_by_symbol, collateral_drift, risk_budget_status
from beidou_live.scheduler import ALREADY_REBALANCED_REASON, MISSED_REBALANCE_REASON
from beidou_live.state import StateStore

# A ratchet: raise it only in the commit that says why.  2026-09-08, 0.50 -> 0.76.  0.50 was declared
# "not a derived threshold", placed between 0.13 measured 2026-09-05 and 1.0 for stage 1 deleted - and
# that 0.13 came from a book where the probe reached one of sixteen names.  It now reaches four of
# eighteen (D-019), which is why the reading crossed at 2026-09-08T04:00Z; stage 1 was intact on all
# fourteen other names to four decimals.  Re-derived under a rule fixed before it ran, over the 88
# live cycles carrying `asset_vol`: working max 0.581, deleted min 1.000, ratio 1.72 against a
# pre-registered 1.5 gate; limit = their geometric mean.  Method, counterfactual and full numbers:
# `scratchpad/m015_recalibrate_with_probe.py` (its docstring is the pre-registration).  Kept there
# rather than copied here, so a correction is made once.
#
# What it does NOT buy: `compression`'s denominator is the smallest risk contribution, which on the
# overlaid names is a near-cancellation between two books (floor so far 0.295 of the median), so the
# statistic is unbounded above for reasons unrelated to stage 1.  Alert again from a cancellation and
# the answer is to measure the main book separately, NOT to raise this a second time.
RISK_COMPRESSION_LIMIT = 0.76


def _day_of(record: dict[str, Any]) -> str | None:
    """The UTC day a record belongs to, preferring the bar its data carried (D-025).

    ``bar_open_ms`` is derived from the host clock, which is the reference for scheduling but may sit a
    whole bar away from the venue's; ``as_of_ms`` comes from the klines themselves.  Bucketing by the
    data keeps a day's report describing the day that was actually traded.
    """
    for key in ("as_of_ms", "bar_open_ms"):
        value = record.get(key)
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d")
    stamp = record.get("at")
    return str(stamp)[:10] if stamp else None


def expectations_from_evidence(evidence_by_strategy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What the validation reports promised: OOS Sharpe, full-sample Sharpe/MDD per strategy."""
    out: dict[str, Any] = {}
    for strategy, report in evidence_by_strategy.items():
        verdict = report.get("verdict")
        if str(report.get("kind", "")) == "book":  # a probe book (D-019): expectations are the sleeve's own numbers
            universes = report.get("universes", {}) or {}
            decision = universes.get(str(report.get("universe_mode", "")), {}) or {}
            standalone = decision.get("sleeve_standalone", {}) or {}
            wf = standalone.get("walk_forward", {}) or {}
            full = standalone.get("full_sample", {}) or {}
            verdict = f"{report.get('book_verdict')}/book (sleeve {standalone.get('verdict')})"
        else:
            wf = report.get("walk_forward", {}) or {}
            full = report.get("full_sample", {}) or {}
        out[strategy] = {
            "oos_sharpe": wf.get("oos_sharpe"),
            # The decay rule's comparison distribution, carried from the same evidence run as the Sharpe
            # above so the two can never describe different constructions (report 4.2 Ⅰ).  Absent from
            # every report written before 2026-09-07, which is why `decay_watch` reads INSUFFICIENT_DATA
            # until each strategy's evidence is next re-run.
            "oos_window_sharpe_q10": wf.get("oos_window_sharpe_q10"),
            "full_sample_sharpe": full.get("annualized_sharpe"),
            "full_sample_max_drawdown": full.get("max_drawdown"),
            "verdict": verdict,
        }
    return out


def _day_end_ms(day: str) -> int:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return int((start + timedelta(days=1)).timestamp() * 1000)


def probe_rows(
    store: StateStore, probes: Sequence[ProbeParams], *, equity: float | None, now_ms: int
) -> list[dict[str, Any]]:
    """Status of every probe book (D-019) from the attribution file and the persisted stop records."""
    if not probes:
        return []
    attributions = store.read_jsonl(store.attribution_path)
    stopped = store.load().stopped_books
    rows: list[dict[str, Any]] = []
    for probe in probes:
        status = probe_status(probe, attributions, equity=equity, now_ms=now_ms)
        if probe.book in stopped:
            status = {**status, "status": "STOPPED", "stopped": stopped[probe.book]}
        rows.append(status)
    return rows


DAY_MS = 86_400_000


def _cycles(store: StateStore, *, window_days: int | None = None, now_ms: int | None = None) -> list[dict[str, Any]]:
    """Traded cycles, newest last.  A failed cycle carries no equity and is not a bar that happened."""
    rows = [
        row for row in store.read_jsonl(store.cycles_path) if row.get("equity") is not None and not row.get("dry_run")
    ]
    if window_days is None:
        return rows
    cutoff = (now_ms or 0) - window_days * DAY_MS
    return (
        [row for row in rows if int(row.get("bar_open_ms") or 0) >= cutoff] if now_ms else rows[-(window_days * 24) :]
    )


def evidence_window(store: StateStore) -> dict[str, Any]:
    """When the currently running construction started (D-026 fingerprint), i.e. when live evidence begins.

    Every change to the book - a signal parameter, a band, a half-life - resets what the live record is
    evidence *of*.  Adopting `conviction_mode: sign` and then P10 cell B on the same day made this
    concrete: the numbers before each change describe a different book.  M-010's 30-day window has to
    start here, not at the first cycle ever recorded.
    """
    rows = [row for row in _cycles(store) if row.get("construction")]
    if not rows:
        return {"construction": None, "since_ms": None, "bars": 0, "changes_7d": 0}
    # Compared through `canonical_construction`, because a digest can move without the book moving: the
    # fingerprint's own field set grew twice on 2026-09-07 (P22, P23) and each time this window reset to
    # one bar while every construction VALUE was identical.  Old rows keep the digest they were written
    # with - history is not rewritten - and the equivalence is declared in code, with its proof.
    current = canonical_construction(rows[-1]["construction"])
    since = rows[-1]
    for row in reversed(rows):
        if canonical_construction(row.get("construction")) != current:
            break
        since = row
    latest_ms = int(rows[-1].get("bar_open_ms") or 0)
    recent = [row for row in rows if int(row.get("bar_open_ms") or 0) >= latest_ms - 7 * DAY_MS]
    changes = sum(
        1
        for a, b in pairwise(recent)
        if canonical_construction(a.get("construction")) != canonical_construction(b.get("construction"))
    )
    return {
        "construction": str(current)[:12],
        "since_ms": int(since.get("bar_open_ms") or 0),
        "bars": sum(1 for row in rows if canonical_construction(row.get("construction")) == current),
        "changes_7d": changes,
    }


def _series_by_strategy(store: StateStore, since_ms: int | None) -> dict[str, list[tuple[int, float]]]:
    """Attributed P&L per strategy, ON THE CYCLE GRID - one point per completed cycle, zero where
    that cycle attributed nothing to that strategy.

    An attribution row is written only by a cycle that had income, and both readers of this series
    (`income_drift` for M-002/M-010, `long_run_attribution` for M-G06) annualise it at 8760 bars a
    year and read its length as `size / 24` days.  Measured 2026-09-12 over the 214 cycles the
    attribution record spans: 41 rows for tsmom (19% of cycles) and 25 for flow (12%).  So the series
    was 5x shorter than the hours it covered and every gap - a true zero, since these rows carry only
    realised P&L, commission and funding - had been deleted from it:

        tsmom   days read 1.71 against 8.92 actual;  annualised Sharpe -15.84 against -6.78 (2.33x)
        flow    days read 1.04 against 8.92 actual;  annualised Sharpe  -2.38 against -0.77 (3.08x)

    The inflation is exactly sqrt(cycles/rows) (2.28 and 2.93 predicted): dropping the zeros leaves the
    sum alone and shrinks the count, so the mean rises faster than the standard deviation.  The sign is
    untouched, which is why M-G06 - a point estimate >= 0 and nothing else - would have ruled the same
    either way; M-010 compares against a FIXED expected Sharpe, and that comparison was not safe.

    The grid is bounded by the attribution record's own span.  Zero-filling back to the first cycle
    would assert "no income" over a period when nothing was writing income rows at all.
    """
    attributed: dict[str, dict[int, float]] = {}
    covered: list[int] = []
    for row in store.read_jsonl(store.attribution_path):
        bar = row.get("bar_open_ms")
        if not isinstance(bar, int | float):
            continue
        covered.append(int(bar))
        for strategy, value in (row.get("by_strategy") or {}).items():
            try:
                attributed.setdefault(str(strategy), {})[int(bar)] = float(value)
            except (TypeError, ValueError):
                continue
    if not attributed or not covered:
        return {}
    first, last = min(covered), max(covered)
    grid = sorted(
        {
            int(row["bar_open_ms"])
            for row in store.read_jsonl(store.cycles_path)
            if not row.get("dry_run")
            and isinstance(row.get("bar_open_ms"), int | float)
            and first <= int(row["bar_open_ms"]) <= last
            and (since_ms is None or int(row["bar_open_ms"]) >= since_ms)
        }
    )
    if not grid:  # a record with attribution but no readable cycle rows: report what there is
        grid = sorted({bar for bars in attributed.values() for bar in bars if since_ms is None or bar >= since_ms})
    return {strategy: [(bar, bars.get(bar, 0.0)) for bar in grid] for strategy, bars in sorted(attributed.items())}


def income_drift(
    store: StateStore,
    expectations: dict[str, Any],
    *,
    equity: float | None,
    since_ms: int | None,
    bars_per_year: float = 8760.0,
) -> dict[str, Any]:
    """M-002 / M-010: each strategy's realised Sharpe from *attributed income*, against its own expectation.

    The equity-based `drift_check` cannot do this.  Equity here is multi-asset collateral, so it moves
    with BTC even when the book is flat (KILL-033), and it is one number for a book that runs a main
    strategy plus a probe sleeve.  Income rows are per strategy and contain only realised P&L, commission
    and funding, which is what the backtest Sharpe was computed from.
    """
    if not equity or equity <= 0:
        return {"status": "INSUFFICIENT_DATA", "reason": "no equity"}
    rows: dict[str, Any] = {}
    status = "OK"
    for strategy, points in sorted(_series_by_strategy(store, since_ms).items()):
        values = np.asarray([value / equity for _bar, value in points], dtype=float)
        expected = (expectations.get(strategy) or {}).get("oos_sharpe")
        realised = sharpe(values, bars_per_year) if _has_dispersion(values) else None
        days = values.size / 24.0
        z = None
        if realised is not None and expected is not None:
            z = (realised - expected) / math.sqrt(365.0 / max(days, 1.0))
            if z < -2.0:
                status = "ALERT"
        rows[strategy] = {
            "bars": int(values.size),
            "days": round(days, 2),
            "realised_sharpe": realised,
            "expected_sharpe": expected,
            "z": z,
            "pnl": float(values.sum() * equity),
        }
    if not rows or all(row["realised_sharpe"] is None for row in rows.values()):
        status = "INSUFFICIENT_DATA"
    return {"status": status, "by_strategy": rows}


def decay_watch(
    store: StateStore,
    expectations: dict[str, Any],
    *,
    equity: float | None,
    window_days: int = DECAY_WINDOW_DAYS,
    bars_per_year: float = 8760.0,
) -> dict[str, Any]:
    """Per strategy, the adopted decay rule against the whole income history (not just today).

    The window is `window_days` of hourly bars and the history is read from bar zero, because the rule
    is about the live period as a whole; every other number in this report is about one day.

    `q10` is looked up on the strategy's own evidence block.  It is not there yet for any strategy, so
    every row reads INSUFFICIENT_DATA and says which half is missing.  That is the correct reading today
    and it is meant to stay visible until somebody computes the quantile - a blank row would be read as
    "fine".
    """
    bars = int(window_days * 24)
    rows: dict[str, Any] = {}
    for strategy, points in sorted(_series_by_strategy(store, None).items()):
        if not equity or equity <= 0:
            rows[strategy] = {"status": "INSUFFICIENT_DATA", "why": "no equity", "below": 0}
            continue
        returns = [value / equity for _bar, value in points]
        windows = window_sharpes(returns, bars_per_window=bars, bars_per_year=bars_per_year)
        q10 = (expectations.get(strategy) or {}).get("oos_window_sharpe_q10")
        verdict = decay_verdict(live_windows=windows, q10=q10)
        rows[strategy] = verdict | {"whole_windows": len(windows), "window_days": window_days}
    return rows


def decay_verdict(*, live_windows: Sequence[float | None], q10: float | None, consecutive: int = 2) -> dict[str, Any]:
    """The edge-decay rule adopted 2026-09-07 (report 4.2 Ⅰ; ruling in 12.9).

    Two consecutive non-overlapping 30-day windows whose live Sharpe sits below the backtest's q10 for
    windows of the same length -> REVIEW.  The comparison is to an EMPIRICAL distribution, not to
    ``oos_sharpe`` with a normal standard error: a 1.7-Sharpe strategy has enormous 30-day dispersion, so
    its q10 is likely well below zero, and the point-estimate test is much weaker.  That weaker test still
    exists as `drift_vs_expectation`; the two coexist and answer different questions.

    A missing ``q10`` is INSUFFICIENT_DATA and never OK.  The quantile must come from the same
    construction, so it dies with every construction change exactly as M-010's window does; reporting OK
    while there is nothing to compare against is how a detector that has never worked looks healthiest.
    """
    usable = [value for value in live_windows if value is not None]
    if q10 is None:
        return {"status": "INSUFFICIENT_DATA", "why": "no q10 for this construction yet", "below": 0}
    if len(usable) < consecutive:
        return {
            "status": "INSUFFICIENT_DATA",
            "why": f"{len(usable)} whole windows, needs {consecutive}",
            "below": 0,
        }
    tail = usable[-consecutive:]
    below = sum(1 for value in tail if value < q10)
    status = "REVIEW" if below == consecutive else "OK"
    return {"status": status, "below": below, "q10": q10, "windows": tail}


M_G06_WINDOW_MONTHS = 18  # §19 Q2's lagging criterion; not a dial, and shortening it is not an option


def _plus_months(stamp: datetime, months: int) -> datetime:
    """Calendar months, clamped to the shorter month (there is no 31st of February)."""
    total = stamp.month - 1 + months
    year, month = stamp.year + total // 12, total % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return stamp.replace(year=year, month=month, day=min(stamp.day, last[month - 1]))


def long_run_sharpe(
    store: StateStore,
    *,
    equity: float | None,
    now: datetime | None = None,
    window_months: int = M_G06_WINDOW_MONTHS,
    bars_per_year: float = 8760.0,
) -> dict[str, Any]:
    """M-G06: per strategy, the attributed annualised Sharpe over an unbroken 18-month construction.

    §19 Q2 (2026-09-08) split "持续盈利" in two.  M-010 is the leading half and `income_drift` is where
    it lives; this is the lagging half, and until 2026-09-09 it was the one row of the metrics table
    with no code at all.  Every clause below is quoted from that ruling rather than chosen here:

    * **Per strategy, in M-010's units.**  Attributed P&L from the income rows over the day's equity -
      literally `income_drift`'s own series - because §19 says the two must be comparable, and equity
      cannot do it: this account is multi-asset collateral, so equity moves with BTC while the book is
      flat (KILL-033), and it is one number for a main sleeve plus a probe sleeve.  A single equity
      denominator across eighteen months is an approximation, and it is M-010's approximation; using a
      better one here would buy accuracy at the cost of the only thing the ruling asked for.
    * **Timed by `canonical_construction`.**  `evidence_window` already answers this, aliases included,
      so a renamed fingerprint field cannot restart the clock (it moved three times in one day).
    * **Point estimate >= 0, and nothing else.**  `nw_t` is computed and reported and decides nothing,
      which is the disposition D-P2 gave t.  Eighteen months buys a Sharpe standard error of about
      0.82, so a passing sleeve with |t| < 2 is the EXPECTED outcome; gating on t would make M-G06
      unreachable, i.e. would turn the lagging criterion off while appearing to strengthen it.
    * **Failure action "该策略退出 main"**, carried on the row rather than left in the plan.

    Elapsed time is counted in BARS UNDER THE CONSTRUCTION (`bars / 24`), the same reading the audit
    quotes for M-010 as "5.00/30 天".  A wall clock would let eighteen months pass while the loop was
    down and call that evidence.  The calendar is still reported beside it - `calendar_days` and the
    `downtime_days` between them - because `judgeable_from` is a DATE and the countdown is in bars, and
    a reader given only one of those two units would be entitled to assume they are the same thing.
    They are equal only while the loop misses nothing; every hour it is down pushes the date right.

    One asymmetry a reader will notice and should not have to work out: the window's `elapsed_days`
    counts CYCLE rows while each strategy's `days` counts its own ATTRIBUTION rows, and an attribution
    row is written only when that strategy had income - so on 2026-09-09 the window reads 5.17 days
    while tsmom reads 0.83.  That is M-010's series verbatim (`income_drift` divides the same way), and
    matching it is the one thing §19 Q2 asked of this metric; a denser series with the flat bars filled
    in would be a better estimator and would make the two criteria incomparable, which is the trade the
    ruling already decided.  It is named here rather than smoothed over.

    **Today this returns INSUFFICIENT_DATA and that is the answer, not a gap.**  The construction
    running now began 2026-09-04T14:00Z, so the window closes 2028-03-04 - about 542 days away as of
    2026-09-09.  `sharpe_so_far` is reported beside `sharpe_standard_error` precisely so the early
    number cannot be mistaken for a verdict: at five days that error is 8.5.
    """
    window = evidence_window(store)
    since_ms = window["since_ms"]
    block: dict[str, Any] = {
        "status": "INSUFFICIENT_DATA",
        "window_months": window_months,
        "construction": window["construction"],
        "since": None,
        "judgeable_from": None,
        "bars": window["bars"],
        "elapsed_days": round(window["bars"] / 24.0, 4),
        "required_days": None,
        "days_remaining": None,
        "by_strategy": {},
    }
    if since_ms is None:
        return {**block, "why": "no cycle has recorded a construction, so there is nothing to time"}
    since = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
    closes = _plus_months(since, window_months)
    required = (closes - since).days
    elapsed = block["elapsed_days"]
    calendar = max(0.0, ((now or datetime.now(UTC)) - since).total_seconds() / 86_400.0)
    block |= {
        "since": since.isoformat(),
        # The earliest this could close, i.e. assuming the loop misses nothing from here on.
        "judgeable_from": closes.isoformat(),
        "required_days": required,
        "days_remaining": round(max(0.0, required - elapsed), 4),
        "calendar_days": round(calendar, 4),
        # Calendar the record does not cover.  Not an error: the countdown simply did not advance.
        "downtime_days": round(max(0.0, calendar - elapsed), 4),
    }
    if not equity or equity <= 0:
        return {**block, "why": "no equity to express attributed P&L against (M-010's denominator)"}
    open_yet = elapsed < required
    why = (
        f"构造不变 {elapsed:.2f}/{required} 天（{window_months} 个月），最早可判 {closes.date()}" if open_yet else None
    )
    rows: dict[str, Any] = {}
    worst = "OK"
    for strategy, points in sorted(_series_by_strategy(store, since_ms).items()):
        values = np.asarray([value / equity for _bar, value in points], dtype=float)
        days = values.size / 24.0
        point = sharpe(values, bars_per_year) if _has_dispersion(values) else None
        hac = newey_west_tstat(values) if values.size >= 3 else {"t_stat": None}
        if open_yet or point is None:
            status, action = "INSUFFICIENT_DATA", None
        elif point >= 0.0:
            status, action = "OK", None
        else:
            status, action = "FAIL", "该策略退出 main（§19 Q2 滞后判据 M-G06）"
        if status == "FAIL":
            worst = "FAIL"
        elif status == "INSUFFICIENT_DATA" and worst == "OK":
            worst = "INSUFFICIENT_DATA"
        rows[strategy] = {
            "bars": int(values.size),
            "days": round(days, 2),
            # Named for what it is.  It is not the criterion until `judgeable_from`, and a key called
            # `sharpe` would be read as one on the first day the report renders it.
            "sharpe_so_far": point,
            "sharpe_standard_error": math.sqrt(365.0 / days) if days > 0 else None,
            "nw_t": hac["t_stat"],  # reported, never a gate (D-P2)
            "status": status,
            "action": action,
            "why": why if open_yet else ("点估计 < 0" if status == "FAIL" else None),
        }
    if not rows:
        return {**block, "why": why or "no attribution row lands inside this construction"}
    return {**block, "status": "INSUFFICIENT_DATA" if open_yet else worst, "why": why, "by_strategy": rows}


def metrics_parity_status(symbols: Sequence[str], data_root: str | Path) -> dict[str, Any]:
    """M-011 across the managed universe: archive against snapshot, per symbol, folded to one number.

    Two refusals rather than one convenient number.  A symbol with no overlap contributes nothing and
    is counted separately - `metrics_parity` already returns `rate: None` for that case, because zero
    disagreements out of zero comparisons is not agreement, and a dead snapshot stream would otherwise
    look healthiest exactly while it stopped recording.  And the fold takes the WORST symbol, not the
    mean: a book trades a universe, so parity the thinnest symbol does not have is not parity.
    """
    archive, snapshot = MetricsStore(data_root), MetricsStore(data_root, kind="metrics_snapshot")
    compared: dict[str, float] = {}
    unmeasurable: list[str] = []
    for symbol in symbols:
        result = metrics_parity(snapshot.load(symbol), archive.load(symbol))
        if result["rate"] is None:
            unmeasurable.append(symbol)
        else:
            compared[symbol] = float(result["rate"])
    if not compared:
        return {
            "enforced": False,
            "reason": f"no overlapping buckets for any of {len(symbols)} symbols",
            "unmeasurable": unmeasurable,
        }
    worst = max(compared, key=lambda symbol: compared[symbol])
    return {
        "enforced": True,
        "symbols_compared": len(compared),
        "unmeasurable": unmeasurable,
        "worst_symbol": worst,
        "worst_differing_rate": compared[worst],
        "met": not unmeasurable and compared[worst] == 0.0,
    }


def collateral_share(*, equity: float, usdt_equity: float | None) -> dict[str, float | None]:
    """L1-10: the part of `equity` that is collateral rather than the book's own currency.

    The demo account is on multi-assets margin, so `totalMarginBalance` - what `drawdown_state` and the
    vol sizing divide by - carries non-USDT assets valued at mark.  BTC moves and measured equity moves
    with it on a bar where the book did nothing, so a drawdown reading that trips the ladder can belong
    to BTC rather than to the strategy.

    Reported, never enforced, and deliberately NOT subtracted from the equity the book sizes on: changing
    that denominator changes every position size, which is a construction change - it resets M-010's
    window and is the operator's decision on its own merits, not a bug fix. What was missing is that the
    divergence was invisible, and that is what this closes.

    `None` rather than 0.0 when the venue did not report a USDT balance: zero would read as "all of it is
    collateral", which is the opposite of "we do not know" - the `metrics_parity` lesson again.
    """
    if usdt_equity is None or equity <= 0:
        return {"equity": equity, "usdt_equity": usdt_equity, "collateral": None, "share": None}
    collateral = equity - usdt_equity
    return {"equity": equity, "usdt_equity": usdt_equity, "collateral": collateral, "share": collateral / equity}


def _has_dispersion(values: np.ndarray, *, minimum: int = 48) -> bool:
    """Enough bars, and a spread wider than floating-point noise.

    A strategy that only pays funding produces a constant income series; ``np.std`` of identical floats
    is not exactly zero once the mean has been summed and divided, so a naive Sharpe on it came out at
    3e17.  A ratio to a dispersion that is not there is not a measurement.
    """
    if values.size < minimum:
        return False
    spread = float(np.std(values, ddof=1))
    return spread > 1e-9 * max(abs(float(values.mean())), 1e-12)


def leg_split(store: StateStore, *, since_ms: int | None, equity: float | None) -> dict[str, Any]:
    """M-008: the long and short legs of the live book, split by the sign of the target that bar.

    A book that is 45% long by symbol-bar can still earn most of its return on the short side, which is
    what the round-7 decomposition found in the backtest.  Live, that split is the check on it.
    """
    targets_at: dict[int, dict[str, float]] = {}
    for row in _cycles(store):
        bar = row.get("bar_open_ms")
        if isinstance(bar, int | float):
            targets_at[int(bar)] = {str(k): float(v) for k, v in (row.get("targets") or {}).items()}
    legs: dict[str, float] = {"long": 0.0, "short": 0.0, "flat": 0.0}
    counts: dict[str, int] = {"long": 0, "short": 0, "flat": 0}
    for row in store.read_jsonl(store.attribution_path):
        bar = row.get("bar_open_ms")
        if not isinstance(bar, int | float) or (since_ms is not None and int(bar) < since_ms):
            continue
        targets = targets_at.get(int(bar), {})
        for symbol, bucket in (row.get("by_symbol") or {}).items():
            try:
                total = float(bucket.get("total", 0.0))
            except (TypeError, ValueError):
                continue
            weight = targets.get(str(symbol), 0.0)
            side = "long" if weight > 0 else ("short" if weight < 0 else "flat")
            legs[side] += total
            counts[side] += 1
    out: dict[str, Any] = {"pnl": legs, "symbol_bars": counts}
    if equity and equity > 0:
        out["pnl_pct"] = {side: value / equity for side, value in legs.items()}
    return out


def probe_correlation(store: StateStore, probes: Sequence[ProbeParams], *, since_ms: int | None) -> dict[str, Any]:
    """M-014: is a probe sleeve a second return stream, or a tilt on the main book?

    The round-5 finding was that 91% of the short-only flow sleeve's positions stacked on tsmom's shorts.
    If the live income series correlate closely, the sleeve is a tilt and its separate risk budget is a
    fiction, whatever its own P&L says.
    """
    series = _series_by_strategy(store, since_ms)
    out: dict[str, Any] = {}
    for probe in probes:
        sleeve = dict(series.get(probe.strategy) or [])
        others = {name: dict(points) for name, points in series.items() if name != probe.strategy}
        for name, main in others.items():
            shared = sorted(set(sleeve) & set(main))
            if len(shared) < 8:
                out[f"{probe.strategy}~{name}"] = {"bars": len(shared), "correlation": None}
                continue
            a = np.asarray([sleeve[bar] for bar in shared], dtype=float)
            b = np.asarray([main[bar] for bar in shared], dtype=float)
            value = None if a.std() == 0 or b.std() == 0 else float(np.corrcoef(a, b)[0, 1])
            out[f"{probe.strategy}~{name}"] = {"bars": len(shared), "correlation": value}
    return out


def margin_and_rejections(store: StateStore, *, since_ms: int | None) -> dict[str, Any]:
    """M-007: initial-margin usage and the count of venue rejections, by code.

    The plan set two numbers - usage at or below 50% of equity, and zero -2019 (insufficient margin)
    rejections - and neither was ever computed.  The per-cycle ``margin`` block existed but no series was
    taken from it, and order rejections collapsed into a single REJECTED status, so a -2019 could not be
    told apart from a lot-size error.

    Two different quantities are reported, because reading one for the other is what made this metric
    say 0.00% while the book was in fact carrying margin.  ``order_demand`` is what a cycle's *new*
    orders asked for, and only exists on cycles that placed one; ``standing`` is the initial margin the
    *held* positions consume, recorded every cycle from positionRisk (D-027: the venue's own
    ``totalInitialMargin`` is the field that overflowed).  The plan's 50% is about the standing book.
    """
    peak = 0.0
    peak_bar: int | None = None
    standing: list[float] = []
    peak_standing = 0.0
    for row in _cycles(store):
        bar_ms = int(row.get("bar_open_ms") or 0)
        if since_ms is not None and bar_ms < since_ms:
            continue
        equity = row.get("equity")
        held = row.get("margin_usage")
        if isinstance(held, int | float):
            standing.append(float(held))
            peak_standing = max(peak_standing, float(held))
        margin = row.get("margin") or {}
        try:
            needed = float(margin.get("needed_margin") or 0.0)
            usage = needed / float(equity) if equity else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if usage > peak:
            peak, peak_bar = usage, bar_ms
    rejections: dict[str, int] = {}
    for row in store.read_jsonl(store.trades_path):
        bar = row.get("bar_open_ms")
        if since_ms is not None and isinstance(bar, int | float) and int(bar) < since_ms:
            continue
        error = str(row.get("error") or "")
        if not error or str(row.get("status")) == "FILLED":
            continue
        code = error.split(":", 1)[0].strip() or "unknown"
        rejections[code] = rejections.get(code, 0) + 1
    return {
        "peak_margin_usage": peak,  # what a cycle's new orders asked for (0 on cycles that placed none)
        "peak_at_bar_ms": peak_bar,
        "peak_standing_usage": peak_standing if standing else None,
        "last_standing_usage": standing[-1] if standing else None,
        "standing_cycles": len(standing),
        "budget": 0.50,
        "over_budget": peak_standing > 0.50 if standing else peak > 0.50,
        "rejections": rejections,
        "insufficient_margin": sum(count for code, count in rejections.items() if "-2019" in code),
    }


def exit_and_pool_events(store: StateStore, day: str) -> dict[str, Any]:
    """M-005 and M-006: the two things that change the book without a signal changing its mind."""
    # a dry run submits no order (engine.py: DRY_RUN), so its exits and its pool moves never reached the
    # venue; left in, they pad the same M-005 exit list `exit_counterfactuals` excludes them from below
    rows = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day and not row.get("dry_run")]
    exits = [
        {"symbol": event.get("symbol"), "rule": event.get("rule"), "bar": row.get("bar")}
        for row in rows
        for event in (row.get("exit_events") or [])
    ]
    entered: list[str] = []
    left: list[str] = []
    quarantined: list[str] = []
    for row in rows:
        update = row.get("universe_update") or {}
        entered.extend(str(s) for s in (update.get("entered") or []))
        left.extend(str(s) for s in (update.get("left") or []))
        quarantined.extend(str(s) for s in (row.get("quarantined") or []))
    return {
        "exits": exits,
        "exit_count": len(exits),
        "by_rule": {rule: sum(1 for e in exits if e["rule"] == rule) for rule in {str(e["rule"]) for e in exits}},
        "pool_entered": entered,
        "pool_left": left,
        "pool_quarantined": quarantined,  # D-031: the falsifier is counted here, not asserted in a docstring
        "pool_changes": len(entered) + len(left),
    }


BACKTEST_EXITS_PER_WEEK = 562.0 / 49_735.0 * 24.0 * 7.0  # P11: 544 take-profits + 18 stops over 49,735 hourly bars
COUNTERFACTUAL_N_FOR_DECISION = 16  # a half-sigma per-event effect at t = 2; about two months at the rate above
TURNOVER_BPS = 7.0


def noise_scale(store: StateStore, day: str, *, vol_target: float | None) -> dict[str, Any]:
    """DL-EX0: the size of a normal day, so a giveback can be read as a multiple of it instead of as a feeling.

    ``design`` is vol_target / sqrt(365) of the day's last equity; ``realised`` is the std of hourly equity
    changes over the trailing 30 days of traded cycles (bars re-baselined by an external transfer are
    skipped), scaled by sqrt(24); ``peak_giveback`` is the largest drop from the running equity high inside
    the day.  The expected exit count pro-rates the P11 backtest rate for the whole book to the cycles seen
    so far in the current construction, and ``exits_so_far`` counts the exits that actually happened over
    that same evidence window - not over ``realised``'s 30 days, which is a different span, and not counting
    COOLDOWN, which records a cycle an earlier exit blocked rather than an exit of its own.
    """
    trailing = _cycles(store, window_days=30)
    today = [row for row in trailing if _day_of(row) == day]
    equities = [float(row["equity"]) for row in today]
    last = equities[-1] if equities else None
    design = (float(vol_target) / math.sqrt(365.0) * last) if (vol_target and last) else None
    steps = [
        float(b["equity"]) - float(a["equity"])
        for a, b in pairwise(trailing)
        if not (b.get("external_flows") or {}).get("rebaselined")
    ]
    realised = float(np.std(steps, ddof=1) * math.sqrt(24.0)) if len(steps) >= 24 else None
    peak = giveback = None
    for value in equities:
        peak = value if peak is None else max(peak, value)
        giveback = (peak - value) if giveback is None else max(giveback, peak - value)
    window = evidence_window(store)
    bars = int(window.get("bars") or 0)
    since = int(window.get("since_ms") or 0)
    # Both exit counts have to be on one window or their ratio means nothing: the expectation is scaled by
    # `bars`, which is every cycle in the evidence window, so the actual count reads that window too and not
    # `trailing`'s 720-bar tail (they agree only until the window outgrows 30 days).  COOLDOWN is excluded
    # because it is not an exit: `exit_step` returns it once per *blocked* cycle, so one take-profit trails
    # `cooldown_bars` more events behind it, while the backtest rate above counts take-profits and stops.
    exits = sum(
        sum(1 for event in (row.get("exit_events") or []) if event.get("rule") != COOLDOWN)
        for row in _cycles(store)
        if int(row.get("bar_open_ms") or 0) >= since
    )
    return {
        "design_daily_sigma_u": design,
        "realised_daily_sigma_u": realised,
        "peak_giveback_u": giveback,
        "giveback_in_design_sigma": (giveback / design) if (giveback is not None and design) else None,
        "expected_exits_so_far": BACKTEST_EXITS_PER_WEEK / 7.0 * (bars / 24.0),
        "exits_so_far": exits,
    }


def _store_closes(root: str | Path, interval: str) -> Callable[[str], pd.Series]:
    def loader(symbol: str) -> pd.Series:
        frame = KlineStore(root).load(symbol, interval)
        return pd.Series(frame["close"].astype(float).to_numpy(), index=frame["open_time"].astype(int).to_numpy())

    return loader


def exit_counterfactuals(
    store: StateStore,
    *,
    closes: Callable[[str], pd.Series] | None = None,
    root: str | Path = ".beidou/data",
    interval: str = "1h",
    horizons: Sequence[int] = (24, 72),
) -> dict[str, Any]:
    """M-005 as monitoring (K-EX12): for every exit the loop ever made, what the position would have earned had it stayed.

    Counterfactual P&L at horizon h = target x equity x (close_{t+h} / close_t - 1), with close_t the price the
    event recorded and close_{t+h} the mainnet close h bars later; the cost the exit paid is 2 x 7 bps x
    |target| x equity (out and back in).  Events younger than the longest horizon are ``pending``.  About 16
    priced events are needed before the mean says anything (a half-sigma effect at t = 2), which at the
    backtest's 1.9 exits per week is roughly two months - the reason this monitors and D-017 keeps the verdict.
    """
    loader = closes or _store_closes(root, interval)
    span_ms = int(interval_seconds(interval) * 1000)
    rows: list[dict[str, Any]] = []
    pending = 0
    cost_saved = 0.0
    cache: dict[str, pd.Series] = {}
    for record in store.read_jsonl(store.cycles_path):
        events = record.get("exit_events") or []
        # a dry run never submits an order (engine.py: DRY_RUN) and so never pays the exit's fee; kept in,
        # it would fabricate a cost and a counterfactual into this permanent, append-only, full-history scan
        if not events or record.get("equity") is None or record.get("dry_run"):
            continue
        equity = float(record["equity"])
        # the horizon is walked from the bar the *price* came from.  `event["price"]` is the close of the bar
        # at `as_of_ms` (engine.py, via `ExitOverlay.apply`), while `bar_open_ms` is the host clock's, and
        # D-025 has the host a whole bar from the venue's - on 2026-09-04 a full hour behind.  Anchoring on
        # the host would price a 23-bar hold as a 24-bar one; `_day_of` prefers `as_of_ms` for this reason.
        anchor = int(record.get("as_of_ms") or record.get("bar_open_ms") or 0)
        for event in events:
            if event.get("rule") == COOLDOWN or not event.get("price"):
                continue
            symbol = str(event.get("symbol"))
            notional = float(event.get("target") or 0.0) * equity
            cost_saved += 2.0 * TURNOVER_BPS / 10_000.0 * abs(notional)
            if symbol not in cache:
                try:
                    cache[symbol] = loader(symbol)
                # `data_coverage` below catches broad for the same reason: a store that cannot be read is a
                # research problem, never a reporting failure.  A zero-byte or half-written parquet raises
                # `ArrowInvalid`, not `FileNotFoundError`, and one of those must not stop `report daily` -
                # this monitor runs unattended, so the report is how a broken archive gets noticed at all.
                except Exception:
                    cache[symbol] = pd.Series(dtype=float)
            series = cache[symbol]
            row = {"symbol": symbol, "rule": event.get("rule"), "as_of_ms": anchor, "price": float(event["price"])}
            complete = True
            for horizon in horizons:
                future = anchor + horizon * span_ms
                if future in series.index:
                    row[str(horizon)] = notional * (float(series.loc[future]) / float(event["price"]) - 1.0)
                else:
                    row[str(horizon)] = None
                    complete = False
            pending += 0 if complete else 1
            rows.append(row)
    by_horizon: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        values = [float(row[str(horizon)]) for row in rows if row.get(str(horizon)) is not None]
        by_horizon[str(horizon)] = {
            "n": len(values),
            "mean_counterfactual_u": float(np.mean(values)) if values else None,
            "share_where_staying_paid": float(np.mean([v > 0 for v in values])) if values else None,
        }
    return {
        "events": len(rows),
        "pending": pending,
        "cost_saved_u": cost_saved,
        "by_horizon": by_horizon,
        "n_needed_for_decision": COUNTERFACTUAL_N_FOR_DECISION,
        "rows": rows[-20:],
    }


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


def data_coverage(store: StateStore, root: str | Path = ".beidou/data", interval: str = "1h") -> dict[str, Any]:
    """Live symbols whose research klines are missing, so a backtest would silently drop them.

    ``load_panel`` excludes a symbol with no stored klines and logs a warning nobody reads; CYSUSDT was
    traded live for sixteen hours while every research run quietly ran without it.
    """
    state = store.load()
    symbols = list(dict.fromkeys([*state.universe, *state.leaving]))
    try:
        stored = set(KlineStore(str(root)).symbols(interval))
    except Exception:  # a missing store is a research problem, never a reporting failure
        return {"live_symbols": len(symbols), "missing_klines": None}
    missing = [symbol for symbol in symbols if symbol not in stored]
    return {"live_symbols": len(symbols), "missing_klines": missing, "missing_count": len(missing)}


def drift_check(
    store: StateStore, expectations: dict[str, Any], *, window_days: int = 30, bars_per_year: float = 8760.0
) -> dict[str, Any]:
    """Realised trailing Sharpe/drawdown from cycle equity versus the validation expectation."""
    cycles = [
        row for row in store.read_jsonl(store.cycles_path) if row.get("equity") is not None and not row.get("dry_run")
    ][-(window_days * 24) :]
    if len(cycles) < 48:
        return {"status": "INSUFFICIENT_DATA", "bars": len(cycles)}
    # a bar that absorbed an external cash flow (deposit, demo reset; E-044) is not a return and is skipped
    returns = np.asarray(
        [
            float(current["equity"]) / float(previous["equity"]) - 1.0
            for previous, current in pairwise(cycles)
            if float(previous["equity"]) > 0
            and not (current.get("external_flows") or {}).get("rebaselined")
            # M-012: a bar whose host clock jumped re-maps every label, so neither the return into it
            # nor the one out of it is a return between two comparable timestamps
            and not (current.get("clock") or {}).get("jumped")
            and not (previous.get("clock") or {}).get("jumped")
        ]
    )
    if len(returns) < 47:
        return {"status": "INSUFFICIENT_DATA", "bars": len(returns) + 1}
    realised = sharpe(returns, bars_per_year)
    drawdown = max_drawdown(returns)
    expected = [v["oos_sharpe"] for v in expectations.values() if v.get("oos_sharpe") is not None]
    expected_sharpe = sum(expected) / len(expected) if expected else None
    mdds = [
        v["full_sample_max_drawdown"] for v in expectations.values() if v.get("full_sample_max_drawdown") is not None
    ]
    worst_expected_mdd = min(mdds) if mdds else None
    days = len(returns) / 24.0
    status = "OK"
    reasons: list[str] = []
    if expected_sharpe is not None and realised is not None:
        # standard error of an annualised Sharpe estimate over `days` days is roughly sqrt(365/days)
        z = (realised - expected_sharpe) / math.sqrt(365.0 / max(days, 1.0))
        if z < -2.0:
            status = "ALERT"
            reasons.append(f"实现 Sharpe {realised:.2f} 比预期的 {expected_sharpe:.2f} 低 {abs(z):.1f} 个标准误")
    if worst_expected_mdd is not None and drawdown < 1.5 * worst_expected_mdd:
        status = "ALERT"
        reasons.append(f"滚动回撤 {drawdown:.3f} 已超过验证期回撤 {worst_expected_mdd:.3f} 的 1.5 倍")
    return {
        "status": status,
        "window_days": round(days, 1),
        "realised_sharpe": realised,
        "expected_sharpe": expected_sharpe,
        "trailing_drawdown": drawdown,
        "validated_drawdown": worst_expected_mdd,
        "reasons": reasons,
    }


def _risk_budget_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """One readable line per metric; a metric that could not be computed says why instead of showing 0."""
    if not block:
        return {"none": 0}
    drawdown = block.get("drawdown") or {}
    volatility = block.get("realised_vol") or {}
    slippage = block.get("slippage") or {}
    guards = block.get("guards") or {}

    def number(metric: Mapping[str, Any], fmt: str) -> str:
        if metric.get("value") is None:
            return f"not enforced ({metric.get('why', 'no reason recorded')})"
        return fmt.format(metric["value"])

    return {
        "status": block.get("status"),
        "drawdown": f"{drawdown.get('value', 0.0):.2%} of the {drawdown.get('rollback_at', 0.0):.0%} budget"
        + (f" -> {drawdown['action']}" if drawdown.get("action") else ""),
        "realised vol": number(volatility, "{:.1%}") + f" band {volatility.get('band')}",
        "slippage": number(slippage, "{:.2f} bps") + f" limit {slippage.get('limit')} bps",
        "guards": f"pause {guards.get('daily_loss_pause_bars')} / capped {guards.get('gross_capped_bars')} bars"
        f" in {guards.get('window_days')}d",
        "reasons": block.get("reasons") or [],
    }


def _collateral_drift_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """RISK-G11 in the markdown, which is where a human reads it (it only reached the JSON before).

    `account_misleads` is spelled out rather than left implicit in a percentage above 100: the reader of
    the equity line four blocks up needs to be told, in words, that its sign does not tell them which
    way the book went.  Reported, never subtracted - the operator ruled the denominator on 2026-09-08.
    """
    if not block:
        return {"none": 0}
    if not block.get("enforced"):
        return {"not measured": str(block.get("reason", "no reason recorded"))}
    share = block.get("repricing_share")
    return {
        "cycles": block.get("cycles"),
        "equity_change": _fmt_num(block.get("equity_change")),
        "attributed_pnl": _fmt_num(block.get("attributed_pnl")),
        "collateral_repricing": _fmt_num(block.get("collateral_repricing")),
        "repricing_share": "flat window (no move to apportion)" if share is None else f"{share:.1%}",
        "collateral_share_of_equity": _fmt_pct(block.get("collateral_share")),
        "direction": str(block.get("direction")),
        "account_misleads": (
            "YES - 权益方向与账面归因 P&L 方向相反，本窗口的权益线不能用来判断书的盈亏"
            if block.get("account_misleads")
            else "no - 权益方向仍然与书的盈亏同向"
        ),
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


def _long_run_sharpe_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """M-G06.  The countdown is the reading until 2028-03-04, so the countdown is what is rendered."""
    if not block:
        return {"none": 0}
    remaining = block.get("days_remaining")
    lines: dict[str, Any] = {
        "status": block.get("status"),
        "construction": str(block.get("construction")),
        "unbroken_since": str(block.get("since"))[:16],
        "window": f"{float(block.get('elapsed_days') or 0.0):.2f}/{block.get('required_days')} 天"
        f"（{block.get('window_months')} 个月，按 canonical_construction 计时）",
        "judgeable_from": f"{str(block.get('judgeable_from'))[:10]}"
        + (f"（还差 {float(remaining):.1f} 个记录日，前提是循环不再缺）" if remaining is not None else ""),
        "calendar_vs_record": f"日历 {float(block.get('calendar_days') or 0.0):.2f} 天，"
        f"其中 {float(block.get('downtime_days') or 0.0):.2f} 天记录没有覆盖",
    }
    if block.get("why"):
        lines["why"] = str(block["why"])
    for strategy, row in (block.get("by_strategy") or {}).items():
        lines[str(strategy)] = (
            f"{row.get('status')} sharpe_so_far={_fmt_num(row.get('sharpe_so_far'))} "
            f"+-{_fmt_num(row.get('sharpe_standard_error'))} nw_t={_fmt_num(row.get('nw_t'))} (报告，不作门) "
            f"over {row.get('days')}d" + (f" -> {row['action']}" if row.get("action") else "")
        )
    return lines


def _probe_correlation_note(payload: Mapping[str, Any], strategy: str) -> str:
    """The M-014 pair involving this probe, rendered onto its review line (empty when unreadable)."""
    pairs = [
        (pair, row)
        for pair, row in (payload.get("probe_correlation") or {}).items()
        if strategy and pair.endswith(f"~{strategy}") and row.get("correlation") is not None
    ]
    if not pairs:
        return ""
    pair, row = max(pairs, key=lambda item: abs(float(item[1]["correlation"])))
    return f" corr({pair})={float(row['correlation']):+.2f} over {row.get('bars')} bars"


def _risk_adaptation_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """Same rule as `_risk_budget_lines`: a spread that could not be computed says why, not "n/a"."""
    leverages = block.get("leverage_distinct")
    venue = f"{leverages} distinct value(s) across the book" if leverages else "nothing set yet"
    if not block.get("enforced"):
        return {"status": f"not enforced ({block.get('reason', 'no reason recorded')})", "exchange leverage": venue}
    combined, alone, overlaid = block.get("combined") or {}, block.get("single_book") or {}, block.get("overlaid") or {}

    def reading(name: str, entry: Mapping[str, Any]) -> str:
        if entry.get("compression") is None:
            return f"{name}: {entry.get('why', 'no reading')}"
        return (
            f"{name}: compression {entry['compression']:.4f} "
            f"(vol {entry['vol_spread']:.1f}x -> risk {entry['risk_spread']:.4f}x over {entry['symbols']} names)"
        )

    return {
        "status": f"{block.get('status')} - stage 1 removes {1.0 - float(block['compression']):.0%} of the "
        f"market's dispersion (compression {block['compression']:.2f}, alert above {block['limit']:.2f}, "
        f"judged on the {block.get('judged', 'combined')} reading)",
        "market vol spread": f"{block['vol_spread']:.1f}x across {block['symbols']} held symbols",
        "risk contribution spread": f"{block['risk_spread']:.1f}x - this is what sizing equalises",
        # Both readings, always, whichever one the gate took: the combined book is what is actually
        # held and the single-book one is what stage 1 promises.  The gap between them IS the finding.
        "single book (judged)": reading("single", alone),
        "combined book (reported)": reading("combined", combined),
        "carried by two books": (
            f"{', '.join(overlaid.get('names') or [])} via {', '.join(overlaid.get('books') or [])}"
            + (f"; risk spread {overlaid['risk_spread']:.1f}x among them" if overlaid.get("risk_spread") else "")
        )
        or "none",
        "exchange leverage": f"{venue}; carries no risk here (D-037)",
        "by symbol": json_dumps(
            {
                str(row["symbol"]): f"lev={row['leverage']}x vol={row['annual_vol']:.0%} "
                f"w={row['weight']:+.4f} risk={row['risk']:.2%}"
                for row in block.get("rows") or []
            }
        ),
    }


def risk_adaptation(store: StateStore, day: str) -> dict[str, Any]:
    """M-015: how much of each symbol's market volatility the sizing layer takes back out.

    The operator has now asked three times why every symbol sits at the same exchange leverage,
    and the report was the reason: it showed `last_targets` and nothing to read them against, so
    the only per-symbol number visible anywhere was the venue's uniform 5x.  D-037 settled that
    leverage carries no risk here - maintenance margin is indexed by notional tier, not by the
    chosen leverage - and that adaptation happens in the weight instead, via stage 1's
    ``vol_target / asset_vol``.  That was true and invisible, which is the same as unproven.

    Two spreads, over the symbols the book actually holds:
      vol_spread   max sigma / min sigma   - how different these markets are
      risk_spread  max |w|sigma / min |w|sigma - how different their risk contributions are

    ``compression = risk_spread / vol_spread`` is the instrument.  It is not a display: delete
    stage 1 and weights stop depending on sigma, so risk_spread converges on vol_spread and this
    reads 1.0.  Measured on the live book 2026-09-05 it reads 0.13 (vol 12.2x -> risk 1.6x); on
    2026-09-08, with the probe overlaying four of eighteen names, 0.58.  ``RISK_COMPRESSION_LIMIT``
    carries the derivation and the reason it moved.

    Several honest effects push it up with stage 1 untouched, which is why the limit is loose: a
    second book summed by ``combine_books`` disagreeing with the first (the big one - it both stacks
    and cancels), the no-trade band holding a stale weight while sigma moves under it, ``max_weight``
    binding on the calmest names, and a signal with magnitude putting conviction back in the numerator.
    Read an ALERT as "the risk contributions spread apart", never as "stage 1 broke" - max/min over
    the held names cannot tell those apart.

    Refuses to answer rather than passing by default - a spread over one or two names is noise,
    and cycles written before ``asset_vol`` was recorded carry no sigma at all.
    """
    cycles = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    leverage = dict(store.load().leverage_set)
    # Carried on every path, refusals included: it is the number the operator came here to look at,
    # and "the venue is at one leverage for all 15 symbols" is a fact even on a day with no sigma.
    distinct_leverage = len(set(leverage.values()))
    refused = {"enforced": False, "rows": [], "leverage_distinct": distinct_leverage}
    if not cycles:
        return {**refused, "reason": f"no cycles on {day}"}
    last = cycles[-1]
    vols = last.get("asset_vol") or {}
    targets = last.get("targets") or {}
    if not vols:
        return {**refused, "reason": "this cycle predates the asset_vol record"}
    rows = [
        {
            "symbol": symbol,
            "leverage": leverage.get(symbol),
            "annual_vol": float(vols[symbol]),
            "weight": float(weight),
            "risk": abs(float(weight)) * float(vols[symbol]),
        }
        for symbol, weight in sorted(targets.items())
        if symbol in vols and float(vols[symbol]) > 0 and abs(float(weight)) > 0
    ]
    held = sorted(rows, key=lambda row: row["risk"])
    if len(held) < 3:
        reason = f"only {len(held)} symbols carry a weight; a spread over that is noise"
        return {**refused, "reason": reason, "rows": rows}

    def spread(entries: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(entries, key=lambda row: row["risk"])
        if len(ordered) < 3:
            return {"symbols": len(ordered), "compression": None, "why": "fewer than 3 names; a spread is noise"}
        sigmas = [row["annual_vol"] for row in ordered]
        vol = max(sigmas) / min(sigmas)
        risk = ordered[-1]["risk"] / ordered[0]["risk"]
        return {
            "symbols": len(ordered),
            "vol_spread": vol,
            "risk_spread": risk,
            "compression": risk / vol if vol > 0 else None,
        }

    # The books each name is carried by (2026-09-12).  The docstring below this one already said what
    # to do the first time a cancellation set this off - "measure the main book separately, NOT raise
    # this a second time" - and until `books` was written into the cycle there was nothing to measure
    # it with.  Today: combined 0.96 (ALERT), single-book 0.1195, and the single-book risk spread is
    # 1.000000 to six figures across fourteen names.  Stage 1 is not the thing that moved.
    carried = books_by_symbol(last)
    single = [row for row in held if len(carried.get(row["symbol"], frozenset())) == 1] if carried else []
    overlaid = [row for row in held if len(carried.get(row["symbol"], frozenset())) > 1] if carried else []
    combined = spread(held)
    alone = spread(single) if carried else {"symbols": 0, "compression": None, "why": "this cycle records no books"}
    # The gate follows the reading that answers stage 1's question.  Where the record cannot split the
    # books it falls back to the combined number rather than to silence: an unreadable split is not a
    # pass.  The combined reading is computed and printed either way (the L3 shape: moving a reading
    # out of the gate is not deleting it).
    judged = alone if alone.get("compression") is not None else combined
    compression = judged.get("compression")
    return {
        "enforced": True,
        "reason": None,
        "symbols": judged.get("symbols", len(held)),
        "vol_spread": judged.get("vol_spread"),
        "risk_spread": judged.get("risk_spread"),
        "compression": compression,
        "judged": "single_book" if judged is alone else "combined",
        "combined": combined,
        "single_book": alone,
        "overlaid": {
            **spread(overlaid),
            "names": [row["symbol"] for row in overlaid],
            "books": sorted({book for row in overlaid for book in carried.get(row["symbol"], frozenset())}),
        },
        "limit": RISK_COMPRESSION_LIMIT,
        "status": "ALERT" if compression is not None and compression > RISK_COMPRESSION_LIMIT else "OK",
        "leverage_distinct": distinct_leverage,
        "rows": rows,
    }


def _dataset_block(dataset: Mapping[str, Any] | None) -> dict[str, Any]:
    """D-041: what the cited evidence's dataset manifest says about the data on disk.

    Empty lists rather than ``None`` when nothing was passed, so a reader never has to distinguish
    "not checked" from "checked and clean" by the shape of the value - the same mistake the manifest
    itself made about funding.
    """
    block = dict(dataset or {})
    return {"blocking": list(block.get("blocking", [])), "advisory": list(block.get("advisory", []))}


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
    missed = restarts = unreadable = 0
    windows: list[float] = []
    wakes: list[float] = []  # how late each SCHEDULED cycle woke, from its own row
    restart_late: list[float] = []
    for row in rows:
        reason = row.get("reason")
        skipped = row.get("phase") == "SKIPPED" or reason == MISSED_REBALANCE_REASON
        if skipped:
            restarts += 1
            # A bar that was already rebalanced cannot have had its rebalance missed.  The row still
            # carries the window the engine allowed, so `widest_window_seconds` keeps it; only the miss
            # COUNT declines to charge it.
            if reason != ALREADY_REBALANCED_REASON:
                missed += 1
            if isinstance(window := row.get("window_seconds"), int | float):
                windows.append(float(window))
            if isinstance(value := row.get("late_seconds"), int | float):
                restart_late.append(float(value))
            continue
        # The bar is per row when the row carries it (every completed cycle does, from 2026-09-10) and
        # falls back to the widest the engine allowed that day, for rows written before it did.
        if isinstance(window := row.get("window_seconds"), int | float):
            windows.append(float(window))
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
    reasons: list[str] = []
    if missed > params.max_missed_rebalances:
        worst_restart = max(restart_late) if restart_late else 0.0
        reasons.append(
            f"漏掉 {missed} 次再平衡（M-Q03 阈值 {params.max_missed_rebalances}）；"
            f"最迟的一次在 bar 收盘后 {worst_restart:.0f} 秒；失败动作：查重启原因"
        )
    if share is not None and share > params.max_late_cycle_share:
        reasons.append(
            f"迟到周期占比 {share:.1%} 高于 M-Q03 的 {params.max_late_cycle_share:.0%}"
            f"（{len(late)}/{len(measurable)} 个已排定周期在再平衡窗口外醒来；重启不计）"
        )
    return {
        "cycles": len(rows),
        "missed_rebalances": missed,
        "skipped_bars": missed,
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


def daily_payload(
    store: StateStore,
    day: str,
    expectations: dict[str, Any] | None = None,
    probes: Sequence[ProbeParams] = (),
    risk_budget: RiskBudgetParams | None = None,
    dataset: Mapping[str, Any] | None = None,
    *,
    vol_target: float | None = None,
    data_root: str | Path = ".beidou/data",
    closes: Callable[[str], pd.Series] | None = None,
) -> dict[str, Any]:
    cycles = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    trades = [row for row in store.read_jsonl(store.trades_path) if _day_of(row) == day]
    attributions = [row for row in store.read_jsonl(store.attribution_path) if _day_of(row) == day]
    equities = [float(row["equity"]) for row in cycles if row.get("equity") is not None]
    by_strategy: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    commissions = funding = realized = 0.0
    foreign_total = 0.0
    foreign_rows = 0
    foreign_by_symbol: dict[str, float] = {}
    unreconciled = 0
    for row in attributions:
        for strategy, value in (row.get("by_strategy") or {}).items():
            by_strategy[strategy] = by_strategy.get(strategy, 0.0) + float(value)
        for symbol, bucket in (row.get("by_symbol") or {}).items():
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + float(bucket.get("total", 0.0))
            commissions += float(bucket.get("COMMISSION", 0.0))
            funding += float(bucket.get("FUNDING_FEE", 0.0))
            realized += float(bucket.get("REALIZED_PNL", 0.0))
        # D-032: P&L from fills the loop did not place is real money and stays in the report, but it is
        # not the strategy's and must not reach the series M-010 judges the strategy by.
        foreign = row.get("foreign") or {}
        foreign_total += float(foreign.get("total", 0.0) or 0.0)
        foreign_rows += int(foreign.get("rows", 0) or 0)
        for symbol, bucket in (foreign.get("by_symbol") or {}).items():
            foreign_by_symbol[symbol] = foreign_by_symbol.get(symbol, 0.0) + float(bucket.get("total", 0.0))
        if "foreign" in row and not foreign.get("reconciled"):
            unreconciled += 1
    statuses: dict[str, int] = {}
    traded = 0.0
    for row in trades:
        statuses[str(row.get("status"))] = statuses.get(str(row.get("status")), 0) + 1
        if row.get("executed_qty") and row.get("avg_price"):
            traded += float(row["executed_qty"]) * float(row["avg_price"])
    guard_events = [reason for row in cycles for reason in (row.get("guard_reasons") or [])]
    window = evidence_window(store)
    flows = [row.get("external_flows") or {} for row in cycles]
    return {
        "day": day,
        "cycles": len(cycles),
        "skipped_cycles": sum(1 for row in cycles if row.get("skip")),
        "external_flows": {
            "total": sum(float(flow.get("total", 0.0) or 0.0) for flow in flows),
            "rows": sum(int(flow.get("rows", 0) or 0) for flow in flows),
            "rebaselined_cycles": sum(1 for flow in flows if flow.get("rebaselined")),
        },
        "equity_start": equities[0] if equities else None,
        "equity_end": equities[-1] if equities else None,
        "equity_change_pct": (equities[-1] / equities[0] - 1.0) if len(equities) >= 2 and equities[0] else None,
        # L1-10: the last cycle's split of that equity into USDT and collateral.  Rows written before the
        # engine recorded it carry nothing, and nothing is what gets reported - not a zero.
        "collateral": next(
            (row["collateral"] for row in reversed(cycles) if isinstance(row.get("collateral"), dict)),
            None,
        ),
        "orders": statuses,
        "traded_notional": traded,
        "realized_pnl": realized,
        "commissions": commissions,
        "funding": funding,
        "pnl_by_strategy": by_strategy,
        "pnl_by_symbol": by_symbol,
        "foreign_fills": {
            "total": foreign_total,
            "rows": foreign_rows,
            "by_symbol": foreign_by_symbol,
            "unreconciled_cycles": unreconciled,
        },
        "guard_events": {event: guard_events.count(event) for event in set(guard_events)},
        # M-Q03 / AC-L4 / RISK-P2: what the day's restarts cost, against the plan's own "<= 5% / 0".
        # DL-L4 wrote these into the cycle rows and nothing read them; a cost that only exists in a
        # JSONL is an assumption, not a measurement, and one nothing compares to a bar is not a metric.
        "restarts": restart_cost(cycles, trades, risk_budget or RiskBudgetParams()),
        "last_targets": cycles[-1].get("targets") if cycles else {},
        "expectations": expectations or {},
        "risk_budget": risk_budget_status(
            _cycles(store),
            store.read_jsonl(store.trades_path),
            risk_budget or RiskBudgetParams(),
            store.read_jsonl(store.attribution_path),
        ),
        # DL-D4 / M-011: do the T+1 archive and what the loop could actually read agree on the buckets
        # they share?  The whole same-source contract is this one number, and until now `metrics_parity`
        # existed with nothing calling it - which is the shape this repository keeps finding, a
        # measurement that is written but never taken.
        "metrics_parity": metrics_parity_status(sorted(cycles[-1].get("universe") or []) if cycles else [], data_root),
        # The instrument the 2026-09-08 ruling owes: the denominator stays total equity, so the
        # pro-cyclical amplifier is an ACCEPTED risk - and an accepted risk with nothing measuring it is
        # a sentence.  Beside `risk_budget` rather than inside it on purpose: it is not a threshold and
        # it must never page.
        "collateral_drift": collateral_drift(_cycles(store), store.read_jsonl(store.attribution_path)),
        "drift": drift_check(store, expectations or {}),
        "evidence_window": window,
        "income_drift": income_drift(
            store, expectations or {}, equity=equities[-1] if equities else None, since_ms=window["since_ms"]
        ),
        # M-G06 (§19 Q2's lagging half).  INSUFFICIENT_DATA for the next year and a half, on purpose:
        # the row that says how far off it is is the only honest thing it can say today.
        "long_run_sharpe": long_run_sharpe(store, equity=equities[-1] if equities else None),
        "legs": leg_split(store, since_ms=window["since_ms"], equity=equities[-1] if equities else None),
        "probe_correlation": probe_correlation(store, probes, since_ms=window["since_ms"]),
        "events": exit_and_pool_events(store, day),
        "noise_scale": noise_scale(store, day, vol_target=vol_target),
        "exit_counterfactual": exit_counterfactuals(store, closes=closes, root=data_root),
        "plan_gaps": plan_gaps(store, day),
        "clock": clock_health(store, day),
        "data_coverage": data_coverage(store, root=data_root),
        "margin": margin_and_rejections(store, since_ms=window["since_ms"]),
        "risk_adaptation": risk_adaptation(store, day),
        "probes": probe_rows(store, probes, equity=equities[-1] if equities else None, now_ms=_day_end_ms(day)),
        "dataset": _dataset_block(dataset),
    }


def daily_alerts(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Split a daily payload's findings into (alerts, notices): what pages, and what only gets read.

    An alert says the running book has moved and the operator can do something about it now.  A
    notice is true and worth seeing at review, but nothing can be done with it in the next hour -
    a construction-cadence count is the case that forced the distinction: two promotions landed on
    2026-09-04, a promotion cannot be un-done, and the hourly check paged on it for three days,
    twice an hour, because `dedup_window_seconds` equals the job's period.  That is precisely the
    repeated alert `alerts.py` says buries the one that matters, so notices leave the paging path
    entirely; they stay in the report's Evidence window block.  Making a notice loud again is a
    matter of moving one append, not of finding a suppression to undo.
    """
    alerts: list[str] = []
    for name, key in (("权益", "drift"), ("策略收益", "income_drift")):
        block = payload.get(key) or {}
        if str(block.get("status")) == "ALERT":
            detail = block.get("reasons") or [
                f"{strategy}: z={row.get('z'):.1f}"
                for strategy, row in (block.get("by_strategy") or {}).items()
                if row.get("z") is not None and row["z"] < -2.0
            ]
            alerts.append(f"{name}漂移告警：{'；'.join(str(d) for d in detail)}")
    budget = payload.get("risk_budget") or {}
    if str(budget.get("status")) == "ALERT":
        # P13's ladder: the thresholds were fixed before the change went live, so this says what to do
        # rather than that something looks off.  It alerts; a human still runs the one-line change.
        alerts.append("风险预算告警：" + "；".join(str(r) for r in budget.get("reasons") or []))
    adaptation = payload.get("risk_adaptation") or {}
    if str(adaptation.get("status")) == "ALERT":
        # M-015: the weights stopped taking each symbol's volatility back out.  Loud rather than
        # quiet because this is the layer D-037 pointed at when it ruled the leverage layer inert.
        # The clause read "per-symbol sizing is no longer vol-scaled", and the Lark translation
        # carried it over faithfully as "不再按波动率缩放" - a conclusion the statistic cannot
        # support, and wrong the first time it fired: `compression` is measured AFTER
        # `combine_books` sums the books, so a second book disagreeing reads as stage 1 failing.
        # Since 2026-09-12 the number quoted is the reading the gate took, and the line says which one
        # and what the other reads.  The old text told the operator to "check the probe/main overlap"
        # and gave them nothing to check it with; now the overlap is measured and sits beside it.
        combined = (adaptation.get("combined") or {}).get("compression")
        alerts.append(
            f"风险自适应告警：压缩度 {adaptation.get('compression'):.2f} > {adaptation.get('limit'):.2f}"
            f"（{adaptation.get('judged', 'combined')} 读法）"
            + (f"；合并两本书读 {combined:.2f}" if combined is not None else "")
            + "；风险贡献相互拉开——查第一层"
        )
    notices: list[str] = []
    if str(budget.get("status")) == "BLIND":
        # A criterion with no reading is not a breach and cannot be acted on in the next hour - it
        # clears itself once the bars or fills arrive.  It is here rather than nowhere because the
        # 2026-09-07 report said OK while M-Q08's slippage instrument had zero usable fills.
        notices.append(
            "风险预算读不出数（BLIND）："
            + "；".join(f"{entry.get('metric')}（{entry.get('why')}）" for entry in budget.get("unreadable") or [])
        )
    window = payload.get("evidence_window") or {}
    if int(window.get("changes_7d") or 0) > 1:
        # the plan allowed one promotion per week and nothing ever counted them
        notices.append(f"最近 7 天有 {window['changes_7d']} 次构造变更；计划允许每周一次晋升")
    drift = payload.get("collateral_drift") or {}
    if drift.get("account_misleads"):
        # RISK-G11.  NOT an alert, and the reasoning is the same distinction this docstring draws.  The
        # amplifier itself is a standing fact the operator ACCEPTED on 2026-09-08 with the denominator
        # ruling, so there is nothing to do about it inside the hour and it must never page (its own
        # module says so); on the paging path, with `dedup_window_seconds` equal to the hourly job's
        # period, it would re-announce itself twice an hour for as long as the account holds BTC - the
        # construction-cadence shape exactly.  What DID change is which side of 1.0 the share sits on,
        # and that is a fact a reader of the equity line needs at review: above 1 the account's equity
        # direction no longer tells them which way the book went.
        notices.append(
            f"抵押品重估占权益变化的 {drift['repricing_share']:.1%}（>100%）："
            f"权益 {drift['equity_change']:+.2f} 而书的归因 P&L 是 {drift['attributed_pnl']:+.2f}，"
            "本窗口权益方向与书的盈亏方向相反（RISK-G11，只报告不相减）"
        )
    restarts = payload.get("restarts") or {}
    if str(restarts.get("status")) == "ALERT":
        # M-Q03.  A notice for the reason the plan itself gives: its registered failure action is
        # "查重启原因", an investigation at review.  The miss is already past by the time this renders,
        # `live status --check` already pages when the loop is actually down, and a single planned
        # deployment restart would otherwise hold the hourly check red until UTC midnight.  Making it
        # loud is moving this one append into the list above.
        notices.append("M-Q03 迟到成交：" + "；".join(str(r) for r in restarts.get("reasons") or []))
    lagging = payload.get("long_run_sharpe") or {}
    if str(lagging.get("status")) == "FAIL":
        # M-G06.  INSUFFICIENT_DATA says nothing here on purpose - it will be the answer until
        # 2028-03-04 and a finding repeated for 542 days is not a finding.  A FAIL is real, and its
        # action ("该策略退出 main") is a governance transition taken through `lifecycle.apply` at
        # review rather than something to do inside the hour.
        notices.append(
            "M-G06 滞后判据不通过："
            + "；".join(
                f"{strategy} 归因年化 Sharpe {row.get('sharpe_so_far'):.2f} < 0 -> {row.get('action')}"
                for strategy, row in (lagging.get("by_strategy") or {}).items()
                if str(row.get("status")) == "FAIL"
            )
        )
    return alerts, notices


ALPHA_EFFORT_TARGET = 0.90  # operator decision 2026-09-04; see docs/RESEARCH_LOG.md


def effort_share(changed_lines: Mapping[str, int]) -> dict[str, Any]:
    """How much of a period's authored work went into alpha, against the 90% target.

    The target cannot be read off the source tree: reaching 90% of *lines* would mean 68,706 lines of
    signal code against today's 3,661, and bloated signal code is exactly what the V5 rebuild deleted.
    The accumulated 22% is sunk - an exchange client, a live loop, a data pipeline and a CLI have a floor
    that does not shrink because the goal changed.  What the goal can govern is the *next* line written,
    so this measures the share of newly authored lines, and generated evidence under ``reports/`` is
    excluded because writing a report is not effort.

    Tests are counted with the thing they test, since a test for the exit overlay is live-loop work and a
    test for a signal is alpha work.
    """
    buckets: dict[str, int] = {"alpha": 0, "research": 0, "infrastructure": 0}
    for path, lines in changed_lines.items():
        if path.startswith("reports/"):
            continue
        if path.startswith(("beidou_alpha/", "tests/alpha/")):
            buckets["alpha"] += lines
        elif path.startswith(("docs/RESEARCH_LOG", "docs/analysis/")):
            buckets["research"] += lines
        else:
            buckets["infrastructure"] += lines
    total = sum(buckets.values())
    share = (buckets["alpha"] + buckets["research"]) / total if total else None
    return {
        "lines": buckets,
        "total": total,
        "alpha_share": share,
        "target": ALPHA_EFFORT_TARGET,
        "on_target": None if share is None else share >= ALPHA_EFFORT_TARGET,
    }


def _parsed(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# DL-K3 landed on this date, and C-P6's rule applies to it exactly as it does to DL-K1: a mechanism
# that lands today judges tomorrow's runs.  Run against the archive the first time, this check produced
# nine FAILs and not one was a finding - seven mined validations with no `mined` ledger rows because
# DL-K2 landed the same day, and two reports whose log commit lands an hour later, which is a batch
# commit at the end of a session.  A commit timestamp says when the text was SAVED, not when it was
# written, and this check cannot tell those apart; nine lines of noise train an operator to skip the
# section, which is worse than not having one.
PREREGISTRATION_EFFECTIVE_FROM = "2026-09-07T00:00:00+00:00"


def preregistration_skipped(reports: Sequence[Mapping[str, Any]], *, effective_from: str) -> int:
    """How many reports this check declines to judge.  Reported, never silent."""
    boundary = _parsed(effective_from)
    if boundary is None:
        return 0
    return sum(1 for report in reports if (_parsed(report.get("generated_at")) or boundary) < boundary)


def preregistration_problems(
    reports: Sequence[Mapping[str, Any]],
    *,
    first_mentioned: Mapping[str, str | None],
    search_charged: Mapping[str, str | None],
    effective_from: str = "",
) -> list[str]:
    """DL-K3 / KILL-R9: report the validations whose hypothesis was not registered before the result.

    Two orderings, because there are two kinds of strategy.  A hand-written one is registered by name,
    so the earliest commit to ``docs/RESEARCH_LOG.md`` mentioning it has to predate the report.  A
    mined one cannot be: its id is DERIVED from the search, so the hash provably cannot appear in a
    commit written before the run that produced it.  What must precede a mined candidate is the search
    itself - the mine that enumerated it and charged it to the ledger (DL-K2) - and checking its hash
    against the log would only confirm that somebody wrote the verdict down afterwards.

    Silence is never a pass.  A strategy the log never mentions, a candidate no search ever charged and
    a timestamp that will not parse are all reported: a check that skips what it cannot read is a check
    that reports success it did not perform, which is the failure mode P19 found five times in a row.
    """
    boundary = _parsed(effective_from)
    problems: list[str] = []
    for report in reports:
        strategy = str(report.get("strategy", ""))
        path = str(report.get("path", strategy))
        produced = _parsed(report.get("generated_at"))
        if boundary is not None and produced is not None and produced < boundary:
            continue  # counted by `preregistration_skipped`, not judged here
        if produced is None:
            problems.append(f"{path}: its own timestamp could not be read, so nothing about its order is known")
            continue
        if strategy.startswith("mined_"):
            candidate = strategy.removeprefix("mined_")
            charged = _parsed(search_charged.get(candidate))
            if charged is None:
                problems.append(
                    f"{path}: no search ever charged candidate {candidate} to the ledger, so this "
                    "validation has no enumerated space behind it (DL-K2)"
                )
            elif charged > produced:
                problems.append(
                    f"{path}: validated at {produced.isoformat()} but the search that found {candidate} "
                    f"was only charged at {charged.isoformat()}"
                )
            continue
        mentioned = _parsed(first_mentioned.get(strategy))
        if mentioned is None:
            problems.append(
                f"{path}: {strategy} is never mentioned in docs/RESEARCH_LOG.md's history, so no "
                "pre-registration precedes this report"
            )
        elif mentioned > produced:
            problems.append(
                f"{path}: {strategy} was reported at {produced.isoformat()} but first appears in the "
                f"log at {mentioned.isoformat()} - the pre-registration was written after the result"
            )
    return problems


def weekly_payload(
    store: StateStore,
    day: str,
    *,
    expectations: dict[str, Any] | None = None,
    changed_lines: Mapping[str, int] | None = None,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The plan's weekly research report, which was listed as a deliverable and never built.

    Its job is not to add numbers but to put the week's decisions next to the week's evidence: how many
    days the current construction has actually run, what each strategy earned by attributed income, how
    many configurations were charged to the ledger, and whether the promotion cadence was respected.  A
    week is the unit because that is the cadence the plan set for promotions.
    """
    end = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    since_ms = int((end - timedelta(days=7)).timestamp() * 1000)
    cycles = [row for row in _cycles(store) if int(row.get("bar_open_ms") or 0) >= since_ms]
    window = evidence_window(store)
    equities = [float(row["equity"]) for row in cycles if row.get("equity") is not None]
    income = income_drift(store, expectations or {}, equity=equities[-1] if equities else None, since_ms=since_ms)
    decay = decay_watch(store, expectations or {}, equity=equities[-1] if equities else None)
    constructions = sorted({str(row.get("construction")) for row in cycles if row.get("construction")})
    return {
        "week_ending": day,
        "since_ms": since_ms,
        "cycles": len(cycles),
        "skipped_cycles": sum(1 for row in cycles if row.get("skip")),
        "equity_start": equities[0] if equities else None,
        "equity_end": equities[-1] if equities else None,
        "constructions_seen": constructions,
        "promotions": max(0, len(constructions) - 1),
        "promotion_budget": 1,
        "evidence_window": window,
        "income": income,
        # The adopted decay rule (report 4.2 Ⅰ, ruling 12.9).  Weekly rather than daily on purpose: it
        # asks about the live period as a whole, and every other number in the daily report is about
        # one day.  It reads INSUFFICIENT_DATA until somebody computes the q10 for this construction.
        "decay": decay,
        "legs": leg_split(store, since_ms=since_ms, equity=equities[-1] if equities else None),
        "margin": margin_and_rejections(store, since_ms=since_ms),
        "effort": effort_share(changed_lines) if changed_lines is not None else None,
        "dataset": _dataset_block(dataset),
    }


def weekly_markdown(payload: dict[str, Any]) -> str:
    income_rows = (payload.get("income") or {}).get("by_strategy") or {}
    return render_markdown(
        f"Weekly report, week ending {payload['week_ending']}",
        [
            (
                "Cycles",
                {key: payload.get(key) for key in ("cycles", "skipped_cycles", "equity_start", "equity_end")},
            ),
            (
                # The adopted decay rule.  It cannot fire before roughly 2026-11-05 - the construction
                # froze 2026-09-06 and two whole non-overlapping 30-day windows have to follow it - and
                # it needs a q10 nobody has computed yet.  Both facts are printed rather than hidden,
                # because a rule that is silently unable to fire is the same as no rule.
                "Edge decay (M-010 vs backtest q10)",
                {
                    name: f"{row['status']}"
                    + (f" - {row['why']}" if row.get("why") else f" ({row['below']}/2 below q10)")
                    for name, row in sorted((payload.get("decay") or {}).items())
                }
                or {"none": "no attributed income yet"},
            ),
            (
                # D-041: the manifest was written into every report and read by nothing.  It is read now,
                # and this is where a human sees the answer after startup has scrolled away.
                "Dataset provenance (D-041)",
                {
                    key: json_dumps(value) if value else "none"
                    for key, value in (payload.get("dataset") or {"blocking": [], "advisory": []}).items()
                },
            ),
            (
                "Promotions this week (the plan allows one)",
                {
                    "constructions_seen": len(payload.get("constructions_seen") or []),
                    "promotions": payload.get("promotions"),
                    "within_budget": int(payload.get("promotions") or 0) <= int(payload.get("promotion_budget") or 1),
                    "current_construction": (payload.get("evidence_window") or {}).get("construction"),
                    "bars_under_it": (payload.get("evidence_window") or {}).get("bars"),
                },
            ),
            (
                "Income by strategy (M-002/M-010)",
                {
                    strategy: (
                        f"pnl={_fmt_num(row.get('pnl'))} sharpe={_fmt_num(row.get('realised_sharpe'))} "
                        f"vs {_fmt_num(row.get('expected_sharpe'))} over {_fmt_num(row.get('days'))}d"
                    )
                    for strategy, row in income_rows.items()
                }
                or {"none": 0},
            ),
            (
                # DL-K3.  A pass here says the week's validations were each preceded by the thing that
                # registered them; it does not say the registration was any good.
                "Pre-registration order (DL-K3 / KILL-R9)",
                (
                    {
                        "checked": (payload.get("preregistration") or {}).get("checked", 0),
                        "not judged (predate the check)": (payload.get("preregistration") or {}).get(
                            "skipped_as_predating_the_check", 0
                        ),
                        "verdict": "PASS",
                    }
                    if not ((payload.get("preregistration") or {}).get("problems") or [])
                    else {f"FAIL {i + 1}": line for i, line in enumerate(payload["preregistration"]["problems"])}
                ),
            ),
            ("Legs (M-008)", (payload.get("legs") or {}).get("pnl") or {"none": 0}),
            (
                "Effort share (target 90% on alpha)",
                {
                    "alpha_share": _fmt_pct((payload.get("effort") or {}).get("alpha_share")),
                    "target": _fmt_pct((payload.get("effort") or {}).get("target")),
                    "on_target": (payload.get("effort") or {}).get("on_target"),
                    "lines": json_dumps((payload.get("effort") or {}).get("lines") or {}),
                }
                if payload.get("effort")
                else {"none": 0},
            ),
            (
                "Margin (M-007)",
                {
                    "peak_standing_usage": _fmt_pct((payload.get("margin") or {}).get("peak_standing_usage")),
                    "peak_order_demand": _fmt_pct((payload.get("margin") or {}).get("peak_margin_usage")),
                    "insufficient_margin_rejections": (payload.get("margin") or {}).get("insufficient_margin"),
                },
            ),
        ],
    )


def daily_markdown(payload: dict[str, Any]) -> str:
    return render_markdown(
        f"Daily report {payload['day']}",
        [
            (
                "Equity",
                {
                    k: payload[k]
                    for k in ("equity_start", "equity_end", "equity_change_pct", "cycles", "skipped_cycles")
                },
            ),
            (
                # L1-10: the ladder and the vol sizing divide by the line above, and on multi-assets
                # margin that line carries BTC.  Printing the split is what lets a reader tell a
                # drawdown the strategy caused from one the collateral did.  Reported, never enforced.
                "Collateral in equity (L1-10)",
                dict(payload.get("collateral") or {"share": None, "why": "no cycle recorded it yet"}),
            ),
            (
                # L1-10's other half, and the instrument the 2026-09-08 ruling owes (RISK-G11).  It
                # reached `reports/daily/*.json` and stopped there until 2026-09-09; the line a reader
                # of the equity number above actually needs is `account_misleads`.
                "Collateral repricing (RISK-G11)",
                _collateral_drift_lines(payload.get("collateral_drift") or {}),
            ),
            (
                # AC-L4: the same sentence as the line below it, one restart over.  RISK-P2 assumed a
                # deployment restart costs late fills and a rebalance; this is where that stops being
                # an assumption - and, since 2026-09-09, where it is compared to M-Q03's own bar.
                "Restart cost (M-Q03 / DL-L4 / RISK-P2)",
                _restart_cost_lines(payload.get("restarts") or {}),
            ),
            (
                # D-041: the manifest was written into every report and read by nothing.  It is read now,
                # and this is where a human sees the answer after startup has scrolled away.
                "Dataset provenance (D-041)",
                {
                    key: json_dumps(value) if value else "none"
                    for key, value in (payload.get("dataset") or {"blocking": [], "advisory": []}).items()
                },
            ),
            ("Orders", payload["orders"] or {"none": 0}),
            ("External cash flows (not P&L)", payload.get("external_flows") or {"none": 0}),
            (
                "Costs",
                {
                    "traded_notional": payload["traded_notional"],
                    "commissions": payload["commissions"],
                    "funding": payload["funding"],
                    "realized_pnl": payload["realized_pnl"],
                },
            ),
            ("PnL by strategy", payload["pnl_by_strategy"] or {"none": 0}),
            ("PnL by symbol", payload["pnl_by_symbol"] or {"none": 0}),
            (
                # D-032: fills nobody in this loop placed - an operator flatten, a manual hedge
                "Foreign fills, excluded from the strategy series (D-032)",
                {
                    "total": payload["foreign_fills"]["total"],
                    "rows": payload["foreign_fills"]["rows"],
                    "by_symbol": json_dumps(payload["foreign_fills"]["by_symbol"]),
                    "cycles_that_could_not_reconcile": payload["foreign_fills"]["unreconciled_cycles"],
                }
                if payload["foreign_fills"]["rows"] or payload["foreign_fills"]["unreconciled_cycles"]
                else {"none": 0},
            ),
            ("Guard events", payload["guard_events"] or {"none": 0}),
            (
                # A planner that acts on nothing must still say what it looked at (P10 cell B's falsifier)
                "Plan gaps (no-trade band)",
                {
                    "band_held_symbol_cycles": payload["plan_gaps"]["band_held"],
                    "blocked_entry": json_dumps(payload["plan_gaps"]["blocked_entry"]),
                    "blocked_exit": json_dumps(payload["plan_gaps"]["blocked_exit"]),
                    "by_reason": json_dumps(payload["plan_gaps"]["by_reason"]),
                },
            ),
            (
                "Expectations (validation reports)",
                {
                    k: f"oos_sharpe={_fmt_num(v.get('oos_sharpe'))} full={_fmt_num(v.get('full_sample_sharpe'))} mdd={_fmt_num(v.get('full_sample_max_drawdown'))} {v.get('verdict')}"
                    for k, v in (payload.get("expectations") or {}).items()
                }
                or {"none": 0},
            ),
            ("Risk budget (P13)", _risk_budget_lines(payload.get("risk_budget") or {})),
            ("Drift vs expectation (equity)", payload.get("drift") or {"none": 0}),
            (
                "Evidence window (D-026 construction)",
                {
                    "construction": (payload.get("evidence_window") or {}).get("construction"),
                    "bars_under_it": (payload.get("evidence_window") or {}).get("bars"),
                    "construction_changes_last_7d": (payload.get("evidence_window") or {}).get("changes_7d"),
                },
            ),
            (
                "Drift vs expectation (attributed income, M-002/M-010)",
                {
                    "status": (payload.get("income_drift") or {}).get("status"),
                    **{
                        strategy: (
                            f"sharpe={_fmt_num(row.get('realised_sharpe'))} vs {_fmt_num(row.get('expected_sharpe'))} "
                            f"z={_fmt_num(row.get('z'))} pnl={_fmt_num(row.get('pnl'))} days={_fmt_num(row.get('days'))}"
                        )
                        for strategy, row in ((payload.get("income_drift") or {}).get("by_strategy") or {}).items()
                    },
                },
            ),
            (
                # §19 Q2's lagging criterion.  Rendered while it is still INSUFFICIENT_DATA because the
                # countdown IS the reading: a criterion nobody can see the distance to is a criterion
                # nobody waits for.
                "Long-run attributed Sharpe (M-G06)",
                _long_run_sharpe_lines(payload.get("long_run_sharpe") or {}),
            ),
            (
                "Legs (M-008)",
                {
                    f"{side} pnl": f"{_fmt_num(value)} over {(payload.get('legs') or {}).get('symbol_bars', {}).get(side)} symbol-bars"
                    for side, value in ((payload.get("legs") or {}).get("pnl") or {}).items()
                }
                or {"none": 0},
            ),
            (
                "Probe correlation (M-014)",
                {
                    pair: f"{_fmt_num(row.get('correlation'))} over {row.get('bars')} bars"
                    for pair, row in (payload.get("probe_correlation") or {}).items()
                }
                or {"none": 0},
            ),
            (
                # D-025: not an alert, but every timestamp above is the host's, so say how far off it is
                "Clock (D-025)",
                payload.get("clock") or {"none": 0},
            ),
            (
                "Research data coverage",
                payload.get("data_coverage") or {"none": 0},
            ),
            (
                "Margin and rejections (M-007)",
                {
                    # standing = margin the held book consumes; order_demand = what new orders asked for
                    "peak_standing_usage": _fmt_pct((payload.get("margin") or {}).get("peak_standing_usage")),
                    "last_standing_usage": _fmt_pct((payload.get("margin") or {}).get("last_standing_usage")),
                    "peak_order_demand": _fmt_pct((payload.get("margin") or {}).get("peak_margin_usage")),
                    "budget": _fmt_pct((payload.get("margin") or {}).get("budget")),
                    "over_budget": (payload.get("margin") or {}).get("over_budget"),
                    "insufficient_margin_rejections": (payload.get("margin") or {}).get("insufficient_margin"),
                    "rejections_by_code": json_dumps((payload.get("margin") or {}).get("rejections") or {}),
                },
            ),
            (
                "Exits and pool (M-005 / M-006)",
                {
                    "exits": (payload.get("events") or {}).get("exit_count"),
                    "by_rule": json_dumps((payload.get("events") or {}).get("by_rule") or {}),
                    "pool_entered": json_dumps((payload.get("events") or {}).get("pool_entered") or []),
                    "pool_left": json_dumps((payload.get("events") or {}).get("pool_left") or []),
                    "pool_quarantined": json_dumps((payload.get("events") or {}).get("pool_quarantined") or []),
                },
            ),
            (
                "Noise scale (DL-EX0)",
                {
                    "design_daily_sigma_u": _fmt_num((payload.get("noise_scale") or {}).get("design_daily_sigma_u")),
                    "realised_daily_sigma_u": _fmt_num(
                        (payload.get("noise_scale") or {}).get("realised_daily_sigma_u")
                    ),
                    "peak_giveback_u": _fmt_num((payload.get("noise_scale") or {}).get("peak_giveback_u")),
                    "giveback_in_design_sigma": _fmt_num(
                        (payload.get("noise_scale") or {}).get("giveback_in_design_sigma")
                    ),
                    "expected_exits_so_far": _fmt_num((payload.get("noise_scale") or {}).get("expected_exits_so_far")),
                    "exits_so_far": (payload.get("noise_scale") or {}).get("exits_so_far"),
                },
            ),
            (
                "Exit counterfactuals (M-005, monitoring only)",
                {
                    "events": (payload.get("exit_counterfactual") or {}).get("events"),
                    "pending": (payload.get("exit_counterfactual") or {}).get("pending"),
                    "mean_24h_u": _fmt_num(
                        ((payload.get("exit_counterfactual") or {}).get("by_horizon") or {})
                        .get("24", {})
                        .get("mean_counterfactual_u")
                    ),
                    "mean_72h_u": _fmt_num(
                        ((payload.get("exit_counterfactual") or {}).get("by_horizon") or {})
                        .get("72", {})
                        .get("mean_counterfactual_u")
                    ),
                    "cost_saved_u": _fmt_num((payload.get("exit_counterfactual") or {}).get("cost_saved_u")),
                    "n_needed_for_decision": (payload.get("exit_counterfactual") or {}).get("n_needed_for_decision"),
                },
            ),
            (
                "Probe books (D-019)",
                {
                    str(row.get("book")): (
                        f"{row.get('status')} strategy={row.get('strategy')} "
                        f"pnl_{row.get('window_days')}d={_fmt_num(row.get('pnl'))} "
                        f"({_fmt_pct(row.get('pnl_pct'))} of equity, stop at -{_fmt_pct(row.get('max_loss'))}) "
                        f"days={_fmt_num(row.get('days_running'))}/{row.get('review_after_days')}"
                        # M-014 beside the countdown it belongs to.  The correlation was computed
                        # every day and read by nothing, and the one moment it decides anything is
                        # this review - `probe_correlation`'s own docstring says a sleeve that
                        # correlates closely with the main book is a tilt whose separate risk budget
                        # is a fiction.  2026-09-12: tsmom~flow 0.80 over 148 bars, review due 10-02.
                        + _probe_correlation_note(payload, str(row.get("strategy") or ""))
                    )
                    for row in (payload.get("probes") or [])
                }
                or {"none": 0},
            ),
            (
                # Where per-symbol adaptation actually lives (D-037): the weight, not the leverage
                "Risk adaptation per symbol (M-015)",
                _risk_adaptation_lines(payload.get("risk_adaptation") or {}),
            ),
            ("Last targets", payload["last_targets"] or {"none": 0}),
        ],
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _fmt_num(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None else f"{100.0 * float(value):.2f}%"
