"""Protocols that decouple the live loop from concrete data/venue/alpha implementations.

``beidou_live`` only ever talks to these interfaces.  ``beidou_exchange``
implements :class:`Venue`, ``beidou_data`` implements :class:`MarketData`,
and ``beidou_alpha`` provides the model behind :class:`SignalModel`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import pandas as pd

from beidou_shared.types import AccountState, InstrumentRules, OrderAck, OrderRequest, Position


class Clock(Protocol):
    def now_ms(self) -> int: ...

    async def sleep(self, seconds: float) -> None: ...


class MarketData(Protocol):
    """Closed-bar market data for signal computation (mainnet public data)."""

    async def closed_bars(self, symbols: Sequence[str], interval: str, limit: int) -> dict[str, pd.DataFrame]:
        """Return, per symbol, a frame of *closed* bars indexed by UTC open time."""
        ...

    async def funding_rates(self, symbols: Sequence[str]) -> dict[str, float]:
        """Latest settled funding rate per symbol (fraction per 8h)."""
        ...

    async def funding_history(self, symbols: Sequence[str], start_ms: int) -> dict[str, pd.Series]:
        """Settled funding rates per symbol since ``start_ms``, indexed by UTC settlement time.

        Aligned onto bar open times by ``Panel.from_frames`` exactly as the
        research store does, so a funding-consuming signal sees the same panel
        live as in validation (KILL-027).
        """
        ...


class Venue(Protocol):
    """The exchange account we trade on (demo/testnet)."""

    async def rules(self) -> dict[str, InstrumentRules]: ...

    async def account(self) -> AccountState: ...

    async def positions(self) -> dict[str, Position]: ...

    async def mark_prices(self, symbols: Sequence[str]) -> dict[str, float]: ...

    async def set_leverage(self, symbol: str, leverage: int) -> int: ...

    async def place_order(self, request: OrderRequest) -> OrderAck: ...

    async def query_order(self, symbol: str, client_order_id: str) -> OrderAck | None: ...

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderAck | None: ...

    async def open_orders(self, symbol: str | None = None) -> list[OrderAck]: ...

    async def income(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]: ...


class UniverseUpdate(Protocol):
    """Result of a universe refresh (structural; see ``beidou_data.pool.UniverseUpdate``)."""

    @property
    def symbols(self) -> tuple[str, ...]: ...

    @property
    def entered(self) -> tuple[str, ...]: ...

    @property
    def left(self) -> tuple[str, ...]: ...

    @property
    def at_ms(self) -> int: ...

    def to_dict(self) -> dict[str, Any]: ...


class UniverseProvider(Protocol):
    """Re-ranks the tradable universe (daily); the engine flattens what leaves (D-014)."""

    async def select(self, previous: Sequence[str], rules: Mapping[str, InstrumentRules]) -> UniverseUpdate: ...


class TargetSet(Protocol):
    """Output contract of a signal model (structural; see ``beidou_alpha.ensemble.TargetWeights``)."""

    @property
    def weights(self) -> dict[str, float]: ...

    @property
    def contributions(self) -> dict[str, dict[str, float]]: ...

    @property
    def as_of(self) -> pd.Timestamp: ...


class SignalModel(Protocol):
    """Turns closed bars plus funding into target weights with per-strategy attribution.

    ``previous`` carries the last cycle's per-strategy targets so NO_ACTION can
    hold a position across cycles (D-005) independently of the request window.
    """

    @property
    def warmup_bars(self) -> int: ...

    @property
    def min_history_bars(self) -> int: ...

    @property
    def needs_funding(self) -> bool: ...

    def targets(
        self,
        bars: Mapping[str, pd.DataFrame],
        funding: Mapping[str, float],
        previous: Mapping[str, Mapping[str, float]] | None = None,
        funding_history: Mapping[str, pd.Series] | pd.DataFrame | None = None,
    ) -> TargetSet: ...
