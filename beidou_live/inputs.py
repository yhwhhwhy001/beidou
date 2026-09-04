"""What one live cycle feeds the model: closed bars, the latest funding rates and, when the model reads it,
funding history over the same window.

Shared by the engine and by ``beidou live verify`` so the offline reproduction of a cycle (M-011) builds its
inputs through exactly the same code as the cycle itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from beidou_live.ports import MarketData, SignalModel


def required_history(model: SignalModel, floor: int) -> int:
    """Closed bars a cycle must request: the listing-age filter plus the model's warmup under its real params."""
    needed = int(getattr(model, "min_history_bars", 0)) + int(getattr(model, "warmup_bars", 0))
    return max(int(floor), needed)


def first_open_ms(frame: pd.DataFrame) -> int | None:
    """Open time of the first bar in a closed-bar frame (``open_time`` column or a UTC DatetimeIndex)."""
    if len(frame) == 0:
        return None
    if "open_time" in frame.columns:
        return int(frame["open_time"].iloc[0])
    if isinstance(frame.index, pd.DatetimeIndex):
        stamp = pd.Timestamp(frame.index[0])
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
        return int(stamp.timestamp() * 1000)
    return None


@dataclass
class ModelInputs:
    bars: dict[str, pd.DataFrame]
    funding: dict[str, float]
    funding_history: dict[str, pd.Series] | None = None
    dropped: list[str] = field(default_factory=list)  # symbols whose frame was missing or too short

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": len(self.bars),
            "bars": min((len(frame) for frame in self.bars.values()), default=0),
            "funding_history": self.funding_history is not None,
            "dropped": list(self.dropped),
        }


async def model_inputs(
    market: MarketData, model: SignalModel, symbols: Sequence[str], interval: str, history_bars: int
) -> ModelInputs:
    """Fetch the cycle's inputs.  Funding history is fetched only when the model consumes it (KILL-027)."""
    bars = await market.closed_bars(symbols, interval, history_bars)
    usable = {symbol: frame for symbol, frame in bars.items() if frame is not None and len(frame) >= 2}
    dropped = [symbol for symbol in symbols if symbol not in usable]
    if not usable:
        raise RuntimeError("no closed bars returned for the universe")
    funding = await market.funding_rates(symbols)
    history: dict[str, pd.Series] | None = None
    if bool(getattr(model, "needs_funding", False)):
        starts = [start for start in (first_open_ms(frame) for frame in usable.values()) if start is not None]
        history = await market.funding_history(list(usable), min(starts) if starts else 0)
    return ModelInputs(bars=usable, funding=dict(funding), funding_history=history, dropped=dropped)


def latest_closes(bars: Mapping[str, pd.DataFrame]) -> dict[str, float]:
    return {symbol: float(frame["close"].iloc[-1]) for symbol, frame in bars.items()}
