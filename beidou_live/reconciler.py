"""Venue truth: account, positions, prices, foreign positions and stale open orders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from beidou_live.ports import Venue
from beidou_shared.types import AccountState, OrderAck, Position


@dataclass
class Snapshot:
    account: AccountState
    positions: dict[str, Position]
    prices: dict[str, float]
    foreign_positions: dict[str, Position] = field(default_factory=dict)
    open_orders: list[OrderAck] = field(default_factory=list)

    @property
    def equity(self) -> float:
        return self.account.equity

    def weights(self) -> dict[str, float]:
        if self.account.equity <= 0:
            return {}
        return {symbol: position.notional / self.account.equity for symbol, position in self.positions.items()}

    def gross_notional(self) -> float:
        """Sum of |notional| over the managed positions, from ``positionRisk`` — the loop's only position truth.

        Not ``AccountState.gross_notional()``: the venue's account payload does not always carry a
        positions array, and when it does not that method silently returns 0.0, which made a fully
        invested book look flat in ``cycles.jsonl``.
        """
        return sum(abs(position.notional) for position in self.positions.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "equity": self.account.equity,
            "wallet_balance": self.account.wallet_balance,
            "available_balance": self.account.available_balance,
            "positions": {
                s: {"qty": p.qty, "entry": p.entry_price, "mark": p.mark_price, "upnl": p.unrealized_pnl}
                for s, p in self.positions.items()
            },
            "foreign_positions": sorted(self.foreign_positions),
            "open_orders": len(self.open_orders),
        }


async def take_snapshot(venue: Venue, managed_symbols: Sequence[str]) -> Snapshot:
    account = await venue.account()
    positions = await venue.positions()
    prices = await venue.mark_prices(list({*managed_symbols, *positions}))
    managed = set(managed_symbols)
    inside = {symbol: position for symbol, position in positions.items() if symbol in managed}
    foreign = {symbol: position for symbol, position in positions.items() if symbol not in managed}
    for symbol, position in positions.items():
        prices.setdefault(symbol, position.mark_price)
    return Snapshot(account=account, positions=inside, prices=prices, foreign_positions=foreign)


async def startup_reconcile(
    venue: Venue, managed_symbols: Sequence[str], *, cancel_stale_orders: bool = True
) -> Snapshot:
    """Exchange positions are the only truth; stale open orders (we only use market orders) are cancelled."""
    snapshot = await take_snapshot(venue, managed_symbols)
    open_orders = await venue.open_orders()
    snapshot.open_orders = list(open_orders)
    if cancel_stale_orders:
        for order in open_orders:
            await venue.cancel_order(order.symbol, order.client_order_id)
    return snapshot
