"""Enumerate candidate expressions, deduplicate them by canonical hash, and compile survivors to signals.

The miner **proposes**; nothing here judges.  A candidate compiles to an ordinary ``SignalSpec``, so
walk-forward, CPCV, the trials ledger and the D-020/D-028 verdict decide its fate exactly as they decide a
hand-written signal's.  That is the whole difference from the V2 miner, which carried its own evaluation
stack (and whose generators are unreachable from any entry point in that tree).

**The multiple-testing rule this module exists to enforce.**  A search that evaluates 400 expressions and
reports the best one has performed 400 trials, whatever id the survivor is filed under.  Because
``parse_ledger`` filters the trials ledger *by strategy*, giving each candidate a fresh id would hide the
whole search from the DSR denominator - p-hacking with extra steps, and the exact failure D-020 exists to
stop.  So ``SearchResult`` carries ``evaluated``, the count of distinct canonical expressions the search
actually looked at, and ``declared_trials`` is what must be passed to ``research validate --prior-trials``.
Validating a mined candidate without it is not a shortcut; it is a different, wrong experiment.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations, product
from typing import Any

import pandas as pd

from beidou_alpha.mining.expr import (
    Const,
    CrossSectional,
    Expr,
    ExprError,
    Mul,
    RangePosition,
    Ratio,
    Ret,
    Squash,
    Sum,
    TakerBuy,
    Vol,
    VolumeRatio,
    ZScore,
)
from beidou_alpha.panel import Panel
from beidou_alpha.signals.base import SignalSpec, scores_to_targets


@dataclass(frozen=True)
class Candidate:
    """One expression, with the identity and cost the ledger and the verdict need."""

    expr: Expr
    hash: str
    complexity: int
    lookback: int

    @classmethod
    def of(cls, expr: Expr) -> Candidate:
        reduced = expr.canonical()
        return cls(reduced, reduced.canonical_hash(), reduced.complexity(), reduced.lookback())

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "expression": str(self.expr),
            "complexity": self.complexity,
            "lookback": self.lookback,
        }


@dataclass(frozen=True)
class SearchResult:
    """Candidates that survived structural filtering, and the trial count that must be declared."""

    candidates: tuple[Candidate, ...]
    evaluated: int  # distinct canonical expressions the search looked at
    rejected: dict[str, int] = field(default_factory=dict)

    @property
    def declared_trials(self) -> int:
        """What ``research validate --prior-trials`` must receive for any candidate from this search."""
        return self.evaluated

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "declared_trials": self.declared_trials,
            "kept": len(self.candidates),
            "rejected": dict(self.rejected),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def _momentum_family(horizons: Sequence[int], vol_windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """return / volatility, squashed: the shape momentum signals take once the units cancel."""
    for horizon, window, scale in product(horizons, vol_windows, scales):
        yield Squash(Ratio(Ret(horizon), Vol(window)), scale)


def _normalised_family(horizons: Sequence[int], windows: Sequence[int], robust: Sequence[bool]) -> Iterator[Expr]:
    """z-scored returns, then cross-sectionally ranked: a relative-strength shape."""
    for horizon, window, is_robust in product(horizons, windows, robust):
        yield CrossSectional(ZScore(Ret(horizon), window, is_robust), "rank")


def _flow_family(windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """Taker-buy imbalance and volume surprise, the two order-flow shapes this panel can express."""
    for window, scale in product(windows, scales):
        yield Squash(TakerBuy(window), scale)
        yield Squash(VolumeRatio(window), scale)


def _reversal_family(horizons: Sequence[int], windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """The negated z-score.  The first search could not express reversal at all: with no product node
    there was no way to write a minus sign, so every candidate it offered was a momentum shape."""
    for horizon, window, scale in product(horizons, windows, scales):
        yield Squash(Mul(Const(-1.0), ZScore(Ret(horizon), window)), scale)


def _range_family(windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """Position inside the trailing Donchian range - the only family that reads high and low."""
    for window in windows:
        yield CrossSectional(RangePosition(window), "rank")  # scale-free: ranking discards the magnitude
        for scale in scales:
            yield Squash(RangePosition(window), scale)


def _regime_family(
    horizons: Sequence[int],
    vol_windows: Sequence[int],
    long_windows: Sequence[int],
    scales: Sequence[float],
    momentum_window: int,
) -> Iterator[Expr]:
    """Momentum gated by a volatility-regime ratio: short-window vol over long-window vol.

    Both legs are RETURN, so the ratio is dimensionless and the product stays dimensionless.  This is the
    interaction shape - "trade the forecast harder when vol is expanding" - that a sum cannot express.

    The momentum leg's denominator must differ from the regime numerator, and that is not a detail.  The
    first version of this family reused the same window for both, so every candidate it emitted was
    ``(ret/vol_a) * (vol_a/vol_b)``, which cancels algebraically to ``ret/vol_b``: 75 of 255 candidates
    were plain momentum wearing an interaction's clothes, and the run showed it - three of them scored
    0.817, 0.817, 0.818, differing only in where their warmup NaNs fell.  The canonicaliser cannot catch
    this; it folds structural identities, not algebraic ones over division.  So the family excludes the
    cancelling combination itself.
    """
    for horizon, short, long, scale in product(horizons, vol_windows, long_windows, scales):
        if short >= long or short == momentum_window:
            continue
        yield Squash(Mul(Ratio(Ret(horizon), Vol(momentum_window)), Ratio(Vol(short), Vol(long))), scale)


def _multi_horizon_family(horizons: Sequence[int], vol_window: int, scales: Sequence[float]) -> Iterator[Expr]:
    """Equal-weighted sums of two momentum legs: the shape tsmom actually runs, reachable by search."""
    for (fast, slow), scale in product(combinations(horizons, 2), scales):
        legs = ((0.5, Ratio(Ret(fast), Vol(vol_window))), (0.5, Ratio(Ret(slow), Vol(vol_window))))
        yield Squash(Sum(legs), scale)


def enumerate_candidates(
    *,
    horizons: Sequence[int] = (24, 72, 168, 336, 720),
    vol_windows: Sequence[int] = (48, 168, 400),
    scales: Sequence[float] = (0.5, 1.0, 2.0),
    z_windows: Sequence[int] = (72, 168, 336),
    flow_windows: Sequence[int] = (4, 12, 24),
    range_windows: Sequence[int] = (24, 72, 168),
    regime_long_windows: Sequence[int] = (400, 720),
    max_complexity: int = 8,
    max_lookback: int = 1400,
) -> SearchResult:
    """Enumerate the declared families, drop malformed and duplicate trees, and count everything looked at.

    ``max_lookback`` defaults just under the venue's 1,500-bar request ceiling (E-042): a signal whose
    warmup exceeds what the live loop can fetch is not a candidate, it is a bug waiting for a restart.

    The families are structural shapes, not parameter values, and that distinction is the point.  Adding
    a fourth scale to an existing family would raise ``declared_trials`` - and so the DSR bar anything
    promoted has to clear - without adding a hypothesis.  Each family here says something the others
    cannot: reversal needs a minus sign, the range family is the only reader of high and low, the regime
    family is the only interaction, and the multi-horizon family is the only sum.
    """
    seen: dict[str, Candidate] = {}
    rejected = {"malformed": 0, "duplicate": 0, "too_complex": 0, "too_long": 0}
    evaluated = 0
    families = (
        _momentum_family(horizons, vol_windows, scales),
        _normalised_family(horizons, z_windows, (False, True)),
        _flow_family(flow_windows, scales),
        _reversal_family(horizons, z_windows, scales),
        _range_family(range_windows, scales),
        _regime_family(horizons, vol_windows, regime_long_windows, scales, vol_windows[0]),
        _multi_horizon_family(horizons, vol_windows[0], scales),
    )
    for family in families:
        while True:
            try:
                expr = next(family)
            except StopIteration:
                break
            except ExprError:  # a family that generates an illegal combination is a bug, not a candidate
                rejected["malformed"] += 1
                continue
            candidate = Candidate.of(expr)
            if candidate.hash in seen:
                rejected["duplicate"] += 1
                continue
            evaluated += 1
            if candidate.complexity > max_complexity:
                rejected["too_complex"] += 1
                continue
            if candidate.lookback > max_lookback:
                rejected["too_long"] += 1
                continue
            seen[candidate.hash] = candidate
    ordered = tuple(sorted(seen.values(), key=lambda c: (c.complexity, c.hash)))
    return SearchResult(candidates=ordered, evaluated=evaluated, rejected=rejected)


def to_signal(candidate: Candidate) -> SignalSpec:
    """Compile a candidate into the ordinary signal contract, so the existing judge applies unchanged."""

    def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
        scores = candidate.expr.evaluate(panel)
        return scores_to_targets(
            scores.clip(-1.0, 1.0),
            float(params.get("entry_threshold", 0.2)),
            hold=True,
            zero_is_exit=False,
        )

    def warmup(params: Mapping[str, Any]) -> int:
        """Derived from the tree, not declared by hand, which is the point of tracking lookback at all."""
        return candidate.lookback

    return SignalSpec(
        id=f"mined_{candidate.hash}",
        compute=compute,
        default_params={"entry_threshold": 0.2, "expression": str(candidate.expr), "hash": candidate.hash},
        description=f"mined candidate {candidate.expr}",
        warmup_bars=candidate.lookback,
        warmup=warmup,
        uses_funding=lambda params: False,
        canonical=dict,
    )
