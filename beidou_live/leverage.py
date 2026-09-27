"""Exchange leverage derivation and margin-aware order scaling (D-016; legacy lesson E-035: one risk budget).

Exposure is decided by the portfolio layer (vol target, max_gross, max_weight).
The exchange leverage setting only decides how much *margin* that exposure
consumes: at leverage L and gross G the initial margin is G / L of equity.
``derive_leverage`` picks the smallest integer leverage that keeps margin use
at the gross cap below ``margin_cap``, bounded by the venue bracket and a hard
``max_leverage``.  ``scale_orders_to_margin`` shrinks risk-adding orders
pro-rata when the available balance cannot fund them, instead of letting the
venue reject them one by one (-2019).

`by_vol` (2026-09-27) sets a leverage per symbol instead of one for all.  It is option R1 of
`docs/analysis/2026-09-26-per-symbol-leverage-first-principles.md`; the code calls it by its mode
because governance already has an R1, the trials budget.  ``tier_leverage`` puts each symbol on a
ladder by its vol, ``hysteresis_step`` holds it there against dithering, ``bracket_leverage`` keeps
it inside what the venue accepts at its size, and ``hold_margin_to_auto`` keeps the whole book's
margin at or under what `auto` needs.  None of them touches a weight or an order.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from beidou_exchange.rules import meets_min_notional, quantize_qty
from beidou_live.rebalancer import PlannedOrder
from beidou_shared.types import InstrumentRules


def derive_leverage(max_gross: float, margin_cap: float, max_leverage: int, bracket_max: int | None = None) -> int:
    """Smallest integer leverage with ``max_gross / leverage <= margin_cap``, capped by bracket and policy."""
    if max_gross <= 0 or margin_cap <= 0 or max_leverage < 1:
        raise ValueError("max_gross, margin_cap must be positive and max_leverage >= 1")
    needed = math.ceil(max_gross / margin_cap - 1e-12)
    chosen = min(max(1, needed), int(max_leverage))
    if bracket_max is not None and bracket_max >= 1:
        chosen = min(chosen, int(bracket_max))
    return max(1, chosen)


#: `by_vol`'s ladder (2026-09-27; `docs/analysis/2026-09-26-per-symbol-leverage-first-principles.md`
#: §5.2).  The diagnostic that priced it ran the same ladder up to 20.  It stops at 15 here because 3x-15x
#: is the range the operator picked on 2026-09-26's card, and at sigma_ref 0.90 only a symbol whose sigma
#: is under about 0.26 would reach 20 - no symbol in the universe has been there.
VOL_TIERS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15)


def tier_leverage(sigma: float, *, base: int, sigma_ref: float, tiers: Sequence[int]) -> int | None:
    """`by_vol`'s exchange leverage for one symbol: the tier nearest ``base * sigma_ref / sigma`` in log distance.

    ``base`` is what `auto` sets (D-016's derivation, 5x at the shipped profile), so a symbol at
    ``sigma_ref`` keeps today's leverage and every other one moves by the ratio of the two vols: the
    initial margin per position then stays roughly level instead of following the vol-target weight.
    Log distance because the ladder is geometric.  A tie goes to the lower tier, the side that asks for
    more margin, which is what `min` over an ascending ladder returns.  ``sigma_ref`` is a constant
    rather than the book's median so that one name's vol moving does not re-tier every other name.

    None for a sigma that is missing, zero or not finite: the caller keeps the standing setting.
    """
    if not (isinstance(sigma, int | float) and math.isfinite(sigma) and sigma > 0):
        return None
    ideal = base * sigma_ref / float(sigma)
    return min(sorted(int(tier) for tier in tiers), key=lambda tier: abs(math.log(tier) - math.log(ideal)))


def bracket_leverage(table: Sequence[tuple[float, int]], notional: float) -> int | None:
    """The highest leverage the venue accepts for a position of ``notional`` (E-029, T-9).

    ``table`` is one symbol's ``(notional_cap, initial_leverage)`` rows.  The venue ties leverage to size:
    brackets rise in notional and fall in leverage, and a position may carry the leverage of the first
    bracket whose cap is above it.  ``leverage_brackets`` reads bracket 1 only, the cap for the smallest
    positions, and that was enough for `auto`.  `by_vol` asks for up to 15x, and above a bracket's cap that
    setting is refused (-2027), which D-031 turns into a quarantine after three cycles.  A notional past
    every cap gets the last bracket's leverage, the most the table can offer; None when there is no table.
    """
    rows = sorted(table, key=lambda row: float(row[0]))
    if not rows:
        return None
    for cap, leverage in rows:
        if abs(notional) < float(cap):
            return int(leverage)
    return int(rows[-1][1])


def hold_margin_to_auto(
    chosen: Mapping[str, int],
    notional: Mapping[str, float],
    auto: Mapping[str, int],
    caps: Mapping[str, int],
    tiers: Sequence[int],
) -> tuple[dict[str, int], list[str], float]:
    """`by_vol`'s invariant (M7): under tiers the book never needs more initial margin than under `auto`.

    ``notional`` is the book this cycle is about to hold; ``auto`` is what D-016 would set; ``caps`` bound
    each symbol (the bracket at its notional, the ladder's top).  Both books need ``sum |notional| /
    leverage``, so while the tiered one needs more, the name on the lowest tier moves up one rung - the
    larger position first on a tie - and never past its cap.  If no name can move and the gap is still
    open, every held name below `auto`'s value (capped) is lifted to it, which meets the bound.  No
    position is shrunk: this changes leverage and only leverage, which is what keeps `by_vol` on the display
    side of the firewall (§8.2).

    Why `auto`'s margin rather than `margin_cap` or the available balance: it is the tightest of the three
    that can always be met.  At `max_gross`, `auto` needs at most `margin_cap` of equity (D-016's check),
    so the tiered book needs no more than that either, and the state in which ``scale_orders_to_margin``
    starts shrinking orders is no nearer under tiers than under `auto` (M7, T-5).  It bounds the book,
    not one cycle's orders: the orders of a single cycle may sit on lower tiers than the book as a whole
    and need more margin than they would at 5x.  At k=0.175 the book needs about 4% of equity against a
    pre-check budget near 90% of it (the report's A-005), which is why that gap cannot reach the
    pre-check; T-6's replay of the real record is the check that it never did.

    Returns the leverage per symbol, the names that were raised, and the bound (`auto`'s margin).
    """
    ladder = sorted({int(tier) for tier in tiers})
    out = {symbol: int(value) for symbol, value in chosen.items()}
    held = {symbol: abs(float(value)) for symbol, value in notional.items() if symbol in out and value}
    top = {symbol: int(caps.get(symbol, ladder[-1])) for symbol in held}
    fallback = {symbol: max(1, min(int(auto.get(symbol, out[symbol])), top[symbol])) for symbol in held}
    budget = sum(value / fallback[symbol] for symbol, value in held.items())
    raised: list[str] = []
    while sum(value / max(1, out[symbol]) for symbol, value in held.items()) > budget * (1.0 + 1e-12):
        movable = [
            (out[symbol], -value, symbol, step)
            for symbol, value in held.items()
            for step in [next((tier for tier in ladder if tier > out[symbol]), None)]
            if step is not None and step <= top[symbol]
        ]
        if not movable:
            lifted = [symbol for symbol in held if out[symbol] < fallback[symbol]]
            out.update({symbol: fallback[symbol] for symbol in lifted})
            raised.extend(symbol for symbol in lifted if symbol not in raised)
            break
        _, _, symbol, step = min(movable)
        out[symbol] = step
        if symbol not in raised:
            raised.append(symbol)
    return out, sorted(raised), budget


def hysteresis_step(ideal: int, standing: int | None, streak: int, cycles: int) -> tuple[int | None, int]:
    """S2: move a symbol to a new tier only after ``cycles`` consecutive cycles asking for a different one.

    Returns the leverage to send (None keeps what stands) and the streak to carry.  The streak counts
    cycles in which the tier differed from what stands, whichever tier it was, and resets the moment the
    two agree: sigma dithering across a rung boundary sends nothing (T-8), and a move that persists is
    sent on the ``cycles``-th cycle, to that cycle's tier.  As shipped that is 168 hourly cycles, a week, and EC-2's
    sigma jump costs a week of the old margin and nothing else, because leverage here sets margin, not exposure.
    """
    if standing is None:
        return ideal, 0
    if ideal == standing:
        return None, 0
    if streak + 1 >= cycles:
        return ideal, 0
    return None, streak + 1


def by_vol_problems(tiers: Sequence[int], sigma_ref: float, cycles: int) -> list[str]:
    """S5: what makes a `by_vol` profile unusable, checked at startup the way D-016's margin is for `auto`."""
    problems = []
    if not tiers or list(tiers) != sorted({int(tier) for tier in tiers}) or int(tiers[0]) < 1:
        problems.append(f"leverage_tiers {list(tiers)} must be distinct integers >= 1, ascending")
    if not (math.isfinite(sigma_ref) and sigma_ref > 0):
        problems.append(f"leverage_sigma_ref {sigma_ref} must be a positive number")
    if cycles < 1:
        problems.append(f"leverage_hysteresis {cycles} must be at least one cycle")
    return problems


@dataclass(frozen=True)
class LeveragePlan:
    """One cycle of `by_vol`: what to set, and the four readings that explain it (the cycle row's block)."""

    wanted: dict[str, int]
    ideal: dict[str, int]
    streaks: dict[str, int]
    clamped: dict[str, int]
    raised: list[str]
    margin: dict[str, float]


def plan_leverage(
    *,
    managed: Sequence[str],
    sigma: Mapping[str, float],
    standing: Mapping[str, int],
    tiered: Collection[str],
    streaks: Mapping[str, int],
    target_notional: Mapping[str, float],
    position_notional: Mapping[str, float],
    auto: Mapping[str, int],
    tables: Mapping[str, Sequence[tuple[float, int]]],
    base: int,
    sigma_ref: float,
    tiers: Sequence[int],
    cycles: int,
) -> LeveragePlan:
    """`by_vol`'s per-cycle decision, in the order that makes each rule bind the one after it.

    1. The cap: the bracket at the larger of the target and the standing position, and the ladder's top.
       It is what the venue will accept rather than a preference, so it applies at once, to the vol tier
       and to whatever stands.  ``clamped`` names every symbol whose tier or standing setting is above it.
    2. The vol tier under that cap.  A symbol this loop has not tiered yet takes it at once - the first
       cycle after the switch, or after it entered the universe - and one it has waits out
       ``hysteresis_step``, measured between capped values: a cap that binds every cycle is not a move
       being asked for, and must not build a streak.  No sigma this cycle keeps the standing setting.
    3. ``hold_margin_to_auto`` over the target book, also at once, for the same reason as the cap.

    ``target_notional`` is signed or not - only its size matters - and in the account's units, as is
    ``margin``, which the caller divides by equity.
    """
    ladder = sorted({int(tier) for tier in tiers})
    ideal: dict[str, int] = {}
    wanted: dict[str, int] = {}
    carry: dict[str, int] = {}
    caps: dict[str, int] = {}
    clamped: dict[str, int] = {}
    for symbol in managed:
        size = max(abs(float(target_notional.get(symbol, 0.0))), abs(float(position_notional.get(symbol, 0.0))))
        allowed = bracket_leverage(tables.get(symbol, ()), size)
        cap = caps[symbol] = ladder[-1] if allowed is None else max(1, min(ladder[-1], allowed))
        now = None if standing.get(symbol) is None else int(standing[symbol])
        tier = tier_leverage(sigma.get(symbol, math.nan), base=base, sigma_ref=sigma_ref, tiers=ladder)
        if max(tier or 0, now or 0) > cap:
            clamped[symbol] = cap
        if tier is None:
            if now is not None:
                wanted[symbol] = min(now, cap)
            continue
        ideal[symbol] = tier
        if symbol not in tiered or now is None:
            wanted[symbol] = min(tier, cap)
            continue
        step, streak = hysteresis_step(min(tier, cap), min(now, cap), int(streaks.get(symbol, 0)), cycles)
        wanted[symbol] = min(now, cap) if step is None else step
        if streak:
            carry[symbol] = streak
    wanted, raised, bound = hold_margin_to_auto(wanted, target_notional, auto, caps, ladder)
    need = sum(abs(float(value)) / wanted[symbol] for symbol, value in target_notional.items() if symbol in wanted)
    return LeveragePlan(
        wanted=wanted,
        ideal=ideal,
        streaks=carry,
        clamped=clamped,
        raised=raised,
        margin={"tiered": need, "auto": bound},
    )


def _margin_exposure(order: PlannedOrder) -> float:
    """The part of ``order.notional`` that actually *consumes* margin.

    A flip - long into short, or the reverse - is not ``reduce_only``, because it ends on the other
    side.  Its quantity is ``|current| + |target|``, and the ``|current|`` half CLOSES the standing
    position, so it releases margin rather than consuming it.  Counting the whole notional overstated
    ``needed`` and could shrink a book that fits (2026-09-13 review).  Only the ``|target|`` half is new
    exposure, expressed as ``notional - |current|`` rather than as a flat ``|target|`` so that a flip
    whose quantity was already truncated upstream (``max_order_notional``, the participation cap) is
    charged for what it actually opens - such an order may not even reach zero, and then it opens
    nothing.  For every other order this is ``order.notional`` unchanged.
    """
    flips = order.current_notional * order.target_notional < 0.0
    if not flips:
        return order.notional
    return max(0.0, order.notional - abs(order.current_notional))


def scale_orders_to_margin(
    orders: Sequence[PlannedOrder],
    available_balance: float,
    leverage_by_symbol: Mapping[str, int],
    rules: Mapping[str, InstrumentRules],
    *,
    buffer: float = 0.10,
    default_leverage: int = 1,
) -> tuple[list[PlannedOrder], dict[str, Any]]:
    """Shrink risk-adding orders pro-rata so their initial margin fits ``available_balance * (1 - buffer)``.

    Reduce-only orders are never scaled (they release margin).  Orders that
    fall below the venue minimum after scaling are dropped and reported.

    This is a PRE-CHECK, which is why ``_margin_exposure`` may correct it downwards without a window:
    it exists so the venue does not reject orders one by one (-2019), it can only ever shrink what
    ``plan_rebalance`` already decided, and the venue's own margin check remains the backstop.
    """
    adds = [order for order in orders if not order.reduce_only]
    needed = sum(
        _margin_exposure(order) / max(1, leverage_by_symbol.get(order.symbol, default_leverage)) for order in adds
    )
    budget = max(0.0, float(available_balance)) * (1.0 - buffer)
    if not adds or needed <= budget:
        return list(orders), {"scaled": False, "needed_margin": needed, "budget": budget}
    factor = budget / needed if needed > 0 else 0.0
    kept: list[PlannedOrder] = []
    dropped: list[dict[str, Any]] = []
    for order in orders:
        if order.reduce_only:
            kept.append(order)
            continue
        rule = rules.get(order.symbol)
        # No rule means no step to quantize to, and an unquantizable order is dropped below rather than
        # sent at an unknown step.  Spelled `Decimal(0)`: it used to read `order.quantity * 0`, which is
        # the same zero by Decimal arithmetic but reads like a typo.
        quantity = quantize_qty(float(order.quantity) * factor, rule) if rule is not None else Decimal(0)
        if quantity <= 0 or (rule is not None and not meets_min_notional(quantity, order.price, rule)):
            dropped.append({"symbol": order.symbol, "reason": "MARGIN_SCALED_BELOW_MIN", "factor": factor})
            continue
        kept.append(replace(order, quantity=quantity, note=(order.note + ";" if order.note else "") + "MARGIN_SCALED"))
    return kept, {"scaled": True, "factor": factor, "needed_margin": needed, "budget": budget, "dropped": dropped}
