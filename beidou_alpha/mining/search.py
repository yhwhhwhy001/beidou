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

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations, product
from typing import Any

import pandas as pd

from beidou_alpha.mining.expr import (
    Abs,
    Const,
    CrossSectional,
    Expr,
    ExprError,
    Funding,
    Moment,
    Mul,
    RangePosition,
    Ratio,
    Residual,
    Ret,
    Semi,
    Squash,
    Sum,
    TakerBuy,
    TradeSize,
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

    @property
    def space_digest(self) -> str:
        """R2: WHICH space this was, so a second enumeration of it can be refused rather than charged.

        The identity is the set of canonical expression hashes, not the parameters that produced it.
        Two different parameterisations that enumerate the same set are the same hypothesis space and
        must compare equal; the same parameters against a widened node table are not, and do not.

        Not the same thing as ``evaluated``, which is a count and cannot tell two 514-wide spaces apart,
        and deliberately not written into ``TrialRecord.search_space_version`` - see the note in
        ``research mine``, where changing that field would re-price every existing mined row.
        """
        return hashlib.sha256("|".join(sorted(c.hash for c in self.candidates)).encode("utf-8")).hexdigest()[:16]

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


def _funding_family(
    windows: Sequence[int],
    vol_window: int,
    scales: Sequence[float],
    horizons: Sequence[int],
    interaction_scale: float,
) -> Iterator[Expr]:
    """Carry, and carry crossed with momentum: the only family that reads ``panel.funding``.

    Every shape divides the funding leg by ``Vol`` first, which the dimension system enforces rather than
    suggests - ``Funding`` is RETURN, so ``Squash``, ``Mul`` and a mixed ``Sum`` all refuse it raw.  The
    interaction reuses one ``vol_window`` on both legs, which is safe here and was not in
    ``_regime_family``: this is ``ret * funding / vol**2``, where nothing appears in both a numerator and a
    denominator, so there is no algebraic cancellation for the canonicaliser to miss.

    Three things a reader of the shortlist has to know.

    **Both signs are emitted, so a funding candidate is always near the top.**  ``cross_sectional_rank``
    and ``tanh`` are both odd, so each short shape is the exact mirror of its long one and the mirror of
    the worst candidate is the best.  "A carry expression ranked first" is therefore not information; only
    its margin over the baseline is.

    **The interaction is not a test of tsmom's crowding modifier.**  That modifier is a thresholded,
    one-sided, shrink-only multiplier in {1.0, 0.5} on a cross-sectional *rank* of funding, with
    ``min_periods=window`` and an observed mask.  This is a continuous, symmetric, sign-flipping,
    amplifying product of raw vol-scaled quantities.  No setting of (h, v, w, s, sign) recovers it - the
    positive sign amplifies exactly where the modifier shrinks.  A hit here is an analogy to that
    evidence, never a replication of it.

    **Shape three carries a positive carry weight**, i.e. "go long what pays the most funding", which is
    the opposite of ``carry.py``'s prior.  That is the contract's choice, not an oversight; the negative
    version is reachable through the short arm of shape one.

    Two combinations are excluded by construction.  ``CrossSectional(Funding(w), "rank")`` type-checks -
    ``CrossSectional`` bans only PRICE - but ranks un-normalised carry, which is ``carry.py``'s already
    killed rank mode.  And ``Mul(Ratio(Funding(w), Vol(a)), Ratio(Vol(a), Vol(b)))`` is ``_regime_family``'s
    cancellation verbatim: it collapses to ``funding(w) / vol(b)``, which is shape one with a different
    denominator.
    """
    for window in windows:
        carry = Ratio(Funding(window), Vol(vol_window))
        # The minus sign goes inside the Ratio because Mul refuses a non-RATIO operand and a ranked
        # expression is a SCORE: negating the CrossSectional would raise, and an ExprError leaves this
        # generator closed, silently truncating the rest of the family for one malformed count.
        short_carry = Mul(Const(-1.0), carry)
        yield CrossSectional(carry, "rank")  # scale-free: ranking discards the magnitude
        yield CrossSectional(short_carry, "rank")
        for scale in scales:
            yield Squash(carry, scale)
            yield Squash(short_carry, scale)
        for horizon in horizons:
            momentum = Ratio(Ret(horizon), Vol(vol_window))
            # One operand order only: Mul.canonical sorts by hash, so the reverse is the same tree.
            interaction = Mul(momentum, carry)
            yield Squash(interaction, interaction_scale)
            yield Squash(Mul(Const(-1.0), interaction), interaction_scale)
            yield Squash(Sum(((0.5, momentum), (0.5, carry))), interaction_scale)


# --- DL-A1: five families over the panel's existing columns ------------------------------------
#
# One family per new node, and each says something none of the seven before it could.  All of them
# emit both signs wherever the outer operator is odd (`cross_sectional_rank` and `tanh` both are), so
# - exactly as `_funding_family` records - "a candidate of this family ranked first" is not
# information; the mirror of the worst tree is the best one.  Only the margin over the baseline is.


def _surprise_family(horizons: Sequence[int], z_windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """How *far* a symbol moved, with the direction thrown away.

    The hypothesis the language could not state before ``Abs``: an unusually large move is itself a
    predictor, whichever way it went.  ``|ret|`` z-scored against its own history is the "surprise",
    and both signs are searched because the crypto literature disagrees with itself about whether a
    shock continues or reverts - which is the honest reason to let the data answer.
    """
    for horizon in horizons:
        surprise = ZScore(Abs(Ret(horizon)), z_windows[0])
        for window in z_windows:
            yield CrossSectional(ZScore(Abs(Ret(horizon)), window), "rank")
        for scale in scales:
            yield Squash(surprise, scale)
            yield Squash(Mul(Const(-1.0), surprise), scale)


def _shape_family(moment_windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """Skewness and kurtosis of one-bar returns: the two shapes a mean and a variance cannot see.

    A distribution that has been paying small gains and taking rare large losses looks identical to
    ``Vol`` and quite different to ``Moment``.  Both orders and both signs; nothing here has a prior
    strong enough to fix one.
    """
    for window in moment_windows:
        for order in (3, 4):
            shape = Moment(Ret(1), order, window)
            yield CrossSectional(shape, "rank")
            for scale in scales:
                yield Squash(shape, scale)
                yield Squash(Mul(Const(-1.0), shape), scale)


def _downside_family(horizons: Sequence[int], semi_windows: Sequence[int], scales: Sequence[float]) -> Iterator[Expr]:
    """Momentum scaled by the risk that hurts, instead of by all movement.

    ``_momentum_family`` divides a return by ``Vol``, which charges a rally and a crash the same
    amount.  This divides by ``Semi``, so a symbol that rose in a straight line is not penalised for
    having risen.  It is the same hypothesis measured with a different denominator, which is exactly
    the sort of thing a search should settle rather than an author.
    """
    for horizon in horizons:
        for window in semi_windows:
            scaled = Ratio(Ret(horizon), Semi(Ret(1), window))
            yield CrossSectional(scaled, "rank")
            for scale in scales:
                yield Squash(scaled, scale)
                yield Squash(Mul(Const(-1.0), scaled), scale)


def _residual_family(
    horizons: Sequence[int], residual_windows: Sequence[int], vol_window: int, scales: Sequence[float]
) -> Iterator[Expr]:
    """Residual momentum: the part of the move the market did not explain.

    tsmom trades the whole move, so in a market where everything rises together it is largely long
    beta.  Residualising against the cross-sectional mean asks whether what is left over - the part
    specific to the symbol - carries the edge.  The market is taken over ``panel.reference``, so this
    family inherits P1-01's contract rather than re-opening it.
    """
    for horizon in horizons:
        for window in residual_windows:
            residual = Ratio(Residual(Ret(horizon), window), Vol(vol_window))
            yield CrossSectional(residual, "rank")
            for scale in scales:
                yield Squash(residual, scale)
                yield Squash(Mul(Const(-1.0), residual), scale)


def _print_size_family(
    trade_windows: Sequence[int], z_windows: Sequence[int], horizons: Sequence[int], scales: Sequence[float]
) -> Iterator[Expr]:
    """Whether the volume arrived as a few large prints or many small ones.

    The only hypothesis in this batch that reads a column no node had ever read (``panel.trades``).
    ``_flow_family`` knows how much traded and who lifted; it cannot tell one 10 BTC print from a
    thousand 0.01 ones, and the folklore that large prints lead is at least worth one family.  The
    interaction with momentum is included because "big prints in the direction of the trend" is the
    specific version of the claim that would be interesting.
    """
    for window in trade_windows:
        size = ZScore(TradeSize(window), z_windows[0])
        yield CrossSectional(size, "rank")
        for scale in scales:
            yield Squash(size, scale)
            yield Squash(Mul(Const(-1.0), size), scale)
        for horizon in horizons:
            interaction = Mul(Ratio(Ret(horizon), Vol(z_windows[0])), size)
            yield Squash(interaction, scales[0])
            yield Squash(Mul(Const(-1.0), interaction), scales[0])


def enumerate_candidates(
    *,
    horizons: Sequence[int] = (24, 72, 168, 336, 720),
    vol_windows: Sequence[int] = (48, 168, 400),
    scales: Sequence[float] = (0.5, 1.0, 2.0),
    z_windows: Sequence[int] = (72, 168, 336),
    flow_windows: Sequence[int] = (4, 12, 24),
    range_windows: Sequence[int] = (24, 72, 168),
    regime_long_windows: Sequence[int] = (400, 720),
    include_funding: bool = True,
    funding_windows: Sequence[int] = (24, 72, 168),
    funding_horizons: Sequence[int] = (72, 168),
    funding_scale: float = 1.0,
    # DL-A1 windows.  Reused from the families above wherever the meaning carries over, so the space
    # grows by hypotheses rather than by a fourth value of something already searched (KILL-P6).
    moment_windows: Sequence[int] = (168, 336),
    semi_windows: Sequence[int] = (72, 168),
    residual_windows: Sequence[int] = (168, 336),
    trade_windows: Sequence[int] = (24, 72),
    include_panel_nodes: bool = True,
    max_complexity: int = 10,
    max_lookback: int = 1400,
) -> SearchResult:
    """Enumerate the declared families, drop malformed and duplicate trees, and count everything looked at.

    ``max_lookback`` defaults just under the venue's 1,500-bar request ceiling (E-042): a signal whose
    warmup exceeds what the live loop can fetch is not a candidate, it is a bug waiting for a restart.

    The families are structural shapes, not parameter values, and that distinction is the point.  Adding
    a fourth scale to an existing family would raise ``declared_trials`` - and so the DSR bar anything
    promoted has to clear - without adding a hypothesis.  Each family here says something the others
    cannot: reversal needs a minus sign, the range family is the only reader of high and low, the regime
    family is the only interaction, the multi-horizon family is the only sum, and the funding family
    expresses carry - the one panel input no node could read until ``Funding`` existed.

    ``include_funding`` is a search-space parameter and not a panel probe, deliberately: this function
    stays deterministic and touches no data, which is the property ``research_cmd._resolve_mined`` relies
    on to re-derive a ``mined_<hash>`` id without persisting anything.  The caller narrows the space when
    its panel carries no funding.

    ``max_complexity`` is 10 rather than 8 because the contract's signed momentum-times-carry product is a
    ten-node tree - the minus sign costs two nodes.  The raise is inert on what came before, and the
    reason is monotonicity, not headroom: relaxing an upper bound can only admit trees, never drop them,
    and the recorded baseline has ``rejected["too_complex"] == 0``, so nothing that existed exceeded 8.
    Headroom is precisely what there is none of - 75 of the 225, the single largest bucket, sit *at*
    complexity 8 - so the next momentum-shaped family will land on the cap again.  Asserted rather than
    argued: the identity test enumerates the pre-``Funding`` space at both 8 and 10 and requires the same
    225 hashes in the same order.  At 8 the six negated interaction trees are charged to
    ``declared_trials`` and then dropped unscored, which is the worst of both.
    """
    seen: dict[str, Candidate] = {}
    rejected = {"malformed": 0, "duplicate": 0, "too_complex": 0, "too_long": 0}
    evaluated = 0
    families: tuple[Iterator[Expr], ...] = (
        _momentum_family(horizons, vol_windows, scales),
        _normalised_family(horizons, z_windows, (False, True)),
        _flow_family(flow_windows, scales),
        _reversal_family(horizons, z_windows, scales),
        _range_family(range_windows, scales),
        _regime_family(horizons, vol_windows, regime_long_windows, scales, vol_windows[0]),
        _multi_horizon_family(horizons, vol_windows[0], scales),
    )
    if include_funding:
        families = (
            *families,
            _funding_family(funding_windows, vol_windows[0], scales, funding_horizons, funding_scale),
        )
    if include_panel_nodes:
        # Appended, never interleaved: a family's position does not enter a candidate's hash, but the
        # order candidates are *first seen* in decides which duplicate is kept, and every id already in
        # the ledger has to keep resolving (T-A1-3).
        families = (
            *families,
            _surprise_family(horizons, z_windows, scales),
            _shape_family(moment_windows, scales),
            _downside_family(horizons, semi_windows, scales),
            _residual_family(horizons, residual_windows, vol_windows[0], scales),
            _print_size_family(trade_windows, z_windows, horizons, scales),
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

    # Derived from the candidate, never from a flag threaded in from the caller: ``research mine`` and
    # ``_resolve_mined`` build the same id by different routes, and ``register`` overwrites by id without
    # comparing.  A hardcoded ``False`` here is the KILL-027 shape - a signal telling the live loop it
    # needs no funding history and then reading ``panel.funding`` anyway (D-023).
    reads_funding = candidate.expr.reads_funding()

    return SignalSpec(
        id=f"mined_{candidate.hash}",
        compute=compute,
        default_params={"entry_threshold": 0.2, "expression": str(candidate.expr), "hash": candidate.hash},
        description=f"mined candidate {candidate.expr}",
        warmup_bars=candidate.lookback,
        warmup=warmup,
        uses_funding=lambda params: reads_funding,
        canonical=dict,
    )
