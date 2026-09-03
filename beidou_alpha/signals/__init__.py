"""Signal registry.  Each signal exposes ``compute(panel, params) -> DataFrame[time x symbol]`` with scores in [-1, 1]."""

from __future__ import annotations

from dataclasses import asdict

from beidou_alpha.signals import tsmom
from beidou_alpha.signals.base import SignalSpec, scores_to_targets

SIGNALS: dict[str, SignalSpec] = {
    "tsmom": SignalSpec(
        id="tsmom",
        compute=tsmom.compute,
        default_params=asdict(tsmom.TsmomParams()),
        description="multi-horizon time-series momentum (TrendAlpha port)",
        warmup_bars=tsmom.TsmomParams().warmup_bars,
    ),
}


def get_signal(signal_id: str) -> SignalSpec:
    try:
        return SIGNALS[signal_id]
    except KeyError as exc:
        raise KeyError(f"unknown signal {signal_id!r}; known: {sorted(SIGNALS)}") from exc


__all__ = ["SIGNALS", "SignalSpec", "get_signal", "scores_to_targets"]
