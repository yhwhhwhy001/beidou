"""The alpha model: registry entries -> per-strategy targets -> per-book conviction -> portfolio weights.

Strategies belong to books.  The main book is built exactly as before (ensemble mean -> vol
target -> caps -> no-trade band).  A non-main book (D-018/D-019) is built on its own, scaled
by its fraction of the main risk budget and summed with the main book; the main book's caps
and band then apply to the total (``combine_books``).  With only the main book present the
code path is bit-for-bit the original one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

import pandas as pd

from beidou_alpha.ensemble import TargetWeights, combine_targets, snapshot
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, build_weights, combine_books
from beidou_alpha.registry import MAIN_BOOK, Registry, StrategyEntry
from beidou_alpha.signals import get_signal, scores_to_targets

# strategy -> symbol -> the target held at the end of the previous live cycle (the D-005 hold seed, E-042)
PreviousTargets = Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class AlphaModel:
    entries: tuple[StrategyEntry, ...]
    portfolio: PortfolioParams
    interval: str
    ensemble_method: str = "mean"
    hold_on_no_action: bool = True
    min_history_bars: int = 720
    books: dict[str, float] = field(default_factory=dict)  # non-main book -> fraction of the main risk budget

    def __post_init__(self) -> None:
        for name, fraction in self.books.items():
            if name == MAIN_BOOK or not 0 < fraction <= 1:
                raise ValueError(f"book {name!r}: fraction must be in (0, 1] and the main book is implicit")
        for entry in self.entries:
            if entry.book != MAIN_BOOK and entry.book not in self.books:
                raise ValueError(f"strategy {entry.id} refers to undeclared book {entry.book!r}")

    @classmethod
    def from_registry(
        cls, registry: Registry, portfolio: PortfolioParams, interval: str, *, min_history_bars: int = 720
    ) -> AlphaModel:
        if not registry.enabled:
            raise ValueError("registry has no enabled strategies")
        return cls(
            entries=registry.enabled,
            portfolio=portfolio,
            interval=interval,
            ensemble_method=registry.ensemble_method,
            min_history_bars=min_history_bars,
            books={name: spec.fraction for name, spec in registry.books.items()},
        )

    # --- books ----------------------------------------------------------------
    @property
    def book_names(self) -> tuple[str, ...]:
        """Books in use, the main book first."""
        names = [MAIN_BOOK] if any(entry.book == MAIN_BOOK for entry in self.entries) else []
        for entry in self.entries:
            if entry.book not in names:
                names.append(entry.book)
        return tuple(names)

    def fraction(self, book: str) -> float:
        return 1.0 if book == MAIN_BOOK else self.books[book]

    def entries_of(self, book: str) -> tuple[StrategyEntry, ...]:
        return tuple(entry for entry in self.entries if entry.book == book)

    def without_books(self, names: Iterable[str]) -> AlphaModel:
        """The same model with the given books (and their strategies) removed - a stopped probe (D-019)."""
        dropped = set(names)
        if MAIN_BOOK in dropped:
            raise ValueError("the main book cannot be stopped")
        entries = tuple(entry for entry in self.entries if entry.book not in dropped)
        if not entries:
            raise ValueError("stopping these books would leave no strategy")
        return replace(self, entries=entries, books={k: v for k, v in self.books.items() if k not in dropped})

    # --- targets --------------------------------------------------------------
    def eligible(self, panel: Panel, membership: pd.DataFrame | None = None) -> pd.DataFrame:
        """Symbols become tradable only after ``min_history_bars`` observed bars (new listings are excluded).

        ``membership`` (bool, bars x symbols) restricts trading to the point-in-time universe (D-013).
        """
        history = panel.close.notna().cumsum() >= self.min_history_bars
        if membership is None:
            return history
        member = membership.reindex(index=panel.index, columns=panel.close.columns).fillna(False).astype(bool)
        return history & member

    @property
    def warmup_bars(self) -> int:
        """Bars the model needs before its first decision, under the *registry* parameters.

        Derived from each signal's actual params (a 720-bar horizon needs 721
        bars), not from the signal defaults: the defaults once sized the live
        request window at 817 bars for a weekly strategy (E-042).
        """
        signal_warmup = max(get_signal(entry.id).warmup_for(entry.params) for entry in self.entries)
        return max(signal_warmup, self.portfolio.covariance_halflife, self.portfolio.vol_halflife) + 1

    def strategy_scores(self, panel: Panel) -> dict[str, pd.DataFrame]:
        return {entry.id: get_signal(entry.id).compute(panel, entry.params) for entry in self.entries}

    def strategy_targets(
        self, panel: Panel, membership: pd.DataFrame | None = None, previous: PreviousTargets | None = None
    ) -> dict[str, pd.DataFrame]:
        """Per-strategy held targets; ``previous`` seeds NO_ACTION with what the live loop held last cycle."""
        eligible = self.eligible(panel, membership)
        targets: dict[str, pd.DataFrame] = {}
        for entry, scores in zip(self.entries, self.strategy_scores(panel).values(), strict=True):
            seed = None if previous is None else previous.get(entry.id)
            held = scores_to_targets(
                scores.where(eligible), entry.entry_threshold, hold=self.hold_on_no_action, initial=seed
            )
            if membership is not None:  # leaving the universe is an explicit exit, never a held position
                held = held.mask(~eligible & held.notna(), 0.0)
            targets[entry.id] = held
        return targets

    def book_targets(self, per_strategy: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Per-book conviction: the ensemble of that book's strategies."""
        return {
            book: combine_targets(
                {entry.id: per_strategy[entry.id] for entry in self.entries_of(book)},
                {entry.id: entry.weight for entry in self.entries_of(book)},
                method=self.ensemble_method,
            )
            for book in self.book_names
        }

    def combined_targets(self, panel: Panel, targets: Mapping[str, pd.DataFrame] | None = None) -> pd.DataFrame:
        """Conviction of the main book (or of the only book when the main one is absent)."""
        per_strategy = dict(targets) if targets is not None else self.strategy_targets(panel)
        return self.book_targets(per_strategy)[self.book_names[0]]

    def book_weights(
        self, per_strategy: Mapping[str, pd.DataFrame], close: pd.DataFrame, bars_per_year: float
    ) -> dict[str, pd.DataFrame]:
        """Each book vol-targeted on its own (no band) and scaled by its fraction."""
        bare = replace(self.portfolio, no_trade_band=0.0, no_trade_rel_band=0.0)
        return {
            book: build_weights(conviction, close, bars_per_year, bare) * self.fraction(book)
            for book, conviction in self.book_targets(per_strategy).items()
        }

    def weights_from(
        self, per_strategy: Mapping[str, pd.DataFrame], close: pd.DataFrame, bars_per_year: float
    ) -> pd.DataFrame:
        if self.book_names == (MAIN_BOOK,):
            return build_weights(self.book_targets(per_strategy)[MAIN_BOOK], close, bars_per_year, self.portfolio)
        return combine_books(self.book_weights(per_strategy, close, bars_per_year), self.portfolio)

    def weights(self, panel: Panel, membership: pd.DataFrame | None = None) -> pd.DataFrame:
        return self.weights_from(self.strategy_targets(panel, membership), panel.close, panel.bars_per_year)

    def evaluate(
        self, panel: Panel, membership: pd.DataFrame | None = None, previous: PreviousTargets | None = None
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
        per_strategy = self.strategy_targets(panel, membership, previous)
        combined = self.combined_targets(panel, per_strategy)
        weights = self.weights_from(per_strategy, panel.close, panel.bars_per_year)
        return weights, combined, per_strategy

    def targets(
        self,
        bars: Mapping[str, pd.DataFrame],
        funding: Mapping[str, float],
        previous: PreviousTargets | None = None,
    ) -> TargetWeights:
        """Live entry point: closed bars per symbol -> latest target weights.

        ``previous`` is the last cycle's per-strategy targets: a sub-threshold
        score keeps the held target (D-005) even when the sub-threshold stretch
        is longer than the request window, exactly as in a full-history
        backtest.  ``funding`` (latest rate per symbol) is accepted for the
        port's sake; no enabled signal consumes it on the live path, and a
        signal that needs funding *history* must not be enabled without wiring
        it into the panel (KILL-027).  Contributions are unscaled per-strategy
        targets (the hold seed of the next cycle); a book's fraction reaches
        attribution through ``LiveConfig.strategy_weights`` (D-019).
        """
        panel = Panel.from_frames(bars, interval=self.interval)
        if len(panel.index) < self.warmup_bars:
            raise ValueError(f"need at least {self.warmup_bars} closed bars, got {len(panel.index)}")
        weights, combined, per_strategy = self.evaluate(panel, previous=previous)
        return snapshot(weights, combined, per_strategy)
