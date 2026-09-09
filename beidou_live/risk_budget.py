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


def collateral_drift(rows: Sequence[Mapping[str, Any]], attribution: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How much of the window's equity change was collateral being repriced rather than the book trading.

    The operator ruled on 2026-09-08 (audit question 3) that the denominator stays total equity: it is
    the venue's own margin basis, and under cross margin the collateral really does absorb losses.  That
    ruling makes a pro-cyclical amplifier a NAMED accepted risk rather than an oversight - every weight
    is a fraction of an equity that is 52% non-USDT, so collateral up 10% is every target notional up
    5.2%, and the backtest models no collateral at all.

    An accepted risk with no instrument is a sentence, which is the failure this repository keeps
    finding, so this is the instrument.  It measures and changes nothing.  Measured over the first 23
    cycles that recorded a collateral reading (2026-09-07T15:00Z onward): equity -60.11, attributed
    P&L -16.19, so 73% of the move was repricing.

    Reported, never subtracted.  Subtracting it would silently turn this into the USDT-denominator book
    the operator did not choose.
    """
    priced = [row for row in rows if isinstance((row.get("collateral") or {}).get("equity"), int | float)]
    if len(priced) < 2:
        return {"enforced": False, "reason": f"needs 2 cycles with a collateral reading, has {len(priced)}"}
    first, last = priced[0]["collateral"], priced[-1]["collateral"]
    equity_change = float(last["equity"]) - float(first["equity"])
    since = int(priced[0].get("bar_open_ms") or 0)
    attributed = sum(float(row.get("total") or 0.0) for row in attribution if int(row.get("bar_open_ms") or 0) >= since)
    repriced = equity_change - attributed
    return {
        "enforced": True,
        "cycles": len(priced),
        "equity_change": equity_change,
        "attributed_pnl": attributed,
        "collateral_repricing": repriced,
        # `None` rather than 0.0 or 1.0 on a flat window: a share of nothing is not a share, and zero
        # would read as "none of it was collateral", which is the opposite of what it would mean.
        "repricing_share": None if equity_change == 0.0 else repriced / equity_change,
        "collateral_share": _latest_collateral_share(rows),
    }


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


def attributed_drawdown_state(
    rows: Sequence[Mapping[str, Any]],
    attribution: Sequence[Mapping[str, Any]],
    params: RiskBudgetParams,
) -> dict[str, Any]:
    """R8's ruler: the drawdown of the equity path with collateral repricing taken out.

    ``drawdown_state`` above reads venue equity, which is the operator's capital and therefore the
    right denominator for P13's budget.  It is the wrong NUMERATOR for a rule that decides whether the
    book is losing money: 52% of this account is non-USDT collateral and, measured over the 23 cycles
    that carried a collateral reading, 73% of the equity change was repricing rather than trading
    (KILL-AR-05).  A -35% equity drawdown can be bitcoin, and de-risking a book that never lost
    anything is a real cost paid for a reading that was never about the book.

    So the path here is ``base + cumsum(attributed P&L)``: it starts at the account's equity and moves
    only when the book realises something.  The high-water mark re-bases on the same event
    ``drawdown_state`` uses - a cycle whose external flows were re-baselined - because a demo reset
    arrives as a TRANSFER and carrying a pre-reset peak forward reports a drawdown nobody suffered.

    Like every metric in this file it refuses to report zero when it cannot compute: a record with no
    priced cycle, or with no attribution row inside it, says ``enforced: false`` and why.
    """
    by_bar: dict[int, float] = {}
    for row in attribution:
        bar = row.get("bar_open_ms")
        try:
            key = int(bar)  # type: ignore[arg-type]
            by_bar[key] = by_bar.get(key, 0.0) + float(row.get("total") or 0.0)
        except (TypeError, ValueError):
            continue
    base: float | None = None
    path = peak = 0.0
    drawdown = 0.0
    baseline_at: str | None = None
    bars = 0
    used = 0
    for row in rows:
        value = row.get("equity")
        if not isinstance(value, int | float) or value <= 0:
            continue
        equity = float(value)
        if base is None or (row.get("external_flows") or {}).get("rebaselined"):
            base = path = peak = equity
            baseline_at = str(row.get("at") or "")
            bars = 0
        bars += 1
        try:
            bar_key = int(row.get("bar_open_ms"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            bar_key = None
        if bar_key is not None and bar_key in by_bar:
            path += by_bar.pop(bar_key)
            used += 1
        peak = max(peak, path)
        current = 0.0 if peak <= 0 else path / peak - 1.0
        drawdown = min(drawdown, current)
    if base is None:
        return {
            "enforced": False,
            "why": "no cycle carried an equity reading, so there is no path to draw down from",
            "value": None,
            "action": None,
            "rows": 0,
        }
    if used == 0:
        return {
            "enforced": False,
            "why": "no attribution row lands on a priced cycle: the book has realised nothing to measure",
            "value": None,
            "action": None,
            "rows": 0,
            "base": base,
            "baseline_at": baseline_at,
        }
    action: str | None = None
    if current <= -params.rollback_at:
        action = f"vol_target -> {params.rollback_to}"
    elif current <= -params.deescalate_at:
        action = f"vol_target -> {params.deescalate_to}"
    return {
        "enforced": True,
        # `value` is the CURRENT distance below the running peak, because that is what a ladder is: a
        # book that recovered is not still down.  `drawdown_state` above reports the deepest reading
        # ever instead - correct for a monitor answering "was the budget ever breached", wrong for a
        # rule that acts, which would then be pinned to the worst hour this account ever had.  Both are
        # returned so neither question has to be answered with the other one's number.
        # negative, so it reads the same way as `Policy.throttle_scalar` takes it
        "value": current,
        "max_drawdown": drawdown,
        "path": path,
        "peak": peak,
        "base": base,
        "attributed": path - base,
        "baseline_at": baseline_at,
        "bars": bars,
        "rows": used,
        "deescalate_at": -params.deescalate_at,
        "rollback_at": -params.rollback_at,
        "action": action,
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
        reason = f"只有 {len(returns)} 根 bar，需要 {params.min_vol_bars} 根"
    elif len(constructions) > 1:
        reason = f"窗口内有 {len(constructions)} 个构造"
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
            "why": f"只有 {fills} 笔成交，需要 {params.min_slippage_fills} 笔"
            + (f"；其中 {without_reference} 笔没有 decision_close" if without_reference else ""),
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
    rows: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    params: RiskBudgetParams,
    attribution: Sequence[Mapping[str, Any]] = (),
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
    # Both rulers, side by side, because the gap between them IS the reading: on 2026-09-09 the equity
    # path was 1.30% below its mark and the attributed path 0.23%, a factor of 5.6 on the same record.
    # R8 acts on the second one; this block is where a reader can see why that choice is not cosmetic.
    attributed = attributed_drawdown_state(rows, attribution, params)
    volatility = realised_vol(rows, params)
    slippage = slippage_bps(trades, params, latest_ms=_latest_ms(rows))
    guards = guard_firings(rows, params)
    reasons: list[str] = []
    if attributed["enforced"] and attributed["action"]:
        reasons.append(f"归因回撤 {attributed['value']:.1%}（R8 口径），已执行：{attributed['action']}")
    if drawdown["action"]:
        # Kept as a reason but named for what it is: this one is a notice, R8 does not act on it.
        reasons.append(f"权益自高水位回撤 {drawdown['value']:.1%}（含抵押品重估，R8 不据此动作）")
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
        "attributed_drawdown": attributed,
        "realised_vol": volatility,
        "slippage": slippage,
        "guards": guards,
    }


__all__ = [
    "RiskBudgetParams",
    "attributed_drawdown_state",
    "drawdown_state",
    "guard_firings",
    "realised_vol",
    "risk_budget_status",
    "slippage_bps",
]
