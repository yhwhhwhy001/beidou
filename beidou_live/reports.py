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

from beidou_alpha.panel import interval_seconds
from beidou_alpha.report import render_markdown
from beidou_alpha.validation.metrics import max_drawdown, sharpe
from beidou_data.store import KlineStore
from beidou_live.probe import ProbeParams, probe_status
from beidou_live.risk_budget import RiskBudgetParams, risk_budget_status
from beidou_live.state import StateStore

RISK_COMPRESSION_LIMIT = 0.50  # see `risk_adaptation`: a bound between "working" (0.13) and "stage 1 deleted" (1.0)


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
    rows = [row for row in store.read_jsonl(store.cycles_path) if _day_of(row) == day]
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
    so far in the current construction.
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
    exits = sum(len(row.get("exit_events") or []) for row in trailing if int(row.get("bar_open_ms") or 0) >= int(window.get("since_ms") or 0))
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
        if not events or record.get("equity") is None or record.get("dry_run"):
            continue
        equity = float(record["equity"])
        bar = int(record.get("bar_open_ms") or 0)
        for event in events:
            if event.get("rule") == "COOLDOWN" or not event.get("price"):
                continue
            symbol = str(event.get("symbol"))
            notional = float(event.get("target") or 0.0) * equity
            cost_saved += 2.0 * TURNOVER_BPS / 10_000.0 * abs(notional)
            if symbol not in cache:
                try:
                    cache[symbol] = loader(symbol)
                except FileNotFoundError:
                    cache[symbol] = pd.Series(dtype=float)
            series = cache[symbol]
            row = {"symbol": symbol, "rule": event.get("rule"), "bar_open_ms": bar, "price": float(event["price"])}
            complete = True
            for horizon in horizons:
                future = bar + horizon * span_ms
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


def _risk_adaptation_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """Same rule as `_risk_budget_lines`: a spread that could not be computed says why, not "n/a"."""
    leverages = block.get("leverage_distinct")
    venue = f"{leverages} distinct value(s) across the book" if leverages else "nothing set yet"
    if not block.get("enforced"):
        return {"status": f"not enforced ({block.get('reason', 'no reason recorded')})", "exchange leverage": venue}
    return {
        "status": f"{block.get('status')} - stage 1 removes {1.0 - float(block['compression']):.0%} of the "
        f"market's dispersion (compression {block['compression']:.2f}, alert above {block['limit']:.2f})",
        "market vol spread": f"{block['vol_spread']:.1f}x across {block['symbols']} held symbols",
        "risk contribution spread": f"{block['risk_spread']:.1f}x - this is what sizing equalises",
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
    reads 1.0.  Measured on the live book 2026-09-05 it reads 0.13 (vol 12.2x -> risk 1.6x).  The
    0.50 limit is a wide bound placed between those two, not a derived threshold, because several
    honest effects push it up: the no-trade band holds a stale weight while sigma moves under it,
    ``max_weight`` binds on the calmest names, and a signal with magnitude (today's tsmom is pure
    sign) puts conviction back into the numerator.

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
    sigmas = [row["annual_vol"] for row in held]
    vol_spread = max(sigmas) / min(sigmas)
    risk_spread = held[-1]["risk"] / held[0]["risk"]
    compression = risk_spread / vol_spread if vol_spread > 0 else None
    return {
        "enforced": True,
        "reason": None,
        "symbols": len(held),
        "vol_spread": vol_spread,
        "risk_spread": risk_spread,
        "compression": compression,
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


def restart_cost(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """AC-L4 / RISK-P2: what the day's restarts actually cost, counted rather than assumed.

    DL-L4 wrote both facts into every cycle row - how late the wake-up was relative to the bar close,
    and whether that lateness cost a rebalance - and nothing read them.  The plan assumed every
    deployment restart costs late fills and 7bps of gross; this is the number that would show whether
    the assumption is generous or mean.

    ``missed_rebalances`` is a RUNNING TOTAL held on the engine, so summing the column double-counts
    every row after the first miss.  It is also per process: a value that drops means a new process
    started, and the day owes both stretches.  Hence max-per-stretch, summed across stretches.
    """
    missed = 0
    running = 0
    skipped = 0
    late: list[float] = []
    for row in rows:
        if row.get("phase") == "SKIPPED" or row.get("reason") == "restart outside the rebalance window":
            skipped += 1
        seen = row.get("missed_rebalances")
        if isinstance(seen, int):
            if seen < running:  # the counter reset: a new process
                missed += running
                running = seen
            else:
                running = seen
        value = row.get("late_seconds")
        if isinstance(value, (int, float)):
            late.append(float(value))
    missed += running
    return {
        "missed_rebalances": missed,
        "skipped_bars": skipped,
        "worst_late_seconds": max(late) if late else None,
        "late_bars": len(late),
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
        # AC-L4 / RISK-P2: what the day's restarts cost.  DL-L4 wrote these into the cycle rows and
        # nothing read them; a cost that only exists in a JSONL is an assumption, not a measurement.
        "restarts": restart_cost(cycles),
        "last_targets": cycles[-1].get("targets") if cycles else {},
        "expectations": expectations or {},
        "risk_budget": risk_budget_status(
            _cycles(store), store.read_jsonl(store.trades_path), risk_budget or RiskBudgetParams()
        ),
        "drift": drift_check(store, expectations or {}),
        "evidence_window": window,
        "income_drift": income_drift(
            store, expectations or {}, equity=equities[-1] if equities else None, since_ms=window["since_ms"]
        ),
        "legs": leg_split(store, since_ms=window["since_ms"], equity=equities[-1] if equities else None),
        "probe_correlation": probe_correlation(store, probes, since_ms=window["since_ms"]),
        "events": exit_and_pool_events(store, day),
        "noise_scale": noise_scale(store, day, vol_target=vol_target),
        "exit_counterfactual": exit_counterfactuals(store, closes=closes, root=data_root),
        "plan_gaps": plan_gaps(store, day),
        "clock": clock_health(store, day),
        "data_coverage": data_coverage(store),
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
    for name, key in (("equity", "drift"), ("income", "income_drift")):
        block = payload.get(key) or {}
        if str(block.get("status")) == "ALERT":
            detail = block.get("reasons") or [
                f"{strategy}: z={row.get('z'):.1f}"
                for strategy, row in (block.get("by_strategy") or {}).items()
                if row.get("z") is not None and row["z"] < -2.0
            ]
            alerts.append(f"{name} drift ALERT: {'; '.join(str(d) for d in detail)}")
    budget = payload.get("risk_budget") or {}
    if str(budget.get("status")) == "ALERT":
        # P13's ladder: the thresholds were fixed before the change went live, so this says what to do
        # rather than that something looks off.  It alerts; a human still runs the one-line change.
        alerts.append("risk budget ALERT: " + "; ".join(str(r) for r in budget.get("reasons") or []))
    adaptation = payload.get("risk_adaptation") or {}
    if str(adaptation.get("status")) == "ALERT":
        # M-015: the weights stopped taking each symbol's volatility back out.  Loud rather than
        # quiet because this is the layer D-037 pointed at when it ruled the leverage layer inert.
        alerts.append(
            f"risk adaptation ALERT: compression {adaptation.get('compression'):.2f} > "
            f"{adaptation.get('limit'):.2f}; per-symbol sizing is no longer vol-scaled"
        )
    notices: list[str] = []
    window = payload.get("evidence_window") or {}
    if int(window.get("changes_7d") or 0) > 1:
        # the plan allowed one promotion per week and nothing ever counted them
        notices.append(
            f"{window['changes_7d']} construction changes in the last 7 days; the plan allows one promotion per week"
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
                # AC-L4: the same sentence as the line below it, one restart over.  RISK-P2 assumed a
                # deployment restart costs late fills and a rebalance; this is where that stops being
                # an assumption.
                "Restart cost (DL-L4 / RISK-P2)",
                dict(payload.get("restarts") or {"missed_rebalances": 0}),
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
                    "realised_daily_sigma_u": _fmt_num((payload.get("noise_scale") or {}).get("realised_daily_sigma_u")),
                    "peak_giveback_u": _fmt_num((payload.get("noise_scale") or {}).get("peak_giveback_u")),
                    "giveback_in_design_sigma": _fmt_num((payload.get("noise_scale") or {}).get("giveback_in_design_sigma")),
                    "expected_exits_so_far": _fmt_num((payload.get("noise_scale") or {}).get("expected_exits_so_far")),
                    "exits_so_far": (payload.get("noise_scale") or {}).get("exits_so_far"),
                },
            ),
            (
                "Exit counterfactuals (M-005, monitoring only)",
                {
                    "events": (payload.get("exit_counterfactual") or {}).get("events"),
                    "pending": (payload.get("exit_counterfactual") or {}).get("pending"),
                    "mean_24h_u": _fmt_num(((payload.get("exit_counterfactual") or {}).get("by_horizon") or {}).get("24", {}).get("mean_counterfactual_u")),
                    "mean_72h_u": _fmt_num(((payload.get("exit_counterfactual") or {}).get("by_horizon") or {}).get("72", {}).get("mean_counterfactual_u")),
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
