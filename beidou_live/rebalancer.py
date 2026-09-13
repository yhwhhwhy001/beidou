"""Target weights -> concrete orders against the venue's real positions.

One order per symbol per bar, with a deterministic client id derived from
the bar open time so a crash/restart inside a bar cannot double-submit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from beidou_exchange.rules import meets_min_notional, quantize_qty
from beidou_shared.types import InstrumentRules, Position, Side


@dataclass(frozen=True)
class RebalanceParams:
    """``exempt_reductions`` must be flipped together with ``beidou_alpha.backtest.ParticipationModel``.

    The two halves replay the same cap and only agree because they are written to the same rule.  Flip
    one and the backtest scores a book live cannot execute, or refuses turnover live would have sent -
    KILL-027's shape, where a live-only guardrail made the two halves quietly different instruments.
    """

    no_trade_band: float = 0.005
    no_trade_rel_band: float = 0.0
    max_order_notional: float | None = None
    max_participation: float = 0.0  # cap on a risk-adding order: fraction of the symbol's average hourly quote volume
    # False = today's scope, where only a full close escapes the cap.  True = the scope the line above
    # has always claimed: every ``reduce_only`` order (full close AND pure reduction) escapes it.
    exempt_reductions: bool = False
    tag: str = "bd"


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: Side
    quantity: Decimal
    reduce_only: bool
    client_order_id: str
    target_weight: float
    current_notional: float
    target_notional: float
    price: float
    note: str = ""

    @property
    def notional(self) -> float:
        return float(self.quantity) * self.price

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "reduce_only": self.reduce_only,
            "client_order_id": self.client_order_id,
            "target_weight": self.target_weight,
            "current_notional": self.current_notional,
            "target_notional": self.target_notional,
            "price": self.price,
            "note": self.note,
        }


def client_order_id(tag: str, symbol: str, bar_open_ms: int, suffix: str = "") -> str:
    identifier = f"{tag}-{int(bar_open_ms)}-{symbol}{suffix}"
    if len(identifier) > 36:
        raise ValueError("client order id exceeds 36 characters")
    return identifier


def plan_rebalance(
    targets: Mapping[str, float],
    *,
    managed_symbols: Sequence[str],
    equity: float,
    positions: Mapping[str, Position],
    prices: Mapping[str, float],
    rules: Mapping[str, InstrumentRules],
    bar_open_ms: int,
    params: RebalanceParams,
    liquidity: Mapping[str, float] | None = None,
    deescalating: bool = False,
) -> tuple[list[PlannedOrder], list[dict[str, Any]]]:
    """``liquidity``: average hourly quote volume per symbol, used with ``params.max_participation`` (T-S03).

    ``deescalating``: R8's ladder is acting this cycle, so a same-direction REDUCTION is a risk action
    rather than a rebalance and skips the relative band.  Not a ``RebalanceParams`` field on purpose -
    it is the state of a governance rule, already recorded per cycle under ``risk_ladder``, not a
    construction choice, and it must stay out of ``construction_fingerprint`` for the same reason.
    """
    orders: list[PlannedOrder] = []
    skipped: list[dict[str, Any]] = []
    if equity <= 0:
        return [], [{"reason": "NON_POSITIVE_EQUITY", "equity": equity}]
    for symbol in managed_symbols:
        target_weight = float(targets.get(symbol, 0.0) or 0.0)
        position = positions.get(symbol)
        current_qty = position.qty if position is not None else 0.0
        price = prices.get(symbol) or (position.mark_price if position is not None else None)
        rule = rules.get(symbol)
        if price is None or price <= 0:
            skipped.append({"symbol": symbol, "reason": "NO_PRICE"})
            continue
        if rule is None or not rule.tradable:
            skipped.append({"symbol": symbol, "reason": "NOT_TRADABLE"})
            continue
        target_notional = target_weight * equity
        current_notional = current_qty * price
        delta = target_notional - current_notional
        same_direction_resize = (
            current_qty != 0.0 and target_notional != 0.0 and (target_notional > 0) == (current_qty > 0)
        )
        threshold = params.no_trade_band * equity
        # R8's first rung could not place an order, and both halves of the record read normal.  At
        # vol_target 0.30 - what the loop ran from R8's wiring on 2026-09-09 until the 0.60 restart -
        # the rung takes 0.30 -> 0.225, i.e. it multiplies every weight by 0.75, so a book standing at
        # its unthrottled target needs an order of 0.25 x |current|, inside a relative band of 0.40 x
        # |current|.  The de-escalation was swallowed at every drawdown between the two rungs; only the
        # second (0.15/0.30 = 0.50) cleared the band, so a two-rung ladder had one rung.
        # `risk_ladder.acting` said true and `skipped[]` said NO_TRADE_BAND, and neither row is wrong.
        #
        # The relative band's job is suppressing small same-direction RESIZES on the ordinary weight
        # path; the ladder is not on that path, it is a step imposed on top of it, and it only ever
        # shrinks.  So the exception is exactly that: reductions, while the ladder acts.  An increase
        # keeps the band even then - a raw target that grew through the scalar is ordinary rebalancing.
        # The absolute band is deliberately still applied, so this cannot manufacture dust.
        #
        # Whether a rung clears the band at all is an accident of k.  The loop now runs 0.60 (72034790
        # moved the evidence pointer, construction 46b8d731530a), where the rungs are 0.375 and 0.25
        # and both clear - so this repairs nothing that is broken TODAY, and that is the point: the
        # same commit rejected O-3, restating the rungs proportionally to a wider budget, precisely
        # because it put the first one back at 0.75.  The coincidence recurs at the next re-scale.
        shedding = deescalating and abs(target_notional) < abs(current_notional)
        if same_direction_resize and not shedding:
            threshold = max(threshold, params.no_trade_rel_band * abs(current_notional))
        if abs(delta) < threshold:
            # The most common outcome of a cycle used to be the only one that left no trace: a bare
            # `continue`.  So a symbol the band can never let in read exactly like a symbol that did not
            # need trading - CYSUSDT carried a -41 USDT target against a 54 USDT absolute band for a full
            # day, was scored every cycle, ordered never, and no report could name it.  The two blocked
            # cases are separated from the ordinary suppressed resize because they are structural, not
            # transient: no price path clears the band while the target stays this small.  Flat and
            # wanting flat is neither, and stays silent so `leaving` symbols cannot flood the log.
            if current_qty != 0.0 or target_notional != 0.0:
                skipped.append(
                    {
                        "symbol": symbol,
                        "reason": (
                            "BAND_BLOCKS_ENTRY"
                            if current_qty == 0.0
                            else "BAND_BLOCKS_EXIT"
                            if abs(target_notional) < 1e-9
                            else "NO_TRADE_BAND"
                        ),
                        "delta_notional": delta,
                        "threshold": threshold,
                    }
                )
            continue
        closing = abs(target_notional) < 1e-9 and current_qty != 0.0
        same_direction = current_qty != 0.0 and (target_notional > 0) == (current_qty > 0)
        pure_reduction = same_direction and abs(target_notional) < abs(current_notional)
        reduce_only = closing or pure_reduction
        if closing:
            quantity = quantize_qty(abs(current_qty), rule)
            if quantity == 0:
                quantity = Decimal(str(abs(current_qty)))
        else:
            quantity = quantize_qty(abs(delta) / price, rule)
            if reduce_only:
                quantity = min(quantity, quantize_qty(abs(current_qty), rule))
        if quantity <= 0:
            skipped.append({"symbol": symbol, "reason": "QUANTITY_ROUNDS_TO_ZERO", "delta_notional": delta})
            continue
        if (
            params.max_order_notional is not None
            and float(quantity) * price > params.max_order_notional
            and not closing
        ):
            quantity = quantize_qty(params.max_order_notional / price, rule)
            if quantity <= 0:
                skipped.append({"symbol": symbol, "reason": "ORDER_CAP_BELOW_STEP"})
                continue
        note = ""
        cap = (liquidity or {}).get(symbol)
        # T-S03's cap and the scope it actually has.  The field's comment has always read "risk-adding
        # order", but the exemption here was `closing` alone, so a 15% -> 5% pure reduction was
        # truncated by `max_participation x trailing volume` - a cap that shrinks with the same volume
        # curve that dries up in the bar where getting smaller matters most (2026-09-13 review, second
        # pass: the exit channel contracts with liquidity).  Widening it is a live behaviour change and
        # moves every backtest number, so it is a knob, off by default, and the backtest half carries
        # the same one under the same name.
        exempt_from_cap = reduce_only if params.exempt_reductions else closing
        if params.max_participation > 0 and cap is not None and cap > 0 and not exempt_from_cap:
            allowed = params.max_participation * float(cap)
            if float(quantity) * price > allowed:
                quantity = quantize_qty(allowed / price, rule)
                note = "PARTICIPATION_CAPPED"
                if quantity <= 0:
                    skipped.append(
                        {"symbol": symbol, "reason": "PARTICIPATION_CAP_BELOW_STEP", "cap_notional": allowed}
                    )
                    continue
        if not reduce_only and not meets_min_notional(quantity, price, rule):
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "MIN_NOTIONAL",
                    "notional": float(quantity) * price,
                    "min": str(rule.min_notional),
                }
            )
            continue
        orders.append(
            PlannedOrder(
                symbol=symbol,
                side=Side.for_delta(delta),
                quantity=quantity,
                reduce_only=reduce_only,
                client_order_id=client_order_id(params.tag, symbol, bar_open_ms),
                target_weight=target_weight,
                current_notional=current_notional,
                target_notional=target_notional,
                price=float(price),
                note=note,
            )
        )
    return orders, skipped


def flatten_orders(
    positions: Mapping[str, Position], rules: Mapping[str, InstrumentRules], bar_open_ms: int, tag: str = "bdflat"
) -> list[PlannedOrder]:
    orders: list[PlannedOrder] = []
    for symbol, position in positions.items():
        if position.qty == 0.0:
            continue
        rule = rules.get(symbol)
        quantity = quantize_qty(abs(position.qty), rule) if rule is not None else Decimal(str(abs(position.qty)))
        if quantity <= 0:
            quantity = Decimal(str(abs(position.qty)))
        orders.append(
            PlannedOrder(
                symbol=symbol,
                side=Side.SELL if position.qty > 0 else Side.BUY,
                quantity=quantity,
                reduce_only=True,
                client_order_id=client_order_id(tag, symbol, bar_open_ms),
                target_weight=0.0,
                current_notional=position.notional,
                target_notional=0.0,
                price=position.mark_price,
            )
        )
    return orders
