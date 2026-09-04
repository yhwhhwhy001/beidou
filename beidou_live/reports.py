"""Daily report from the live state files (markdown + json)."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import numpy as np

from beidou_alpha.report import render_markdown
from beidou_alpha.validation.metrics import max_drawdown, sharpe
from beidou_live.probe import ProbeParams, probe_status
from beidou_live.state import StateStore


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
    current = rows[-1]["construction"]
    since = rows[-1]
    for row in reversed(rows):
        if row.get("construction") != current:
            break
        since = row
    latest_ms = int(rows[-1].get("bar_open_ms") or 0)
    recent = [row for row in rows if int(row.get("bar_open_ms") or 0) >= latest_ms - 7 * DAY_MS]
    changes = sum(1 for a, b in pairwise(recent) if a.get("construction") != b.get("construction"))
    return {
        "construction": str(current)[:12],
        "since_ms": int(since.get("bar_open_ms") or 0),
        "bars": sum(1 for row in rows if row.get("construction") == current),
        "changes_7d": changes,
    }


def _series_by_strategy(store: StateStore, since_ms: int | None) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for row in store.read_jsonl(store.attribution_path):
        bar = row.get("bar_open_ms")
        if not isinstance(bar, int | float) or (since_ms is not None and int(bar) < since_ms):
            continue
        for strategy, value in (row.get("by_strategy") or {}).items():
            try:
                out.setdefault(str(strategy), []).append((int(bar), float(value)))
            except (TypeError, ValueError):
                continue
    return out


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
    """M-007: peak initial-margin usage and the count of venue rejections, by code.

    The plan set two numbers - usage at or below 50% of equity, and zero -2019 (insufficient margin)
    rejections - and neither was ever computed.  The per-cycle ``margin`` block existed but no series was
    taken from it, and order rejections collapsed into a single REJECTED status, so a -2019 could not be
    told apart from a lot-size error.
    """
    peak = 0.0
    peak_bar: int | None = None
    for row in _cycles(store):
        bar_ms = int(row.get("bar_open_ms") or 0)
        if since_ms is not None and bar_ms < since_ms:
            continue
        margin = row.get("margin") or {}
        equity = row.get("equity")
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
        "peak_margin_usage": peak,
        "peak_at_bar_ms": peak_bar,
        "budget": 0.50,
        "over_budget": peak > 0.50,
        "rejections": rejections,
        "insufficient_margin": sum(count for code, count in rejections.items() if "-2019" in code),
    }


def exit_and_pool_events(store: StateStore, day: str) -> dict[str, Any]:
    """M-005 and M-006: the two things that change the book without a signal changing its mind."""
    rows = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    exits = [
        {"symbol": event.get("symbol"), "rule": event.get("rule"), "bar": row.get("bar")}
        for row in rows
        for event in (row.get("exit_events") or [])
    ]
    entered: list[str] = []
    left: list[str] = []
    for row in rows:
        update = row.get("universe_update") or {}
        entered.extend(str(s) for s in (update.get("entered") or []))
        left.extend(str(s) for s in (update.get("left") or []))
    return {
        "exits": exits,
        "exit_count": len(exits),
        "by_rule": {rule: sum(1 for e in exits if e["rule"] == rule) for rule in {str(e["rule"]) for e in exits}},
        "pool_entered": entered,
        "pool_left": left,
        "pool_changes": len(entered) + len(left),
    }


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
            reasons.append(f"realised Sharpe {realised:.2f} is {abs(z):.1f} s.e. below expected {expected_sharpe:.2f}")
    if worst_expected_mdd is not None and drawdown < 1.5 * worst_expected_mdd:
        status = "ALERT"
        reasons.append(f"trailing drawdown {drawdown:.3f} exceeds 1.5x the validated drawdown {worst_expected_mdd:.3f}")
    return {
        "status": status,
        "window_days": round(days, 1),
        "realised_sharpe": realised,
        "expected_sharpe": expected_sharpe,
        "trailing_drawdown": drawdown,
        "validated_drawdown": worst_expected_mdd,
        "reasons": reasons,
    }


def daily_payload(
    store: StateStore, day: str, expectations: dict[str, Any] | None = None, probes: Sequence[ProbeParams] = ()
) -> dict[str, Any]:
    cycles = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
    trades = [row for row in store.read_jsonl(store.trades_path) if _day_of(row) == day]
    attributions = [row for row in store.read_jsonl(store.attribution_path) if _day_of(row) == day]
    equities = [float(row["equity"]) for row in cycles if row.get("equity") is not None]
    by_strategy: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    commissions = funding = realized = 0.0
    for row in attributions:
        for strategy, value in (row.get("by_strategy") or {}).items():
            by_strategy[strategy] = by_strategy.get(strategy, 0.0) + float(value)
        for symbol, bucket in (row.get("by_symbol") or {}).items():
            by_symbol[symbol] = by_symbol.get(symbol, 0.0) + float(bucket.get("total", 0.0))
            commissions += float(bucket.get("COMMISSION", 0.0))
            funding += float(bucket.get("FUNDING_FEE", 0.0))
            realized += float(bucket.get("REALIZED_PNL", 0.0))
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
        "orders": statuses,
        "traded_notional": traded,
        "realized_pnl": realized,
        "commissions": commissions,
        "funding": funding,
        "pnl_by_strategy": by_strategy,
        "pnl_by_symbol": by_symbol,
        "guard_events": {event: guard_events.count(event) for event in set(guard_events)},
        "last_targets": cycles[-1].get("targets") if cycles else {},
        "expectations": expectations or {},
        "drift": drift_check(store, expectations or {}),
        "evidence_window": window,
        "income_drift": income_drift(
            store, expectations or {}, equity=equities[-1] if equities else None, since_ms=window["since_ms"]
        ),
        "legs": leg_split(store, since_ms=window["since_ms"], equity=equities[-1] if equities else None),
        "probe_correlation": probe_correlation(store, probes, since_ms=window["since_ms"]),
        "events": exit_and_pool_events(store, day),
        "margin": margin_and_rejections(store, since_ms=window["since_ms"]),
        "probes": probe_rows(store, probes, equity=equities[-1] if equities else None, now_ms=_day_end_ms(day)),
    }


def weekly_payload(store: StateStore, day: str, *, expectations: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "legs": leg_split(store, since_ms=since_ms, equity=equities[-1] if equities else None),
        "margin": margin_and_rejections(store, since_ms=since_ms),
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
            ("Legs (M-008)", (payload.get("legs") or {}).get("pnl") or {"none": 0}),
            (
                "Margin (M-007)",
                {
                    "peak_usage": _fmt_pct((payload.get("margin") or {}).get("peak_margin_usage")),
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
            ("Guard events", payload["guard_events"] or {"none": 0}),
            (
                "Expectations (validation reports)",
                {
                    k: f"oos_sharpe={_fmt_num(v.get('oos_sharpe'))} full={_fmt_num(v.get('full_sample_sharpe'))} mdd={_fmt_num(v.get('full_sample_max_drawdown'))} {v.get('verdict')}"
                    for k, v in (payload.get("expectations") or {}).items()
                }
                or {"none": 0},
            ),
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
                "Margin and rejections (M-007)",
                {
                    "peak_margin_usage": _fmt_pct((payload.get("margin") or {}).get("peak_margin_usage")),
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
                    )
                    for row in (payload.get("probes") or [])
                }
                or {"none": 0},
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
