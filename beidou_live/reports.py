"""Daily report from the live state files (markdown + json)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

from beidou_alpha.report import render_markdown
from beidou_alpha.validation.metrics import max_drawdown, sharpe
from beidou_live.state import StateStore


def _day_of(record: dict[str, Any]) -> str | None:
    bar = record.get("bar_open_ms")
    if isinstance(bar, int | float):
        return datetime.fromtimestamp(bar / 1000, tz=UTC).strftime("%Y-%m-%d")
    stamp = record.get("at")
    return str(stamp)[:10] if stamp else None


def expectations_from_evidence(evidence_by_strategy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What the validation reports promised: OOS Sharpe, full-sample Sharpe/MDD per strategy."""
    out: dict[str, Any] = {}
    for strategy, report in evidence_by_strategy.items():
        wf = report.get("walk_forward", {}) or {}
        full = report.get("full_sample", {}) or {}
        out[strategy] = {
            "oos_sharpe": wf.get("oos_sharpe"),
            "full_sample_sharpe": full.get("annualized_sharpe"),
            "full_sample_max_drawdown": full.get("max_drawdown"),
            "verdict": report.get("verdict"),
        }
    return out


def drift_check(
    store: StateStore, expectations: dict[str, Any], *, window_days: int = 30, bars_per_year: float = 8760.0
) -> dict[str, Any]:
    """Realised trailing Sharpe/drawdown from cycle equity versus the validation expectation."""
    cycles = [
        row for row in store.read_jsonl(store.cycles_path) if row.get("equity") is not None and not row.get("dry_run")
    ]
    equity = [float(row["equity"]) for row in cycles][-(window_days * 24) :]
    if len(equity) < 48:
        return {"status": "INSUFFICIENT_DATA", "bars": len(equity)}
    returns = np.asarray([equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity)) if equity[i - 1] > 0])
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


def daily_payload(store: StateStore, day: str, expectations: dict[str, Any] | None = None) -> dict[str, Any]:
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
    return {
        "day": day,
        "cycles": len(cycles),
        "skipped_cycles": sum(1 for row in cycles if row.get("skip")),
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
    }


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
            ("Drift vs expectation", payload.get("drift") or {"none": 0}),
            ("Last targets", payload["last_targets"] or {"none": 0}),
        ],
    )


def _fmt_num(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"
