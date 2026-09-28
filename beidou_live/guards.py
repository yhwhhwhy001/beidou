"""Minimal guards that protect the validity of the experiment (D-004).  They never add risk.

The two guards that bind the whole book - the per-symbol cap plus the gross cap, and the daily-loss
pause - are defined once in ``beidou_alpha.overlays.exposure`` and called from here, so the backtest
replay and this loop cannot drift.  Only the cycle-level policy lives here: what makes a cycle skip,
what turns ``allow_increase`` off, and what gets recorded as a reason.  And one input the replay does not
need: the gross cap's denominator (``max_gross_denominator``).  The backtest holds no collateral, so there
total equity and USDT are the same number and the replay's cap is already the USDT one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from beidou_alpha.overlays.exposure import clamp_book, hold_or_reduce

#: What `max_gross` is a multiple of.  "equity" is total equity, BTC collateral included - the historical
#: reading and the default.  "usdt_equity" is the venue's USDT balance, the money that can open a position.
GROSS_DENOMINATORS = ("equity", "usdt_equity")


@dataclass(frozen=True)
class GuardParams:
    daily_loss_pause: float = -0.05
    stale_bars_max: int = 2
    max_gross: float = 2.0
    max_weight: float = 0.15
    # risk-g11-denominator, operator ruling 2026-09-28.  `max_gross` 2.0 and `margin_cap` 0.40 were set on
    # a backtest that holds no collateral, where the two denominators are one number; on the demo account
    # about 43% of equity is BTC.  `margin_cap` needs no switch of its own: D-016 sets leverage from the
    # RATIO max_gross / margin_cap, so the margin at the gross cap is a share of whatever the cap divides by.
    max_gross_denominator: str = "equity"


@dataclass
class GuardDecision:
    targets: dict[str, float]
    allow_increase: bool = True
    skip_cycle: bool = False
    reasons: list[str] = field(default_factory=list)


# The reason CODES are what `cycles.jsonl`, the report and the tests read, so they stay in place and
# stay stable.  The operator's channel is Chinese (ruling 2026-09-08), and a bare `GROSS_CAPPED` in a
# Chinese alert is a line the reader has to translate before they can act on it, so the alert renders
# the code with its gloss rather than replacing it: the record keeps the token, the human gets both.
REASON_ZH = {
    "STALE_MARKET_DATA": "行情数据陈旧",
    "KILL_SWITCH": "紧急停止开关已启用",
    "DAILY_LOSS_PAUSE": "当日亏损触发暂停加仓",
    "GROSS_CAPPED": "总敞口已被上限截断",
    "NO_USDT_EQUITY": "可动用 USDT 读不到或不为正，只减不加",
}


def describe_guard_reason(reason: str) -> str:
    """``CODE（中文）`` for an alert; an unknown code still prints itself rather than vanishing."""
    gloss = REASON_ZH.get(reason)
    return f"{reason}（{gloss}）" if gloss else reason


def evaluate_guards(
    targets: Mapping[str, float],
    *,
    current_weights: Mapping[str, float],
    kill_switch: bool,
    equity: float,
    day_start_equity: float | None,
    latest_bar_ms: int | None,
    expected_bar_ms: int,
    interval_ms: int,
    params: GuardParams,
    usdt_equity: float | None = None,
) -> GuardDecision:
    decision = GuardDecision(targets=dict(targets))
    if latest_bar_ms is None or expected_bar_ms - latest_bar_ms > params.stale_bars_max * interval_ms:
        decision.skip_cycle = True
        decision.reasons.append("STALE_MARKET_DATA")
        return decision
    if kill_switch:
        decision.allow_increase = False
        decision.reasons.append("KILL_SWITCH")
    if day_start_equity and equity > 0 and equity / day_start_equity - 1.0 < params.daily_loss_pause:
        decision.allow_increase = False
        decision.reasons.append("DAILY_LOSS_PAUSE")
    max_gross = params.max_gross
    if params.max_gross_denominator == "usdt_equity":
        if usdt_equity is not None and usdt_equity > 0 and equity > 0:
            # Weights are fractions of `equity`, so a cap on the USDT line is the same cap scaled by the USDT
            # share.  Never above 1: a USDT slice larger than equity (0 of 493 armed rows since 2026-09-07)
            # must not loosen the cap it replaces.
            max_gross *= min(1.0, usdt_equity / equity)
        else:
            # Unread or not positive: hold rather than guess.  A cap of 0 would switch `clamp_book`'s gross cap
            # OFF, and total equity would size on money that cannot open a position.  None of those 493 rows
            # lacked the reading.
            decision.allow_increase = False
            decision.reasons.append("NO_USDT_EQUITY")
    symbols = list(decision.targets)
    values = np.array([float(decision.targets[symbol]) for symbol in symbols], dtype=float)
    capped, gross_capped = clamp_book(values, params.max_weight, max_gross)
    if gross_capped:
        decision.reasons.append("GROSS_CAPPED")
    if not decision.allow_increase:
        held = np.array([float(current_weights.get(symbol, 0.0)) for symbol in symbols], dtype=float)
        capped = hold_or_reduce(capped, held)
    decision.targets = dict(zip(symbols, (float(value) for value in capped), strict=True))
    return decision
