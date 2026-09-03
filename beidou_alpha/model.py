"""The alpha model: registry entries -> per-strategy targets -> combined conviction -> portfolio weights."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from beidou_alpha.ensemble import TargetWeights, combine_targets, snapshot
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, build_weights
from beidou_alpha.registry import Registry, StrategyEntry
from beidou_alpha.signals import get_signal, scores_to_targets


@dataclass(frozen=True)
class AlphaModel:
    entries: tuple[StrategyEntry, ...]
    portfolio: PortfolioParams
    interval: str
    ensemble_method: str = "mean"
    hold_on_no_action: bool = True

    @classmethod
    def from_registry(cls, registry: Registry, portfolio: PortfolioParams, interval: str) -> AlphaModel:
        if not registry.enabled:
            raise ValueError("registry has no enabled strategies")
        return cls(
            entries=registry.enabled, portfolio=portfolio, interval=interval, ensemble_method=registry.ensemble_method
        )

    @property
    def warmup_bars(self) -> int:
        signal_warmup = max(get_signal(entry.id).warmup_bars for entry in self.entries)
        return max(signal_warmup, self.portfolio.covariance_halflife, self.portfolio.vol_halflife) + 1

    def strategy_scores(self, panel: Panel) -> dict[str, pd.DataFrame]:
        return {entry.id: get_signal(entry.id).compute(panel, entry.params) for entry in self.entries}

    def strategy_targets(self, panel: Panel) -> dict[str, pd.DataFrame]:
        return {
            entry.id: scores_to_targets(scores, entry.entry_threshold, hold=self.hold_on_no_action)
            for entry, scores in zip(self.entries, self.strategy_scores(panel).values(), strict=True)
        }

    def combined_targets(self, panel: Panel, targets: Mapping[str, pd.DataFrame] | None = None) -> pd.DataFrame:
        per_strategy = dict(targets) if targets is not None else self.strategy_targets(panel)
        return combine_targets(
            per_strategy, {entry.id: entry.weight for entry in self.entries}, method=self.ensemble_method
        )

    def weights(self, panel: Panel) -> pd.DataFrame:
        return build_weights(self.combined_targets(panel), panel.close, panel.bars_per_year, self.portfolio)

    def evaluate(self, panel: Panel) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
        per_strategy = self.strategy_targets(panel)
        combined = self.combined_targets(panel, per_strategy)
        weights = build_weights(combined, panel.close, panel.bars_per_year, self.portfolio)
        return weights, combined, per_strategy

    def targets(self, bars: Mapping[str, pd.DataFrame], funding: Mapping[str, float]) -> TargetWeights:
        """Live entry point: closed bars per symbol (+ latest funding, unused by tsmom) -> latest target weights."""
        panel = Panel.from_frames(bars, interval=self.interval)
        if len(panel.index) < self.warmup_bars:
            raise ValueError(f"need at least {self.warmup_bars} closed bars, got {len(panel.index)}")
        weights, combined, per_strategy = self.evaluate(panel)
        return snapshot(weights, combined, per_strategy)
