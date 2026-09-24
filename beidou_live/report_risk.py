"""The daily risk dashboard: how big the book is, how much it can lose, and what binds it.

Checklist area #3 of `docs/analysis/2026-09-23-external-prompt-checklist-vs-beidou.md`: the risk
budget and its ladder (P13), the sigma ruler (DL-EX0) with the backtest VaR / ES beside it (G4),
margin (M-007), collateral (L1-10 / RISK-G11), the long and short legs (M-008) and per-symbol risk
adaptation (M-015, D-046).  Item 3.9, what closing each held position would take, sits beside margin.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.backtest import ImpactModel
from beidou_alpha.overlays.exits import COOLDOWN
from beidou_alpha.registry import MAIN_BOOK
from beidou_data.store import KlineStore, interval_ms
from beidou_live.report_common import (
    DAY_MS,
    _cycles,
    _day_of,
    _fmt_num,
    _fmt_pct,
    evidence_window,
    json_dumps,
    readable_state,
)
from beidou_live.risk_budget import books_by_symbol
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


PLAN_MARGIN_BUDGET = 0.50  # the plan's M-007 number; superseded as the BAR by `margin_cap` (2026-09-14)


def margin_and_rejections(
    store: StateStore, *, since_ms: int | None, margin_cap: float | None = None
) -> dict[str, Any]:
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

    2026-09-14: the BAR is now ``margin_cap`` when the caller can supply it, with the plan's 50% kept
    beside it as ``plan_budget`` rather than deleted.  Two numbers governed one quantity - the plan's
    50% doing the judging here, and the profile's ``margin_cap`` 0.40 checked once at startup against
    the CONFIG (D-016) and never against the reading - so realized standing margin could sit anywhere
    in 40-50%, above the declared policy, and this metric read OK.  That is the ``max_slippage_bps``
    shape the profile already records: a bar 1.25x looser than the policy it stands for cannot fail
    before the policy is already breached.  ``margin_cap`` wins because it is the number the operator
    configured and the one D-016 derives the venue leverage from; the plan's 50% stays visible because
    what a corrected ruler was wrong ABOUT is the part a later reader needs.

    A breach means realized ``gross/equity`` exceeded ``max_gross`` between rebalances - the only path
    there is, since stage 3 clips gross at rebalance and margin is ``gross / L``.  It self-corrects at
    the next rebalance, which is why ``daily_alerts`` routes it as a notice rather than a page.

    2026-09-19, and this is the 2026-09-14 correction one layer down: the BAR was fixed then, the
    DENOMINATOR is fixed now.  ``margin_usage`` divides by total equity, which on multi-assets margin
    is about half BTC collateral - money that backs the position but cannot open one.  ``margin_cap``
    0.40 was calibrated on the backtest, and the backtest models no collateral at all (RISK-G11), so
    0.40 was always a statement about the USDT line.  Measuring it against total equity made the ruler
    1.9x looser than the policy it stands for - the identical shape to the 1.25x the entry above
    describes, and it had the identical effect: on 2026-09-13T22:00Z the book stood at 22.73% of equity
    and **48.71% of tradable USDT**, and M-007 has never once reported a breach.  Gross the same day
    read 1.20x equity against a ``max_gross`` of 2.0, and 2.44x tradable.

    So ``over_budget_tradable`` is the reading that judges, and ``over_budget`` stays beside it because
    what a corrected ruler was wrong ABOUT is the part a later reader needs - the same rule the entry
    above follows.  Nothing here changes what the loop DOES: ``max_gross`` clips weights in
    ``guards.clamp_book`` and ``margin_cap`` derives venue leverage (D-016), both on total equity, and
    both are inside the construction fingerprint frozen to 2026-10-13.  Moving those denominators
    would resize every position and reset M-010, M-G06 and `realised_vol`; it is a construction
    decision for the operator, and this instrument exists to put a number on it first.
    """
    budget = float(margin_cap) if margin_cap else PLAN_MARGIN_BUDGET
    peak = 0.0
    peak_bar: int | None = None
    standing: list[float] = []
    peak_standing = 0.0
    tradable: list[float] = []
    peak_tradable = 0.0
    peak_tradable_bar: int | None = None
    gross_tradable: list[float] = []
    for row in _cycles(store):
        bar_ms = int(row.get("bar_open_ms") or 0)
        if since_ms is not None and bar_ms < since_ms:
            continue
        equity = row.get("equity")
        held = row.get("margin_usage")
        if isinstance(held, int | float):
            standing.append(float(held))
            peak_standing = max(peak_standing, float(held))
        usdt = (row.get("collateral") or {}).get("usdt_equity")
        if isinstance(held, int | float) and isinstance(usdt, int | float) and float(usdt) > 0 and equity:
            # `margin_usage` is margin/EQUITY and equity is roughly half collateral here, so rescaling
            # by equity/usdt_equity is the same reading against the money that can actually open a
            # position.  Done here rather than in the loop because it is a report, not a decision.
            rescaled = float(held) * float(equity) / float(usdt)
            tradable.append(rescaled)
            if rescaled > peak_tradable:
                peak_tradable, peak_tradable_bar = rescaled, bar_ms
            gross = row.get("gross_before")
            if isinstance(gross, int | float):
                gross_tradable.append(float(gross) / float(usdt))
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
        "budget": budget,
        "plan_budget": PLAN_MARGIN_BUDGET,
        "over_budget": peak_standing > budget if standing else peak > budget,
        # The same reading against the money that can open a position.  `over_budget_tradable` is the
        # one that decides, for the reason in this function's docstring.
        "peak_standing_usage_tradable": peak_tradable if tradable else None,
        "last_standing_usage_tradable": tradable[-1] if tradable else None,
        "peak_tradable_at_bar_ms": peak_tradable_bar,
        "last_gross_over_tradable": gross_tradable[-1] if gross_tradable else None,
        "peak_gross_over_tradable": max(gross_tradable) if gross_tradable else None,
        "over_budget_tradable": (peak_tradable > budget) if tradable else None,
        "rejections": rejections,
        "insufficient_margin": sum(count for code, count in rejections.items() if "-2019" in code),
    }


# The bars `LiveEngine._liquidity` averages for the participation cap: the profile's `pool.liquidity_window`.
# The cycle record does not carry it, so it is written here and a test holds it to the profile.
LIQUIDITY_WINDOW_BARS = 24


def liquidity_to_close(
    store: StateStore, day: str, *, root: str | Path = ".beidou/data", interval: str = "1h"
) -> dict[str, Any]:
    """Checklist 3.9: how long, and at what cost, each held position could be closed.  Reported, never alerted.

    The book is the last traded cycle on or before `day` that planned at all - a guard skip places no
    order, so what it holds is what the cycle before it left.  Cycle rows carry no `positions`.  What
    they carry is `current_notional` on the `skipped` and `orders` entries: the planner's qty x mark for
    each managed symbol it holds.  Rows since 2026-09-14T19:00Z carry it on every band-held entry, and on
    all 232 of them it adds up to the row's own `gross_before` within 0.06% (the two marks are read a
    moment apart); older rows carry it on orders only.  Both sums are returned, so a row that does not
    account for its whole book says so.  The cycle's own fills are then applied at the price the planner
    used, so a position that cycle closed is not reported as one still to close.

    Two rulers, kept apart because they measure different things (the 2026-09-09 VWAP entry in
    `docs/RESEARCH_LOG.md` makes the same point about fills):
      participation  |notional| / the mean quote volume of the last `LIQUIDITY_WINDOW_BARS` bars: an
                     HOUR of volume, `LiveEngine._liquidity`'s quantity, the one `max_participation`
                     is a fraction of;
      impact         `ImpactModel`'s square-root law, coefficient x sigma_daily x sqrt(|notional| / ADV),
                     with ADV a DAY of volume (the 720-bar mean x 24) and sigma_daily the 720-bar std
                     of the open-to-close returns `run_backtest` hands `impact_costs` by default.  The
                     coefficient is the shipped default, 1.0: an assumption, not a calibration - demo
                     fills are too small to separate impact from spread (`config/costs.yaml`, `impact`).

    **A full close is not rate-limited, and that was checked before it was written here.**
    `plan_rebalance` exempts `closing` (target 0 while holding) from the cap whatever
    `exempt_reductions` says, sizes it to the whole position and never applies `max_order_notional` to
    it; with `exempt_crossings` on, as the running construction has it, the absolute band cannot hold it
    back either.  The exit overlay and a symbol leaving the universe both reach the venue that way, and
    `live flatten` does not go through the planner at all.  So a full close is one market order in one
    bar: its cost is impact, not time.  `bars_under_cap`, ceil(participation / max_participation), is
    the other path.  A pure reduction - R8's ladder shrinking every weight - is capped each bar while
    `exempt_reductions` is off, and the column is how many bars the cap would stretch a reduction of
    the whole position over.  It is a floor: each capped order is rounded down to the step, so a real
    walk-down can take a bar more.  Both knobs are read off the record (`construction_full.rebalance`)
    rather than the config, for `max_weight_of`'s reason.

    The kline archive can lag the loop by a bar or more, so each window ends at the newest archived bar
    at or before the cycle's, and how far behind that is gets returned rather than hidden.

    It gates nothing, so it must not take the report down with it (`market_beta`'s rule): whatever it
    raises comes back as the block's `why`, and a symbol whose archive cannot be read is listed with the
    reason while the others are still priced.
    """
    try:
        return _liquidity_to_close(store, day, root=root, interval=interval)
    except Exception as exc:
        return {"enforced": False, "why": f"{type(exc).__name__}: {exc}"}


def _liquidity_to_close(store: StateStore, day: str, *, root: str | Path, interval: str) -> dict[str, Any]:
    cycles = [row for row in _cycles(store) if (_day_of(row) or "") <= day]
    at = next((i for i in range(len(cycles) - 1, -1, -1) if not cycles[i].get("skip")), None)
    if at is None:
        return {"enforced": False, "why": f"no traded cycle on or before {day}"}
    row = cycles[at]
    rebalance: Mapping[str, Any] = {}
    for earlier in reversed(cycles[: at + 1]):
        full = earlier.get("construction_full")
        if isinstance(full, Mapping) and isinstance(full.get("rebalance"), Mapping):
            rebalance = full["rebalance"]
            break
    raw_cap, raw_exempt = rebalance.get("max_participation"), rebalance.get("exempt_reductions")
    cap = float(raw_cap) if isinstance(raw_cap, int | float) else None
    before: dict[str, float] = {}
    for entry in [*(row.get("skipped") or []), *(row.get("orders") or [])]:
        if isinstance(entry.get("current_notional"), int | float):
            before[str(entry.get("symbol"))] = float(entry["current_notional"])
    held = dict(before)
    for order in row.get("orders") or []:
        try:
            filled = float(order.get("executed_qty") or 0.0) * float(order.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        symbol = str(order.get("symbol"))
        held[symbol] = held.get(symbol, 0.0) + (filled if str(order.get("side")) == "BUY" else -filled)
    # Flat after the cycle: a fill that took the position to zero leaves float dust, not a position.
    book = {symbol: value for symbol, value in held.items() if abs(value) > 1e-6 * abs(before.get(symbol, 0.0))}
    step = interval_ms(interval)
    decision_ms = int(row.get("as_of_ms") or row.get("bar_open_ms") or 0)
    impact = ImpactModel()
    klines = KlineStore(root)
    rows: list[dict[str, Any]] = []
    unpriced: dict[str, str] = {}
    for symbol, notional in sorted(book.items()):
        try:
            frame = klines.load(symbol, interval, end_ms=decision_ms + step)
        except FileNotFoundError:
            unpriced[symbol] = f"no {interval} archive"
            continue
        except Exception as exc:  # a half-written parquet raises ArrowInvalid: that symbol's problem only
            unpriced[symbol] = f"archive unreadable: {type(exc).__name__}: {exc}"
            continue
        if frame.empty:
            unpriced[symbol] = "no archived bar at or before this cycle"
            continue
        rows.append(_closing_cost(symbol, notional, frame, cap, impact, bars_per_day=DAY_MS // step))
    rows.sort(key=lambda entry: (entry["participation"] is None, -(entry["participation"] or 0.0)))
    priced = [entry for entry in rows if entry["impact_u"] is not None]
    priced_gross = sum(abs(entry["notional_u"]) for entry in priced)
    total = sum(entry["impact_u"] for entry in priced)
    through = min((entry["volume_through_ms"] for entry in rows), default=None)
    accounted = sum(abs(value) for value in before.values())
    gross_before = row.get("gross_before")
    return {
        "enforced": True,
        "bar": row.get("bar"),
        "decision_ms": decision_ms,
        "equity_u": row.get("equity"),
        "gross_before_u": gross_before,
        "accounted_before_u": accounted,
        # False on a row that names positions without their notional, the shape rows had before
        # 2026-09-14T19:00Z: the reading covers part of the book, and "0 positions" means "not recorded".
        "book_complete": (
            abs(accounted - float(gross_before)) <= 0.01 * float(gross_before)
            if isinstance(gross_before, int | float)
            else None
        ),
        "positions": len(book),
        "gross_u": sum(abs(value) for value in book.values()),
        "max_participation": cap,
        "exempt_reductions": raw_exempt if isinstance(raw_exempt, bool) else None,
        "liquidity_window_bars": LIQUIDITY_WINDOW_BARS,
        "impact_model": {
            "coefficient": impact.coefficient,
            "adv_window_bars": impact.adv_window,
            "vol_window_bars": impact.vol_window,
        },
        # None, not 0.0, when no position could be priced: a zero would read as "closing costs nothing".
        "impact_u": total if priced else None,
        "impact_bps": 1e4 * total / priced_gross if priced_gross else None,
        "slowest_bars_under_cap": max(
            (entry["bars_under_cap"] for entry in rows if entry["bars_under_cap"] is not None), default=None
        ),
        "hardest": [entry["symbol"] for entry in rows if entry["participation"] is not None][:3],
        "volume_through_ms": through,
        "volume_lag_bars": (decision_ms - through) // step if through is not None else None,
        "rows": rows,
        "unpriced": unpriced,
    }


def _closing_cost(
    symbol: str,
    notional: float,
    frame: pd.DataFrame,
    max_participation: float | None,
    impact: ImpactModel,
    *,
    bars_per_day: int,
) -> dict[str, Any]:
    """One position against its own bars, each ruler computed the way its owner computes it.

    The hourly mean is `LiveEngine._liquidity` line for line, fallback included (volume x close where
    `quote_volume` is missing), and a mean that is not positive is None: the loop applies no cap then.
    ADV and sigma are `impact_costs`' - its ADV takes whatever bars exist, its sigma needs a quarter of
    the window - and an unreadable one is None, never the 0.0 the backtest fills it with.
    """
    quote = (
        frame["quote_volume"].astype(float)
        if "quote_volume" in frame.columns
        else frame["volume"].astype(float) * frame["close"].astype(float)
    )
    hourly = float(quote.tail(LIQUIDITY_WINDOW_BARS).mean())
    adv = float(quote.tail(impact.adv_window).mean()) * bars_per_day
    returns = (frame["close"].astype(float) / frame["open"].astype(float) - 1.0).tail(impact.vol_window)
    sigma = float(returns.std()) * math.sqrt(bars_per_day) if returns.count() >= impact.vol_window // 4 else None
    size = abs(notional)
    participation = size / hourly if hourly > 0 else None
    fraction = impact.coefficient * sigma * math.sqrt(size / adv) if sigma is not None and adv > 0 else None
    return {
        "symbol": symbol,
        "notional_u": notional,
        "hourly_quote_volume_u": hourly if hourly > 0 else None,
        "participation": participation,
        # ceil, with a hair of slack so a reduction of exactly k caps is k bars and not k + 1
        "bars_under_cap": (
            max(1, math.ceil(participation / max_participation - 1e-9))
            if participation is not None and max_participation
            else None
        ),
        "adv_u": adv if adv > 0 else None,
        "sigma_daily": sigma,
        "impact_bps": 1e4 * fraction if fraction is not None else None,
        "impact_u": size * fraction if fraction is not None else None,
        "volume_through_ms": int(frame["open_time"].iloc[-1]),
    }


def _liquidity_to_close_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """3.9 in the daily report: the book, how a close goes out, the model, then positions hardest first."""
    if not block.get("enforced"):
        return {"status": "n/a", "why": block.get("why") or "no reading"}
    cap, window = block.get("max_participation"), block.get("liquidity_window_bars")
    if cap is None:
        reductions = "读不出：没有周期记下 max_participation。"
    elif cap <= 0:
        reductions = "循环没有参与率上限，纯减仓也是一根 bar。"
    elif block.get("exempt_reductions"):
        reductions = "exempt_reductions 开着，纯减仓也不受限。限速那一列不描述任何平仓路径。"
    else:
        reductions = (
            f"每单不超过近 {window} 根 bar 平均小时成交额的 {100 * cap:g}%。"
            "限速那一列只对纯减仓成立，不对完全平仓成立。"
        )
    model = block.get("impact_model") or {}
    rows = block.get("rows") or []
    gross_before = block.get("gross_before_u")
    partial = block.get("book_complete") is False
    # What an empty table means depends on whether the row accounted for its book: a flat record, or one
    # that named its positions without their notional.
    empty = "读不出：这一行没有按币记下名义额" if partial else "这一行没有记下持仓"
    lines: dict[str, Any] = {
        "持仓（本周期成交后）": f"{block['positions']} 个，gross {block['gross_u']:,.2f} U；bar {block.get('bar')}",
        "按币合计 / gross_before（成交前）": (
            f"{block['accounted_before_u']:,.2f} / "
            + (f"{gross_before:,.2f} U" if isinstance(gross_before, int | float) else "n/a")
        ),
    }
    if partial and isinstance(gross_before, int | float):  # `book_complete` is only False beside a number
        gap = abs(block["accounted_before_u"] - float(gross_before))
        lines["按币记录不全"] = f"按币合计与 gross_before 差 {gap:,.2f} U，超过 1%。下面只覆盖按币记下的部分。"
    lines |= {
        "完全平仓": "一笔市价单，一根 bar 内发完。参与率上限豁免完全平仓。代价在冲击上，不在时间上。",
        "纯减仓限速": reductions,
        "冲击模型（平方根律）": (
            f"c·σ_d·√(名义额/ADV_d)。c={model.get('coefficient')}，"
            f"ADV 取 {model.get('adv_window_bars')} 根 bar，σ_d 取 {model.get('vol_window_bars')} 根。"
        ),
        "c 没有校准": "c 是假设。demo 成交太小，冲击与价差分不开。见 config/costs.yaml 的 impact 段。",
        "整本书一次完全平仓": (
            f"冲击 {_fmt_num(block.get('impact_u'))} U，名义加权 {_fmt_num(block.get('impact_bps'))} bps；"
            f"纯减仓限速下最慢 {block.get('slowest_bars_under_cap') or 'n/a'} 根 bar"
        ),
        "最难平的三个（按完全平仓的参与率）": "、".join(
            f"{entry['symbol']} {entry['participation']:.4%}" for entry in rows if entry["symbol"] in block["hardest"]
        )
        or (empty if not block["positions"] else "读不出，见下面各仓"),
    }
    for entry in rows:
        pace = (
            f"参与率 {entry['participation']:.4%}（小时均量 {entry['hourly_quote_volume_u'] / 1e6:,.2f}M U）；"
            f"纯减仓 {entry['bars_under_cap'] or 'n/a'} bar"
            if entry.get("participation") is not None
            else "参与率读不出（窗口里没有成交额）"
        )
        cost = (
            f"冲击 {entry['impact_bps']:.2f} bps = {entry['impact_u']:.2f} U"
            f"（σ_d {entry['sigma_daily']:.2%}，ADV {entry['adv_u'] / 1e6:,.1f}M U）"
            if entry.get("impact_bps") is not None
            else "冲击读不出（σ_d 的 bar 不够，或没有成交额）"
        )
        lines[str(entry["symbol"])] = f"{entry['notional_u']:+,.2f} U；{pace}；{cost}"
    for symbol, why in (block.get("unpriced") or {}).items():
        lines[str(symbol)] = f"读不出：{why}"
    through, lag = block.get("volume_through_ms"), block.get("volume_lag_bars")
    lines["成交额截至"] = (
        (empty if not block["positions"] else "没有读到成交额")
        if through is None
        else f"归档最后一根 {pd.Timestamp(through, unit='ms', tz='UTC').isoformat()}"
        + (f"，比决策 bar 早 {lag} 根" if lag else "，就是决策 bar")
    )
    return lines


BACKTEST_EXITS_PER_WEEK = 562.0 / 49_735.0 * 24.0 * 7.0  # P11: 544 take-profits + 18 stops over 49,735 hourly bars


def noise_scale(store: StateStore, day: str, *, vol_target: float | None) -> dict[str, Any]:
    """DL-EX0: the size of a normal day, so a giveback can be read as a multiple of it instead of as a feeling.

    ``design`` is vol_target / sqrt(365) of the day's last equity; ``realised`` is the std of hourly equity
    changes over the trailing 30 days of traded cycles (bars re-baselined by an external transfer are
    skipped), scaled by sqrt(24); ``peak_giveback`` is the largest drop from the running equity high inside
    the day.  The expected exit count pro-rates the P11 backtest rate for the whole book to the cycles seen
    so far in the current construction, and ``exits_so_far`` counts the exits that actually happened over
    that same evidence window - not over ``realised``'s 30 days, which is a different span, and not counting
    COOLDOWN, which records a cycle an earlier exit blocked rather than an exit of its own.

    DL-GB0: ``peak_giveback`` resets at midnight and the giveback an operator sees does not.  On 2026-09-15
    the day-inside ruler read 145.2 U / 0.43 sigma while the number being asked about was the 430 U back
    from the 09-14T21:00Z high - 1.28 design daily sigma.  So ``giveback_since_hwm_u`` measures from the
    high-water mark the LOOP wrote (``throttle.equity_hwm``, the one D-015's throttle acts on) and not from
    a high this function recomputes off the equity path: the instrument reads the loop's reading.
    ``giveback_since_hwm_hours`` dates that mark to the EARLIEST cycle row still carrying it, which is when
    the mark became visible and not when it happened - the 2026-09-14 high was set inside the 20:24Z restart
    gap, whose two cycles wrote heartbeat rows with no equity at all, so the first row to carry it is the
    21:00Z bar and the true age can be a bar older.  ``giveback_since_hwm_in_horizon_sigma`` divides by the
    design sigma scaled to those hours, because a fall of ten hours measured against a 24-hour sigma reads
    small by construction: the same 430 U is 1.28 sigma_d and 1.98 sigma_10h.

    The last three keys are a reconciliation, not new measurements.  One page carried three drawdowns and
    the operator read the largest: ``drawdown_vs_hwm_pct`` is equity against the loop's own high (it
    reproduces ``throttle.drawdown`` exactly, which is the check), ``ladder_drawdown_pct`` is R8's ruler -
    attributed P&L against the ladder's own anchor, a different book on a different window - and
    ``giveback_in_design_sigma`` above is the day-inside one.  The two percentages point the same way with
    OPPOSITE signs: the ladder's is copied as the loop writes it, negative, because changing a sign to make
    a table tidy would make the field stop matching the record it came from.

    A-GB01, answered 2026-09-15: the percentage the operator reads is the venue's USDT equity.  Checked
    against the account - USDT equity 4,932.05 at 09-13T22:00Z to 5,311.89 at 09-14T19:00Z is +7.70%, the
    "涨了 8 个点" the equity line scored as +3.78% - so the last three keys CONVERT numbers already computed
    above into that reader's unit.  They are conversions, not measurements.  Both numerators stay on total
    equity: ``giveback_since_hwm_u`` is measured from ``throttle.equity_hwm`` and stays that way, because a
    high-water mark recomputed on the USDT series would be a FOURTH ruler on a page whose problem is that it
    already carries three.  Only the denominator moves, to ``collateral.usdt_equity`` off the day's last
    cycle.  That denominator is about 47% of equity (``collateral.share`` 0.5256 on 2026-09-15, the rest BTC
    and other collateral), so the same money reads about 2.1x larger as a share of USDT equity than as a
    share of equity - which is the whole of "8 points against 3.8 points".  Two adjacent lines on the page
    are NOT in that ratio and the difference is not a bug: ``drawdown_vs_hwm_pct`` divides by the HIGH-WATER
    MARK, so it sits against ``giveback_since_hwm_in_usdt_pct`` at hwm/usdt_equity - 2.18 on 2026-09-15,
    against 2.11 for the collateral share alone.  Same numerator, three denominators, all of them named.

    Worth stating plainly, because the cheap reading is that the operator is simply on the wrong ruler: USDT
    equity is CLOSER to the book's own P&L than total equity is.  Trades settle in USDT, while collateral
    repricing moves total equity without touching it.  Measured on this event: low to peak was +400.80
    equity against +379.84 USDT, and the 20.96 difference is BTC being remarked; peak to now is -207.35
    against -187.41, difference -19.94.  ``## Collateral repricing (RISK-G11)`` is the section that measures
    exactly that, and it has reported repricing at 41% of an equity move.  So this ruler is not wrong - it
    has a smaller denominator and it leaves collateral noise out.  It is also not the book: deposits,
    withdrawals, commissions and funding all change USDT equity directly, and the book-level ruler is still
    ``risk_ladder.drawdown`` (attributed P&L plus unrealized).  Three instruments, three jobs, none of them
    a substitute for another.  ROE stays out on purpose: it is a fourth percentage the operator does not
    read, and printing it would re-open the question these keys close.
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

    def stamp(row: Mapping[str, Any]) -> int:
        """`_day_of`'s preference order in milliseconds: the data's clock before the host's (D-025)."""
        return int(row.get("as_of_ms") or row.get("bar_open_ms") or 0)

    last_row = today[-1] if today else None
    recorded_hwm = (last_row.get("throttle") or {}).get("equity_hwm") if last_row else None
    hwm = float(recorded_hwm) if recorded_hwm is not None else None
    since_hwm = max(0.0, hwm - last) if (hwm is not None and last is not None) else None
    # A-GB01's denominator, off the SAME row `hwm` and `last` came from, so the three readings share a
    # cycle.  Rows written before the engine recorded the split carry no `collateral` at all, and a
    # missing USDT balance reads as absent rather than as zero - `collateral_share`'s own rule.
    recorded_usdt = (last_row.get("collateral") or {}).get("usdt_equity") if last_row else None
    usdt = float(recorded_usdt) if recorded_usdt is not None else None
    # Walk back from the day's last cycle while the loop kept writing the same mark, and stop at the row
    # that carried a lower one.  Bounded by that cycle rather than by the end of `trailing`, so asking for
    # a past `--date` cannot date the mark from rows written after the day being reported on.
    anchor = stamp(last_row) if last_row else None
    oldest_at_hwm = None
    for row in reversed([row for row in trailing if anchor and stamp(row) <= anchor]):
        mark = (row.get("throttle") or {}).get("equity_hwm")
        if hwm is None or mark is None or float(mark) != hwm:
            break
        oldest_at_hwm = row
    hours = (anchor - stamp(oldest_at_hwm)) / (DAY_MS / 24.0) if (oldest_at_hwm and anchor) else None
    horizon = design * math.sqrt(hours / 24.0) if (design and hours and hours > 0) else None
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
        "giveback_since_hwm_u": since_hwm,
        "giveback_since_hwm_in_design_sigma": (since_hwm / design) if (since_hwm is not None and design) else None,
        "giveback_since_hwm_hours": hours,
        "giveback_since_hwm_in_horizon_sigma": (since_hwm / horizon) if (since_hwm is not None and horizon) else None,
        "equity_hwm_u": hwm,
        "drawdown_vs_hwm_pct": (since_hwm / hwm) if (since_hwm is not None and hwm) else None,
        "ladder_drawdown_pct": (last_row.get("risk_ladder") or {}).get("drawdown") if last_row else None,
        # A-GB01: the two numbers above, over the denominator the operator's screen divides by.  `usdt`
        # is truth-tested rather than compared to None because a zero USDT balance divides no better
        # than a missing one.
        "usdt_equity_u": usdt,
        "design_daily_sigma_in_usdt_pct": (design / usdt) if (design and usdt) else None,
        "giveback_since_hwm_in_usdt_pct": (since_hwm / usdt) if (since_hwm is not None and usdt) else None,
    }


# G4, 2026-09-23: the backtest's one-day tail on the construction that trades - registry book (tsmom main +
# flow_short sleeve), exits, both book guards, the live band (D2 + D3) and R8's ladder at vol_target 0.60, pit
# universe, every complete UTC day 2021-02-01 -> 2026-09-21 (n = 2,059), from
# `scratchpad/g4_stress_windows_and_var_at_k060.py`.  Historical: VaR is the m-th worst day and ES the mean of
# the m worst, m = ceil(n x (1 - level)) = 103 / 21.  Fractions of equity, positive = a loss.  Measured at ONE
# vol_target, so `tail_readings` refuses to convert them under another.
TAIL_VOL_TARGET = 0.60


BACKTEST_DAILY_VAR = {0.95: 0.043210, 0.99: 0.078255}


BACKTEST_DAILY_ES = {0.95: 0.063345, 0.99: 0.092512}


def tail_readings(store: StateStore, day: str, *, vol_target: float | None) -> dict[str, Any]:
    """G4: the backtest's one-day VaR / ES in USDT at the day's last equity, and live days past the VaR.

    Beside DL-EX0 because sigma sizes a normal day, and read as a normal distribution it misplaces the bad
    ones.  The same replay's daily sd is 3.17% (design 3.14%), but its 99% day sits at 2.47 sd with ES 2.92
    sd, where a normal day would put them at 2.33 and 2.67; its 95% day is 1.36 sd against 1.645.  The
    conversion multiplies by the equity `design_daily_sigma_u` scales, because the weights are fractions of it.

    The live day is `noise_scale`'s series compounded: total equity cycle to cycle, a step whose later
    cycle re-baselined on a transfer skipped, bucketed by `_day_of` of the later cycle.  Measured over the
    nine complete k=0.60 days of the live record (09-14 -> 09-22): it differs from the book's own
    mark-to-market (attributed P&L + change in `unrealized`) by 0.42pp a day at most, and the two agree
    on every day's side of both VaRs.  USDT equity does not: its daily move reads 1.90x (median), and on
    that record it put one day past VaR 95% that neither of the others did.

    Only complete UTC days count, after the day the D-026 window opened and before `day`.  So a count
    never mixes two books, and an hourly run never scores a day that is still open.
    """
    rows = _cycles(store)
    today = [row for row in rows if _day_of(row) == day]
    equity = float(today[-1]["equity"]) if today else None
    if vol_target is None or not math.isclose(vol_target, TAIL_VOL_TARGET):
        why = f"常量在 vol_target {TAIL_VOL_TARGET} 下量得，profile 是 {vol_target}：不换算，不计数"
        return {"enforced": False, "why": why, "equity_u": equity}
    since = int(evidence_window(store).get("since_ms") or 0)
    inside = [row for row in rows if int(row.get("bar_open_ms") or 0) >= since]
    opened = _day_of(inside[0]) if inside else None
    growth: dict[str, float] = {}
    for a, b in pairwise(inside):
        if (b.get("external_flows") or {}).get("rebaselined") or float(a["equity"]) <= 0:
            continue
        key = str(_day_of(b))
        growth[key] = growth.get(key, 1.0) * float(b["equity"]) / float(a["equity"])
    days = {key: value - 1.0 for key, value in growth.items() if opened is not None and opened < key < day}
    out: dict[str, Any] = {
        "enforced": True,
        "equity_u": equity,
        "days": len(days),
        "first_day": min(days, default=None),
    }
    for level in (0.95, 0.99):
        var, es = BACKTEST_DAILY_VAR[level], BACKTEST_DAILY_ES[level]
        tag = f"{round(level * 100)}"
        out[f"var_{tag}"], out[f"es_{tag}"] = var, es
        out[f"var_{tag}_u"] = var * equity if equity is not None else None
        out[f"es_{tag}_u"] = es * equity if equity is not None else None
        out[f"past_var_{tag}"] = sorted(key for key, value in days.items() if value < -var)
        out[f"expected_past_var_{tag}"] = len(days) * (1.0 - level)
    return out


def _tradable_drawdown_line(block: Mapping[str, Any]) -> str:
    """`usdt_drawdown_state` in one line: today, the worst ever, and where the rungs land in this unit.

    The rung conversion is on the page rather than in a footnote because it is the part that cannot be
    guessed from the percentage: at 1.86x the shipped `rollback_at` converts to above 100%, i.e. the
    tradable money would have to go past zero.  That is not a defect in the ladder - it is calibrated on
    the whole capital, under cross margin, where collateral does absorb losses - but it is the number the
    2026-09-20 ruling needs, and no instrument printed it.
    """
    if not block.get("enforced"):
        return f"not enforced ({block.get('why', 'no reason recorded')})"
    rungs = block.get("rungs_in_this_denominator") or {}
    tail = (
        f"；同一梯在这个分母下 = de-escalate {rungs['deescalate_at']:.0%} / rollback {rungs['rollback_at']:.0%}"
        if rungs
        else ""
    )
    return (
        f"当前 {block['value']:.2%}，历史最大 {block['max_drawdown']:.2%}"
        f"（{block.get('deepest_at') or 'n/a'}）；"
        f"HWM {block.get('peak', 0.0):.2f} U @ {block.get('peak_at') or 'n/a'}"
        f"，同一笔亏损在这里读数是总权益口径的 {block.get('vs_total_equity') or float('nan'):.2f} 倍{tail}"
    )


def _risk_budget_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """One readable line per metric; a metric that could not be computed says why instead of showing 0."""
    if not block:
        return {"none": 0}
    drawdown = block.get("drawdown") or {}
    tradable = block.get("usdt_drawdown") or {}
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
        # Immediately under it, because the line above is the SAME account on a denominator half of
        # which cannot trade, and a reader who sees only one of the two has the wrong number either way.
        # Current first: "回撤是多少" was asked on 2026-09-20 and answered with the line above, which is
        # the deepest reading ever and not today's.
        "drawdown (可动用 USDT)": _tradable_drawdown_line(tradable),
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


def _weight_cap_line(cap: Mapping[str, Any]) -> str:
    """D-046 on one line.  An undercount must not render like a count, so the bound says it is one."""
    if not cap.get("readable"):
        return f"读不出（{cap.get('why', '无读数')}）"
    names = "、".join(f"{symbol} {count}" for symbol, count in (cap.get("by_symbol") or {}).items()) or "无"
    line = (
        f"max_weight {cap['max_weight']}：{cap['bound_cycles']}/{cap['cycles']} 个周期"
        f"（{cap['share']:.1%}）截断了至少一个名字；{names}"
    )
    if cap.get("gross_capped_bars"):
        line += (
            f"。**这是下界**：期间 {cap['gross_capped_bars']} 根 bar 触到 gross 上限，"
            "那些 bar 上被单币上限截过的名字已被整行缩放，这里看不见"
        )
    return line


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
        # D-046: the first of this block's four listed causes that is measured rather than named.
        "单币上限绑定": _weight_cap_line(block.get("weight_cap") or {}),
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
    leverage = dict(readable_state(store)[0].leverage_set)
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
    # Narrowed on the way out of the record rather than trusted into the comparison below.  Every
    # number in `judged` was computed by `spread` from a JSON cycle row, so its static type carries
    # the `str` the file could hold; an unreadable one used to reach `compression > limit` and raise
    # TypeError from inside a report whose whole job is to answer.  D-035's rule is the other one:
    # a reading that cannot be computed says so and is never read as a number.
    raw_compression = judged.get("compression")
    if raw_compression is not None and not isinstance(raw_compression, int | float):
        return {**refused, "reason": f"compression is not a number: {raw_compression!r}", "rows": rows}
    compression: float | None = None if raw_compression is None else float(raw_compression)
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
        # D-046: one of the four honest causes this docstring lists, turned from a possibility into a
        # reading.  See `weight_cap_bindings`.
        "weight_cap": weight_cap_bindings(cycles, max_weight_of(store)),
        "rows": rows,
    }


def max_weight_of(store: StateStore) -> float | None:
    """The per-symbol cap the loop is running, off the newest construction it recorded.

    `construction_full` carries it under `guards`, and `beidou_live.config` builds both
    `GuardParams.max_weight` and `PortfolioParams.max_weight` from the one `portfolio.max_weight`
    key, so the guard copy IS the construction copy - there is no second number to get wrong.

    Read from the record rather than from the config file on purpose: the engine builds its model
    once at startup, so a `max_weight` edited on disk is not the cap the running loop is applying
    (KILL-Q15's shape).  ``None`` when no cycle carries a construction, which is the honest answer
    for a record written before the payload existed.
    """
    latest: float | None = None
    for row in store.read_jsonl(store.cycles_path):
        full = row.get("construction_full")
        if not isinstance(full, Mapping):
            continue
        value = (full.get("guards") or {}).get("max_weight")
        if isinstance(value, int | float) and float(value) > 0:
            latest = float(value)
    return latest


def weight_cap_bindings(cycles: Sequence[Mapping[str, Any]], max_weight: float | None) -> dict[str, Any]:
    """How often stage 3's per-symbol cap truncated a name, and which names (D-046).

    `risk_adaptation`'s docstring lists ``max_weight`` binding on the calmest names as one of the
    reasons compression can rise with stage 1 untouched.  It was a possibility, not a reading - and
    it stopped being hypothetical when ``vol_target`` went to 0.60, because a cap that never bound at
    0.15 starts binding when every weight is four times larger.  Nothing had to be recorded to
    measure it: ``book_weights`` has been in the cycle row since 2026-09-12 (it was put there for the
    probe's mark-to-market), and a name the cap truncated sits at exactly ``max_weight``.

    **The reading is a LOWER bound, and the bound has a name.**  Stage 3 clips per symbol and then
    scales the whole row if gross exceeds ``max_gross``, so on a gross-capped bar a truncated name
    lands at ``max_weight * factor`` and this stops seeing it.  ``gross_capped_bars`` is therefore
    reported beside the count rather than assumed away: while it is 0 the reading is exact, and the
    moment it is not, the share below is an undercount.  Measured over the whole record on
    2026-09-17: 0 of 376 cycles have ever carried ``GROSS_CAPPED``, so today it is exact.

    What it found, and why the config comment beside ``vol_target`` needed it: 49 of the 95 cycles
    carrying ``book_weights`` truncate at least one name, and the names are **BTCUSDT (49) and
    BNBUSDT (27)** - not BTCUSDT alone, which is what that comment had recorded from a narrower
    sample.  Both are what inverse-vol sizing does with the calmest names in the book.
    """
    usable = [row for row in cycles if (row.get("book_weights") or {}).get(MAIN_BOOK)]
    gross_capped = sum(1 for row in cycles if "GROSS_CAPPED" in (row.get("guard_reasons") or []))
    if max_weight is None:
        return {"readable": False, "why": "no cycle records a construction, so the cap is unknown"}
    if not usable:
        return {"readable": False, "why": "no cycle in this window records `book_weights`"}
    # Floating point: a clipped weight is `max_weight` to the last bit in the row that produced it,
    # but it has been through JSON, so this asks "at the cap" rather than "equal to it".
    edge = max_weight * (1.0 - 1e-9)
    by_symbol: dict[str, int] = {}
    bound = 0
    for row in usable:
        names = [s for s, w in (row["book_weights"][MAIN_BOOK]).items() if abs(float(w)) >= edge]
        bound += 1 if names else 0
        for symbol in names:
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
    return {
        "readable": True,
        "max_weight": max_weight,
        "cycles": len(usable),
        "bound_cycles": bound,
        "share": bound / len(usable),
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda item: -item[1])),
        # 0 means the share above is exact rather than a lower bound; see the docstring.
        "gross_capped_bars": gross_capped,
    }


def latest_risk_adaptation(store: StateStore) -> dict[str, Any]:
    """M-015 for the day the newest recorded cycle belongs to, for readers that have no day in hand.

    `live status` is one: it answers "what is the loop doing right now", and asking it to name a UTC day
    would make it answer for a day that may have no cycles yet - at 00:30Z, every day.  The day comes
    from `_day_of`, the same ruler `risk_adaptation` buckets by, rather than from the host clock.
    """
    day = next((found for found in map(_day_of, reversed(store.read_jsonl(store.cycles_path))) if found), None)
    if day is None:
        leverage = dict(readable_state(store)[0].leverage_set)
        return {
            "enforced": False,
            "rows": [],
            "reason": "no cycles recorded",
            "leverage_distinct": len(set(leverage.values())),
        }
    return risk_adaptation(store, day)


def risk_adaptation_headline(block: Mapping[str, Any]) -> str:
    """M-015 in one line, for the command that shows `leverage_set` (D-038 again, 2026-09-14).

    D-038 built M-015 because the daily report showed `last_targets` with nothing to read them against,
    so the only per-symbol number anywhere was the venue's uniform 5x.  It fixed the report, and the
    question came back a fifth time - because `live status`, the command an operator actually reaches
    for, dumps `state.to_dict()` and has the same hole in it.

    Two halves, and only the first can refuse.  A reading needs three held names and a cycle written
    after `asset_vol` was recorded; the sentence about the venue leverage needs neither, and it is the
    half the operator came here for, so it is carried on every path - the same rule `risk_adaptation`
    applies to `leverage_distinct`.  The share is `1 - compression`, the ruler `_risk_adaptation_lines`
    already uses: a second ruler for the same quantity is how the two disagree six months from now.
    """
    distinct = block.get("leverage_distinct")
    venue = f"交易所杠杆 {distinct} 个取值，在这本书里不承担风险（D-037）" if distinct else "交易所杠杆尚未设置"
    compression = block.get("compression")
    if not block.get("enforced") or not isinstance(compression, int | float):
        # The English `reason` is not pasted here.  This line lands in `live status`, whose prose is the
        # body of the hourly alert, and that channel is Chinese by the 2026-09-08 ruling; the reason is
        # one command away in a report where English is the house language.
        return f"杠杆自适应（M-015）：本轮读不出（原因见日报的 M-015 段）；{venue}"
    limit = float(block.get("limit", RISK_COMPRESSION_LIMIT))
    reading = "单书" if block.get("judged") == "single_book" else "合并"
    verdict = "当前告警" if block.get("status") == "ALERT" else "当前正常"
    return (
        f"杠杆自适应（M-015）：第一层定价吸收了市场波动离散度的 {1.0 - float(compression):.0%}"
        f"（压缩度 {float(compression):.2f}，{reading}读法，超 {limit:.2f} 告警；{verdict}）；{venue}"
    )


def _noise_scale_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """DL-EX0's day-inside ruler, then DL-GB0's cross-day one, then the three drawdowns side by side.

    The order is the argument.  Two labels carry "(today)" because the day-inside reading is the one that
    silently disagreed with the account, `hours` is printed so the horizon sigma can be checked by hand, and
    the two percentages are adjacent so the page answers "which drawdown is the drawdown" instead of leaving
    a reader to pick the worst of three.  What each number measures is in `noise_scale`'s docstring.

    The last three lines are A-GB01's conversion and are placed last for that reason: the rulers come
    first, then the same fall restated in the unit the operator's screen uses.  The denominator is printed
    rather than left implicit so each percentage can be divided back by hand - which is the only way to see
    that `giveback_since_hwm_in_usdt_pct` and `drawdown_vs_hwm_pct (equity)` share a numerator and differ
    by hwm/usdt_equity, not by the collateral share.  No ROE line - `noise_scale`'s docstring says why.

    2026-09-20: the operator ruled that the drawdown they read is the one the TRADABLE money suffered -
    numerator and denominator both - which is the fourth ruler `noise_scale` deliberately refused to
    build.  It now exists, in `risk_budget.usdt_drawdown_state`, and this block did not change except in
    one label: `giveback_since_hwm_in_usdt_pct` says out loud that it mixes two series, and points at the
    reading that does not.  Not recomputed here, because a conversion that stops being read as a
    measurement is doing its job; the failure was never the arithmetic, it was the name.
    """
    return {
        "design_daily_sigma_u": _fmt_num(block.get("design_daily_sigma_u")),
        "realised_daily_sigma_u": _fmt_num(block.get("realised_daily_sigma_u")),
        "peak_giveback_u (today, UTC)": _fmt_num(block.get("peak_giveback_u")),
        "giveback_in_design_sigma (today)": _fmt_num(block.get("giveback_in_design_sigma")),
        "equity_hwm_u": _fmt_num(block.get("equity_hwm_u")),
        "giveback_since_hwm_u": _fmt_num(block.get("giveback_since_hwm_u")),
        "giveback_since_hwm_hours": _fmt_num(block.get("giveback_since_hwm_hours")),
        "giveback_since_hwm_in_design_sigma": _fmt_num(block.get("giveback_since_hwm_in_design_sigma")),
        "giveback_since_hwm_in_horizon_sigma": _fmt_num(block.get("giveback_since_hwm_in_horizon_sigma")),
        "drawdown_vs_hwm_pct (equity)": _fmt_pct(block.get("drawdown_vs_hwm_pct")),
        "ladder_drawdown_pct (R8, attributed)": _fmt_pct(block.get("ladder_drawdown_pct")),
        "usdt_equity_u (A-GB01 denominator)": _fmt_num(block.get("usdt_equity_u")),
        "design_daily_sigma_in_usdt_pct": _fmt_pct(block.get("design_daily_sigma_in_usdt_pct")),
        # Renamed 2026-09-20, not recomputed: the number is a conversion and stays one, but its old
        # label let it be read as "the drawdown of my USDT" - which it is not, and which the P13 block
        # now prints properly.  Numerator from the total-equity high, denominator from USDT, and on
        # 2026-09-20 those two highs were 13 hours apart.
        "giveback_since_hwm_in_usdt_pct (混口径，非 USDT 自己的回撤)": _fmt_pct(
            block.get("giveback_since_hwm_in_usdt_pct")
        ),
        "→ USDT 自己的回撤见 Risk budget (P13) 的 drawdown (可动用 USDT)": "",
        "expected_exits_so_far": _fmt_num(block.get("expected_exits_so_far")),
        "exits_so_far": block.get("exits_so_far"),
    }


def _tail_readings_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """G4's two readings under the sigma ruler: the backtest tail in USDT, then live days past it."""
    if not block.get("enforced"):
        return {"status": "n/a", "why": block.get("why") or "no reading"}
    lines: dict[str, Any] = {}
    for tag in ("95", "99"):
        lines[f"VaR {tag}% / ES {tag}% (1 day, backtest k={TAIL_VOL_TARGET:.2f})"] = (
            f"{_fmt_pct(block.get(f'var_{tag}'))} / {_fmt_pct(block.get(f'es_{tag}'))} of equity = "
            f"{_fmt_num(block.get(f'var_{tag}_u'))} / {_fmt_num(block.get(f'es_{tag}_u'))} U"
        )
    for tag in ("95", "99"):
        past = block.get(f"past_var_{tag}") or []
        lines[f"live days below -VaR {tag}%"] = (
            f"{len(past)} of {block.get('days')} (expected {_fmt_num(block.get(f'expected_past_var_{tag}'))})"
            + (f" {json_dumps(past)}" if past else "")
        )
    lines["live day"] = (
        f"total equity, transfer steps skipped (noise_scale's); complete UTC days from "
        f"{block.get('first_day') or '(none yet)'}, inside the D-026 window, before this report's day"
    )
    return lines
