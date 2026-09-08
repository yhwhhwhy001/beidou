"""P13's pre-registered monitoring: is the book still inside the risk budget it was sized for?

`vol_target` was set to 0.30 against a declared 50% drawdown budget, from a bootstrap whose q95 sits at
-43.7%/-49.5%.  That bootstrap resamples weekly blocks, so it preserves within-week autocorrelation and
destroys the multi-month regime structure real bear markets have — it is optimistic by construction, and
the de-escalation ladder below is what covers the gap.  The thresholds were written before the change
went live and are not up for renegotiation when they fire; what is deliberately NOT automated is the
acting, because rewriting live position sizing from a cron job is a different risk than measuring it.

Every metric here refuses to read zero when it cannot be computed (the failure this project keeps
finding): a window with too few bars, or one straddling a construction change, reports `enforced: false`
with the reason rather than a number that looks like a pass.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from beidou_live.health import canonical_construction

DAY_MS = 86_400_000


@dataclass(frozen=True)
class RiskBudgetParams:
    deescalate_at: float = 0.35  # drawdown from the high-water mark -> step vol_target down
    rollback_at: float = 0.50  # -> all the way back
    deescalate_to: float = 0.225
    rollback_to: float = 0.15
    vol_band: tuple[float, float] = (0.26, 0.38)
    vol_window_days: int = 30
    min_vol_bars: int = 240  # ten days of hourly cycles before the vol estimate says anything
    # M-Q08 states the bar as "slippage <= 2x model", so it is expressed that way rather than as a
    # standalone number: a constant drifts away from the model it is supposed to track.  The old
    # `max_slippage_bps: 10.0` was set "against the cost model's 7 bps" - but 7 is `turnover_bps`,
    # which bundles the 5 bps taker fee with 2 bps of slippage, while this instrument measures price
    # slippage alone.  A fee-inclusive budget judging a fee-exclusive measurement, at 2.5x the stated
    # bar, could not fail: the measured +4.3 bps sat outside M-Q08's 4 and well inside the gate's 10.
    model_slippage_bps: float = 2.0  # what the backtest's cost model charges for slippage
    slippage_multiple: float = 2.0  # M-Q08's "2x"
    slippage_window_days: int = 30
    min_slippage_fills: int = 30
    guard_window_days: int = 90
    bars_per_year: float = 8760.0

    def __post_init__(self) -> None:
        if not 0 < self.deescalate_at < self.rollback_at < 1:
            raise ValueError("need 0 < deescalate_at < rollback_at < 1")
        if not 0 < self.vol_band[0] < self.vol_band[1]:
            raise ValueError("vol_band must be an increasing positive pair")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RiskBudgetParams:
        if "max_slippage_bps" in payload:
            raise ValueError(
                "max_slippage_bps is retired: the bar is M-Q08's '<= 2x model', so set model_slippage_bps "
                "and slippage_multiple instead.  Left as a plain key it would be dropped here without a "
                "word, and a threshold that silently does nothing is worse than no threshold (L1-04)"
            )
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        if "vol_band" in known:
            low, high = known["vol_band"]
            known["vol_band"] = (float(low), float(high))
        return cls(**known)

    @property
    def max_slippage_bps(self) -> float:
        return self.model_slippage_bps * self.slippage_multiple


def _latest_ms(rows: Sequence[Mapping[str, Any]]) -> int:
    return int(rows[-1].get("bar_open_ms") or 0) if rows else 0


def _latest_collateral_share(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """The newest cycle that recorded one; ``None`` when none did (the field is newer than the log)."""
    for row in reversed(rows):
        share = (row.get("collateral") or {}).get("share")
        if isinstance(share, int | float):
            return float(share)
    return None


def drawdown_state(rows: Sequence[Mapping[str, Any]], params: RiskBudgetParams) -> dict[str, Any]:
    """Drawdown from the high-water mark, with the mark reset at every re-baselined cycle.

    Re-basing matters on this venue: the demo account resets, and a reset shows up as a TRANSFER row
    rather than as a loss.  Carrying a pre-reset peak forward would report a drawdown nobody suffered.
    Note the direction of the remaining error honestly: the mark is NOT reset on a construction change,
    so a drawdown that began under the old vol target still counts here.  That is deliberate — the budget
    is the operator's capital, not this construction's track record — but it does mean the ladder can
    fire on a loss the current book did not cause.
    """
    peak = drawdown = 0.0
    equity = None
    baseline_at: str | None = None
    bars = 0
    for row in rows:
        value = row.get("equity")
        if not isinstance(value, int | float) or value <= 0:
            continue
        if (row.get("external_flows") or {}).get("rebaselined"):
            peak = float(value)
            baseline_at = str(row.get("at") or "")
        equity = float(value)
        bars += 1
        if baseline_at is None:
            baseline_at = str(row.get("at") or "")
        peak = max(peak, equity)
        drawdown = max(drawdown, 0.0 if peak <= 0 else 1.0 - equity / peak)
    action: str | None = None
    if drawdown >= params.rollback_at:
        action = f"vol_target -> {params.rollback_to}"
    elif drawdown >= params.deescalate_at:
        action = f"vol_target -> {params.deescalate_to}"
    return {
        "value": drawdown,
        "equity": equity,
        "peak": peak or None,
        "baseline_at": baseline_at,
        "bars": bars,
        "deescalate_at": params.deescalate_at,
        "rollback_at": params.rollback_at,
        "action": action,
        # L1-10 measured this and the daily report printed it four blocks away from the ladder that
        # divides by the same equity.  On 2026-09-07, 52.3% of it was non-USDT collateral, so a -35%
        # reading here can belong to BTC rather than to the book.  Reported beside the reading, never
        # subtracted from it: that denominator is a construction decision, not a bug fix (L1-10).
        # `None` rather than 0.0 when no cycle recorded it - zero would read as "none of it is".
        "collateral_share": _latest_collateral_share(rows),
    }


def realised_vol(rows: Sequence[Mapping[str, Any]], params: RiskBudgetParams) -> dict[str, Any]:
    """Annualised volatility of the live equity path, enforced only on a single-construction window.

    A bar that absorbed an external cash flow (deposit, demo reset; E-044) is a step in equity, not a
    return, and is skipped - as ``drawdown_state`` and ``reports.drift_check`` already do.  Not
    cosmetic: one +4.7% reset bar adds 0.163 of annualised vol, wider than the [0.26, 0.38] band.
    """
    cutoff = _latest_ms(rows) - params.vol_window_days * DAY_MS
    window = [row for row in rows if int(row.get("bar_open_ms") or 0) >= cutoff]
    priced = [row for row in window if isinstance(row.get("equity"), int | float)]
    # Canonicalised: a fingerprint field-set change is not a construction change, and this gate gave up
    # entirely when it saw two digests (2026-09-07, twice).
    constructions = {canonical_construction(row.get("construction")) for row in window if row.get("construction")}
    returns = [
        float(b["equity"]) / float(a["equity"]) - 1.0
        for a, b in pairwise(priced)
        if float(a["equity"]) > 0 and not (b.get("external_flows") or {}).get("rebaselined")
    ]
    reason = None
    if len(returns) < params.min_vol_bars:
        reason = f"{len(returns)} bars, needs {params.min_vol_bars}"
    elif len(constructions) > 1:
        reason = f"{len(constructions)} constructions in the window"
    if reason is not None:
        return {"value": None, "band": list(params.vol_band), "bars": len(returns), "enforced": False, "why": reason}
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    annual = math.sqrt(variance * params.bars_per_year)
    low, high = params.vol_band
    return {
        "value": annual,
        "band": [low, high],
        "bars": len(returns),
        "enforced": True,
        "inside": low <= annual <= high,
    }


def slippage_bps(trades: Sequence[Mapping[str, Any]], params: RiskBudgetParams, *, latest_ms: int) -> dict[str, Any]:
    """Notional-weighted adverse fill vs the DECISION BAR CLOSE, against M-Q08's "2x model" bar.

    The reference is the decision bar's close because that is what the backtest enters at: weights
    decided on bar t are executed over bar t+1 at its open (`decided.shift(1)` with `open_to_close`),
    and in a continuous market that open is bar t's close.  The venue mark this used to read is an
    index price sampled when the cycle woke; comparing to it answers a question M-Q08 did not ask.

    Rows without `decision_close` - every row written before this field existed - are counted in
    `without_reference` and excluded.  Falling back to the mark is exactly how the wrong number
    would come back, and history is not backfilled."""
    cutoff = latest_ms - params.slippage_window_days * DAY_MS
    weighted = notional = 0.0
    fills = without_reference = 0
    for row in trades:
        if int(row.get("bar_open_ms") or 0) < cutoff or row.get("flatten"):
            continue
        reference, filled = row.get("decision_close"), row.get("avg_price")
        if reference is None and filled and row.get("executed_qty"):
            without_reference += 1
            continue
        quantity = row.get("executed_qty")
        if not reference or not filled or not quantity:
            continue
        try:
            reference, filled, quantity = float(reference), float(filled), float(quantity)
        except (TypeError, ValueError):
            continue
        if reference <= 0 or filled <= 0 or quantity <= 0:
            continue
        sign = 1.0 if str(row.get("side", "")).upper() == "BUY" else -1.0
        adverse = sign * (filled - reference) / reference * 10_000.0
        size = filled * quantity
        weighted += adverse * size
        notional += size
        fills += 1
    if fills < params.min_slippage_fills or notional <= 0:
        return {
            "value": None,
            "limit": params.max_slippage_bps,
            "fills": fills,
            "without_reference": without_reference,
            "enforced": False,
            "why": f"{fills} fills, needs {params.min_slippage_fills}"
            + (f"; {without_reference} carry no decision_close" if without_reference else ""),
        }
    value = weighted / notional
    return {
        "value": value,
        "limit": params.max_slippage_bps,
        "fills": fills,
        "without_reference": without_reference,
        "notional": notional,
        "enforced": True,
        "inside": value <= params.max_slippage_bps,
    }


def guard_firings(rows: Sequence[Mapping[str, Any]], params: RiskBudgetParams) -> dict[str, Any]:
    """How often the two formerly dormant guards actually fire (M-003).

    Reported, never enforced here: the pre-registered test is against the backtest's own frequency, and
    that number lives in a research report rather than in the live state files.  A count that says
    nothing about its comparator is still worth having, because at vol_target 0.15 both were zero.
    """
    cutoff = _latest_ms(rows) - params.guard_window_days * DAY_MS
    window = [row for row in rows if int(row.get("bar_open_ms") or 0) >= cutoff]
    counts = {"DAILY_LOSS_PAUSE": 0, "GROSS_CAPPED": 0}
    for row in window:
        for reason in row.get("guard_reasons") or []:
            if reason in counts:
                counts[reason] += 1
    return {
        "daily_loss_pause_bars": counts["DAILY_LOSS_PAUSE"],
        "gross_capped_bars": counts["GROSS_CAPPED"],
        "bars": len(window),
        "window_days": params.guard_window_days,
    }


def risk_budget_status(
    rows: Sequence[Mapping[str, Any]], trades: Sequence[Mapping[str, Any]], params: RiskBudgetParams
) -> dict[str, Any]:
    """The whole P13 monitoring block: ALERT where a threshold is breached, BLIND where one cannot be read.

    Each metric already refuses to invent a number, but the summary used to collapse "inside the bar"
    and "no reading at all" into the same ``OK``.  On 2026-09-07 that is exactly what it did: L1-04 had
    just re-pointed the slippage instrument at ``decision_close``, no trade row carried one yet, and the
    top line of the report said OK while M-Q08 - one of the demo phase's two success criteria - had zero
    usable fills.  ``BLIND`` is not a lesser ALERT; it says the question was not answered.

    A breach still outranks a blind metric, and BLIND deliberately does not page: it clears itself once
    the readings arrive, which is the "unactionable standing fact" ``daily_alerts`` keeps as a notice.
    """
    drawdown = drawdown_state(rows, params)
    volatility = realised_vol(rows, params)
    slippage = slippage_bps(trades, params, latest_ms=_latest_ms(rows))
    guards = guard_firings(rows, params)
    reasons: list[str] = []
    if drawdown["action"]:
        reasons.append(f"自高水位回撤 {drawdown['value']:.1%}，应执行：{drawdown['action']}")
    if volatility["enforced"] and not volatility["inside"]:
        low, high = volatility["band"]
        reasons.append(f"实现波动率 {volatility['value']:.1%} 已跑出 {low:.0%}-{high:.0%} 区间")
    if slippage["enforced"] and not slippage["inside"]:
        reasons.append(f"滑点 {slippage['value']:.1f} bps 高于假设的 {slippage['limit']:.0f} bps")
    unreadable = [
        {"metric": name, "why": str(block.get("why", ""))}
        for name, block in (("realised_vol", volatility), ("slippage", slippage))
        if not block["enforced"]
    ]
    return {
        "status": "ALERT" if reasons else ("BLIND" if unreadable else "OK"),
        "reasons": reasons,
        "unreadable": unreadable,
        "drawdown": drawdown,
        "realised_vol": volatility,
        "slippage": slippage,
        "guards": guards,
    }


__all__ = [
    "RiskBudgetParams",
    "drawdown_state",
    "guard_firings",
    "realised_vol",
    "risk_budget_status",
    "slippage_bps",
]
