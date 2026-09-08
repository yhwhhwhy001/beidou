"""Signal registry.  Each signal exposes ``compute(panel, params) -> DataFrame[time x symbol]`` with scores in [-1, 1]."""

from __future__ import annotations

from dataclasses import asdict

from beidou_alpha.signals import breakout, carry, flow, meanrev, residual, tsmom, xsmom
from beidou_alpha.signals.base import SignalSpec, scores_to_targets

SIGNALS: dict[str, SignalSpec] = {
    "tsmom": SignalSpec(
        "tsmom",
        tsmom.compute,
        asdict(tsmom.TsmomParams()),
        "multi-horizon time-series momentum (TrendAlpha port)",
        tsmom.TsmomParams().warmup_bars,
        warmup=lambda params: tsmom.TsmomParams.from_mapping(params).warmup_bars,
    ),
    "xsmom": SignalSpec(
        "xsmom",
        xsmom.compute,
        asdict(xsmom.XsmomParams()),
        "cross-sectional momentum / relative strength, market-neutral",
        xsmom.XsmomParams().warmup_bars,
        warmup=lambda params: xsmom.XsmomParams.from_mapping(params).warmup_bars,
    ),
    "carry": SignalSpec(
        "carry",
        carry.compute,
        asdict(carry.CarryParams()),
        "funding-rate carry (short high funding, long low funding)",
        carry.CarryParams().warmup_bars,
        warmup=lambda params: carry.CarryParams.from_mapping(params).warmup_bars,
    ),
    "meanrev": SignalSpec(
        "meanrev",
        meanrev.compute,
        asdict(meanrev.MeanrevParams()),
        "robust z-score mean reversion with trend gate and explicit exits",
        meanrev.MeanrevParams().warmup_bars,
        warmup=lambda params: meanrev.MeanrevParams.from_mapping(params).warmup_bars,
    ),
    "breakout": SignalSpec(
        "breakout",
        breakout.compute,
        asdict(breakout.BreakoutParams()),
        "ATR-normalised Donchian breakout with volume/breadth confirmation",
        breakout.BreakoutParams().warmup_bars,
        warmup=lambda params: breakout.BreakoutParams.from_mapping(params).warmup_bars,
    ),
    "flow": SignalSpec(
        "flow",
        flow.compute,
        asdict(flow.FlowParams()),
        "taker-buy order-flow imbalance scaled by volume expansion",
        flow.FlowParams().warmup_bars,
        warmup=lambda params: flow.FlowParams.from_mapping(params).warmup_bars,
    ),
    "residual": SignalSpec(
        "residual",
        residual.compute,
        asdict(residual.ResidualParams()),
        "beta-neutral residual momentum vs BTC",
        residual.ResidualParams().warmup_bars,
        warmup=lambda params: residual.ResidualParams.from_mapping(params).warmup_bars,
    ),
}


def get_signal(signal_id: str) -> SignalSpec:
    try:
        return SIGNALS[signal_id]
    except KeyError as exc:
        raise KeyError(f"unknown signal {signal_id!r}; known: {sorted(SIGNALS)}") from exc


__all__ = ["SIGNALS", "SignalSpec", "get_signal", "scores_to_targets"]
