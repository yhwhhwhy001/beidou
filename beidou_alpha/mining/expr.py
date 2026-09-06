"""A typed expression language for candidate signals (recovered from the V2 miner, rebuilt on this stack).

Four ideas are taken from `beidou_research/mining/expression_ast.py`; the rest is not.  What is taken:

1. **Dimensions are types.**  ``Mul(PRICE, PRICE)`` is refused at construction, not at evaluation.  A
   crypto price times a price is not a quantity anything trades on, and catching it when the tree is
   built is what keeps a random search from spending its budget on nonsense.
2. **Canonicalisation.**  Constant folding, identity removal, commutative operand ordering.  Two trees
   that mean the same thing reduce to the same tree.
3. **A deterministic hash of the canonical form**, so "have we tried this?" is answerable.
4. **Derived complexity and lookback**, so parsimony is measurable and warmup is never understated.

What is deliberately NOT taken: the V2 primitive library (this evaluates against
``beidou_alpha.features``, which is already validated and shared with live), its own evaluation and
scoring stack (mostly unreachable there, and this system already has one that produces verdicts), and
its persistence layer.  A mined expression compiles to an ordinary ``SignalSpec``, so walk-forward,
CPCV, the trials ledger and the D-020/D-028 verdict judge it exactly as they judge a hand-written
signal.  The miner proposes; nothing about how candidates are killed changes.

The hash matters beyond deduplication.  P12 found that a trials-ledger signature is
``(param_key, range, symbol count)``, which cannot tell two membership rules apart; an expression's
canonical hash gives a mined candidate a real identity to record, so selection pressure over a search
is counted rather than collapsed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, ClassVar

import pandas as pd

from beidou_alpha import features

if TYPE_CHECKING:  # pragma: no cover - typing only
    from beidou_alpha.panel import Panel


def _realized_vol_warmup(window: int) -> int:
    """Bars before ``features.realized_vol`` is non-NaN.

    It rolls over ``pct_change``, so it needs one bar more than the rolling window's own ``min_periods``,
    which ``features.realized_vol`` sets to ``max(2, window // 2)``.  Mirrored here rather than imported
    because the arithmetic is the thing a mined candidate must not get wrong: understating a warmup is
    E-042, where a 720-bar horizon ran on an 817-bar window.
    """
    return max(2, window // 2) + 1


def _robust_zscore_warmup(window: int) -> int:
    """Bars before ``features.robust_zscore`` is non-NaN: its MAD rolls over a deviation that is itself rolling."""
    return 2 * window - 1


class Dim(StrEnum):
    """What a node's value *is*, so illegal combinations fail when the tree is built."""

    PRICE = "price"  # a level in quote currency
    RETURN = "return"  # a dimensionless change over a horizon
    VOLUME = "volume"  # quote volume
    RATIO = "ratio"  # dimensionless, unbounded (a z-score, a ratio of like quantities)
    SCORE = "score"  # dimensionless, bounded to [-1, 1]: what a signal may emit


class ExprError(ValueError):
    """An expression that cannot mean anything, raised where it is written rather than where it runs."""


@dataclass(frozen=True)
class Expr:
    """Base node.  Subclasses declare ``dim``, evaluate on a panel, and report cost and lookback."""

    KIND: ClassVar[str] = ""

    @property
    def dim(self) -> Dim:  # pragma: no cover - abstract
        raise NotImplementedError

    def evaluate(self, panel: Panel) -> pd.DataFrame:  # pragma: no cover - abstract
        raise NotImplementedError

    def children(self) -> tuple[Expr, ...]:
        return ()

    def reads_funding(self) -> bool:
        """Does this tree read ``panel.funding``?  The live loop fetches that history only when told to.

        A method, deliberately, and never a dataclass field: ``signature()`` builds its payload from
        ``vars(self)``, so a field would rehash every existing candidate while a method cannot enter a
        signature at all.
        """
        return any(child.reads_funding() for child in self.children())

    def lookback(self) -> int:
        """Bars of history this node needs before it produces a number."""
        return max((child.lookback() for child in self.children()), default=0)

    def complexity(self) -> int:
        return 1 + sum(child.complexity() for child in self.children())

    def canonical(self) -> Expr:
        return self

    def signature(self) -> object:
        """JSON-able structure of the canonical form; the hash is taken over this."""
        payload: dict[str, object] = {"k": self.KIND}
        fields = {name: value for name, value in vars(self).items() if not isinstance(value, Expr)}
        if fields:
            payload["p"] = {k: (v.value if isinstance(v, Enum) else v) for k, v in sorted(fields.items())}
        kids = [child.signature() for child in self.children()]
        if kids:
            payload["c"] = kids
        return payload

    def canonical_hash(self) -> str:
        blob = json.dumps(self.canonical().signature(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def __str__(self) -> str:
        return self.describe()


# --- leaves ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Const(Expr):
    KIND: ClassVar[str] = "const"
    value: float

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return pd.DataFrame(self.value, index=panel.close.index, columns=panel.close.columns)

    def describe(self) -> str:
        return f"{self.value:g}"


@dataclass(frozen=True)
class Ret(Expr):
    """Simple return over ``horizon`` bars."""

    KIND: ClassVar[str] = "ret"
    horizon: int

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise ExprError("return horizon must be at least one bar")

    @property
    def dim(self) -> Dim:
        return Dim.RETURN

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return features.returns(panel.close, self.horizon)

    def lookback(self) -> int:
        return self.horizon + 1

    def describe(self) -> str:
        return f"ret({self.horizon})"


@dataclass(frozen=True)
class Vol(Expr):
    """Realised volatility of one-bar returns over ``window``."""

    KIND: ClassVar[str] = "vol"
    window: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ExprError("volatility window must be at least two bars")

    @property
    def dim(self) -> Dim:
        return Dim.RETURN

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return features.realized_vol(panel.close, self.window)

    def lookback(self) -> int:
        return _realized_vol_warmup(self.window)

    def describe(self) -> str:
        return f"vol({self.window})"


def _required(panel: Panel, field: str, node: str) -> pd.DataFrame:
    """A panel field a candidate needs, or a loud failure naming both.

    ``Panel`` declares ``quote_volume`` and ``taker_buy_quote`` optional.  A node that silently accepted
    ``None`` would evaluate to something other than what was validated, which is the KILL-027 failure:
    a signal must refuse to run on inputs it did not have when it was judged.
    """
    frame = getattr(panel, field, None)
    if frame is None:
        raise ExprError(f"{node} needs panel.{field}, which this panel does not carry")
    assert isinstance(frame, pd.DataFrame)
    return frame


@dataclass(frozen=True)
class VolumeRatio(Expr):
    """Bar quote volume over its trailing mean: dimensionless by construction."""

    KIND: ClassVar[str] = "volratio"
    window: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ExprError("volume window must be at least two bars")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return features.volume_ratio(_required(panel, "quote_volume", "volratio"), self.window)

    def lookback(self) -> int:
        return self.window + 1

    def describe(self) -> str:
        return f"volratio({self.window})"


@dataclass(frozen=True)
class TakerBuy(Expr):
    """Taker-buy share of quote volume, centred on zero (0.5 is balanced flow)."""

    KIND: ClassVar[str] = "takerbuy"
    window: int

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ExprError("taker-buy window must be at least one bar")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        buys = _required(panel, "taker_buy_quote", "takerbuy")
        total = _required(panel, "quote_volume", "takerbuy")
        return features.taker_buy_ratio(buys, total, self.window) - 0.5

    def lookback(self) -> int:
        return self.window

    def describe(self) -> str:
        return f"takerbuy({self.window})"


@dataclass(frozen=True)
class Funding(Expr):
    """Settled funding summed over a trailing window: the carry a position pays or is paid.

    ``lookback`` is ``window`` although ``features.funding_per_bar_to_8h`` uses ``min_periods=1`` and is
    non-NaN from the first bar.  That is a deliberate over-statement rather than a mirror of the feature:
    it is the number of bars the value at t reads, it sizes the live request window, and E-042 punishes
    understating a warmup only.  What it declares away is a real number rather than a NaN, because
    ``Panel.from_frames`` zero-fills funding - so the prefix is short, not missing, and the protection
    that actually holds is ``AlphaModel.eligible``'s ``min_history_bars`` (720 by default, larger than
    every window this family searches).
    """

    KIND: ClassVar[str] = "funding"
    window: int

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ExprError("funding window must be at least one bar")

    @property
    def dim(self) -> Dim:
        return Dim.RETURN

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return features.funding_per_bar_to_8h(_required(panel, "funding", "funding"), self.window)

    def lookback(self) -> int:
        return self.window

    def reads_funding(self) -> bool:
        return True

    def describe(self) -> str:
        return f"funding({self.window})"


# --- operators -------------------------------------------------------------------------------------


# --- DL-A1: five nodes over columns the panel already carries (KILL-R28's line) ---------------


@dataclass(frozen=True)
class Abs(Expr):
    """Magnitude without direction.

    What it buys: a return becomes "how far did it move", which can then be z-scored into a surprise.
    ``|price|`` is refused because a price is already non-negative, so the node would be an identity
    wearing a hat - and identities that are not folded away pollute a search's budget.
    """

    KIND: ClassVar[str] = "abs"
    inner: Expr

    def __post_init__(self) -> None:
        if self.inner.dim is Dim.PRICE:
            raise ExprError("abs of a price is the price")

    @property
    def dim(self) -> Dim:
        return self.inner.dim

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return self.inner.evaluate(panel).abs()

    def canonical(self) -> Expr:
        inner = self.inner.canonical()
        if isinstance(inner, Abs):  # |‖x‖| is ‖x‖
            return inner
        return Abs(inner)

    def describe(self) -> str:
        return f"abs({self.inner})"


@dataclass(frozen=True)
class Moment(Expr):
    """Rolling standardised skewness (k=3) or kurtosis (k=4): the shapes a mean and a variance miss.

    Standardised, so the output is dimensionless whatever went in - which is what lets it combine
    with the ratio-typed half of the language.  k=2 is refused because that is the variance and
    ``Vol`` already says it; a window shorter than 30 is refused because a third or fourth moment
    over a dozen points is mostly the estimator's own noise.
    """

    KIND: ClassVar[str] = "moment"
    inner: Expr
    order: int
    window: int

    MIN_WINDOW: ClassVar[int] = 30

    def __post_init__(self) -> None:
        if self.order not in (3, 4):
            raise ExprError("moment order must be 3 (skewness) or 4 (kurtosis); the variance is vol()")
        if self.window < self.MIN_WINDOW:
            raise ExprError(f"a moment of order {self.order} needs at least {self.MIN_WINDOW} bars to mean anything")
        if self.inner.dim is Dim.PRICE:
            raise ExprError("moments of a raw price level are not comparable across symbols")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        values = self.inner.evaluate(panel)
        rolling = values.rolling(self.window, min_periods=self.window)
        return rolling.skew() if self.order == 3 else rolling.kurt()

    def lookback(self) -> int:
        return self.window + self.inner.lookback() - 1

    def canonical(self) -> Expr:
        return Moment(self.inner.canonical(), self.order, self.window)

    def describe(self) -> str:
        return f"{'skew' if self.order == 3 else 'kurt'}({self.inner}, {self.window})"


@dataclass(frozen=True)
class Semi(Expr):
    """Downside semideviation: the root-mean-square of the negative half only.

    "Risk" in this language meant ``Vol``, which counts a rally and a crash the same.  This counts
    only the half that hurts, so a candidate can ask about drawdown risk rather than about movement.
    Refused on a SCORE or a RATIO: the semideviation of a bounded score is a number without a use.
    """

    KIND: ClassVar[str] = "semi"
    inner: Expr
    window: int

    def __post_init__(self) -> None:
        if self.inner.dim is not Dim.RETURN:
            raise ExprError("semideviation is defined here for returns; other dimensions have no downside half")
        if self.window < 2:
            raise ExprError("semideviation window must be at least two bars")

    @property
    def dim(self) -> Dim:
        return Dim.RETURN

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        values = self.inner.evaluate(panel)
        downside = values.clip(upper=0.0)
        return (downside.pow(2).rolling(self.window, min_periods=self.window).mean()) ** 0.5

    def lookback(self) -> int:
        return self.window + self.inner.lookback() - 1

    def canonical(self) -> Expr:
        return Semi(self.inner.canonical(), self.window)

    def describe(self) -> str:
        return f"semi({self.inner}, {self.window})"


@dataclass(frozen=True)
class Residual(Expr):
    """The part of a symbol's move the market did not explain (residual momentum).

    Cross-sectional, so the "market" is the equal-weighted mean **over the panel's reference
    population** and not over whichever columns the caller happened to load.  That is P1-01 / DL-Q1's
    contract: a promoted candidate must not be able to re-open the defect the hand-written signals
    just closed, and the only way to guarantee that is for the node to read ``panel.reference`` the
    same way ``CrossSectional`` does.
    """

    KIND: ClassVar[str] = "residual"
    inner: Expr
    window: int

    def __post_init__(self) -> None:
        if self.inner.dim is not Dim.RETURN:
            raise ExprError("residualisation needs a return; a level has no market to be regressed on")
        if self.window < 30:
            raise ExprError("a rolling beta needs at least 30 bars")

    @property
    def dim(self) -> Dim:
        return Dim.RETURN

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        values = self.inner.evaluate(panel)
        eligible = features.within_reference(values, panel.reference)
        market = eligible.mean(axis=1)
        beta = features.rolling_beta(values, market, self.window)
        return features.residual_returns(values, market, beta)

    def lookback(self) -> int:
        return self.window + self.inner.lookback() - 1

    def canonical(self) -> Expr:
        return Residual(self.inner.canonical(), self.window)

    def describe(self) -> str:
        return f"residual({self.inner}, {self.window})"


@dataclass(frozen=True)
class TradeSize(Expr):
    """Average trade size against its own trailing mean.

    The one thing ``trades`` says that ``quote_volume`` does not: whether the same volume arrived as
    a few large prints or many small ones.  ``quote_volume / trades`` is a level in quote currency,
    so it is divided by its own trailing mean to become dimensionless and comparable across symbols.
    """

    KIND: ClassVar[str] = "tradesize"
    window: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ExprError("trade-size window must be at least two bars")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        counts = _required(panel, "trades", "tradesize")
        volume = _required(panel, "quote_volume", "tradesize")
        # A bar with no prints is a real bar on a thin symbol, not a division to crash on.
        average = volume.where(counts > 0) / counts.where(counts > 0)
        return features.volume_ratio(average, self.window)

    def lookback(self) -> int:
        return self.window + 1

    def describe(self) -> str:
        return f"tradesize({self.window})"


@dataclass(frozen=True)
class Ratio(Expr):
    """``numerator / denominator`` for two nodes of the *same* dimension, which cancels it.

    This is the only way to reach RATIO from dimensioned quantities, and it is why ``ret / vol``
    is expressible while ``ret / volume`` is not.
    """

    KIND: ClassVar[str] = "ratio"
    numerator: Expr
    denominator: Expr

    def __post_init__(self) -> None:
        if self.numerator.dim is not self.denominator.dim:
            raise ExprError(f"cannot divide {self.numerator.dim.value} by {self.denominator.dim.value}")
        if self.numerator.dim is Dim.SCORE:
            raise ExprError("a bounded score is not a quantity to divide")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def children(self) -> tuple[Expr, ...]:
        return (self.numerator, self.denominator)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        bottom = self.denominator.evaluate(panel)
        return self.numerator.evaluate(panel) / bottom.where(bottom.abs() > 0)

    def canonical(self) -> Expr:
        top, bottom = self.numerator.canonical(), self.denominator.canonical()
        if isinstance(bottom, Const) and bottom.value == 1.0:
            return top
        return Ratio(top, bottom)

    def describe(self) -> str:
        return f"({self.numerator} / {self.denominator})"


@dataclass(frozen=True)
class ZScore(Expr):
    """Rolling z-score.  Any dimension in, RATIO out, because the units cancel."""

    KIND: ClassVar[str] = "z"
    inner: Expr
    window: int
    robust: bool = False

    def __post_init__(self) -> None:
        if self.window < 3:
            raise ExprError("z-score window must be at least three bars")
        if self.inner.dim is Dim.SCORE:
            raise ExprError("a bounded score is already normalised; z-scoring it says nothing new")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        inner = self.inner.evaluate(panel)
        if self.robust:
            return features.robust_zscore(inner, self.window)
        return features.zscore(inner, self.window)

    def lookback(self) -> int:
        extra = _robust_zscore_warmup(self.window) if self.robust else self.window
        return self.inner.lookback() + extra

    def canonical(self) -> Expr:
        return ZScore(self.inner.canonical(), self.window, self.robust)

    def describe(self) -> str:
        return f"{'rz' if self.robust else 'z'}({self.inner}, {self.window})"


@dataclass(frozen=True)
class CrossSectional(Expr):
    """Rank or z-score across symbols within a bar; RATIO in, RATIO out."""

    KIND: ClassVar[str] = "cs"
    inner: Expr
    method: str = "rank"

    def __post_init__(self) -> None:
        if self.method not in ("rank", "zscore", "demean"):
            raise ExprError(f"unknown cross-sectional method {self.method!r}")
        if self.inner.dim is Dim.PRICE:
            raise ExprError("ranking raw prices across symbols compares unrelated scales")

    @property
    def dim(self) -> Dim:
        return Dim.SCORE if self.method == "rank" else Dim.RATIO

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        # P1-01 / DL-Q1: the statistic is taken over the panel's reference population, so a
        # promoted candidate cannot re-open the defect the hand-written signals just closed.
        inner = self.inner.evaluate(panel)
        if self.method == "rank":
            return features.cross_sectional_rank(inner, panel.reference)
        if self.method == "zscore":
            return features.cross_sectional_zscore(inner, panel.reference)
        return features.demean_cross_section(inner, panel.reference)

    def canonical(self) -> Expr:
        return CrossSectional(self.inner.canonical(), self.method)

    def describe(self) -> str:
        return f"cs_{self.method}({self.inner})"


@dataclass(frozen=True)
class Squash(Expr):
    """``tanh(inner / scale)``: the only route from an unbounded RATIO to a bounded SCORE."""

    KIND: ClassVar[str] = "squash"
    inner: Expr
    scale: float = 1.0

    def __post_init__(self) -> None:
        if self.inner.dim is not Dim.RATIO:
            raise ExprError(f"squash needs a dimensionless ratio, got {self.inner.dim.value}")
        if not self.scale > 0:
            raise ExprError("squash scale must be positive")

    @property
    def dim(self) -> Dim:
        return Dim.SCORE

    def children(self) -> tuple[Expr, ...]:
        return (self.inner,)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        import numpy as np

        return features.apply_numpy(self.inner.evaluate(panel) / self.scale, np.tanh)

    def canonical(self) -> Expr:
        return Squash(self.inner.canonical(), self.scale)

    def describe(self) -> str:
        return f"squash({self.inner}, {self.scale:g})"


@dataclass(frozen=True)
class Sum(Expr):
    """Weighted sum of same-dimension nodes.  Commutative, so canonicalisation sorts the terms."""

    KIND: ClassVar[str] = "sum"
    terms: tuple[tuple[float, Expr], ...]

    def __post_init__(self) -> None:
        if not self.terms:
            raise ExprError("a sum needs at least one term")
        dims = {expr.dim for _, expr in self.terms}
        if len(dims) > 1:
            raise ExprError(f"cannot add {sorted(d.value for d in dims)}")

    @property
    def dim(self) -> Dim:
        return self.terms[0][1].dim

    def children(self) -> tuple[Expr, ...]:
        return tuple(expr for _, expr in self.terms)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        total = None
        for weight, expr in self.terms:
            piece = expr.evaluate(panel) * weight
            total = piece if total is None else total.add(piece, fill_value=0.0)
        assert total is not None
        return total

    def canonical(self) -> Expr:
        merged: dict[str, tuple[float, Expr]] = {}
        for weight, expr in self.terms:
            reduced = expr.canonical()
            if weight == 0.0:
                continue
            key = reduced.canonical_hash()
            if key in merged:  # x + x folds to 2x, which is the identity a search keeps rediscovering
                merged[key] = (merged[key][0] + weight, reduced)
            else:
                merged[key] = (weight, reduced)
        kept = [(w, e) for w, e in merged.values() if w != 0.0]
        if not kept:
            return Const(0.0)
        if len(kept) == 1 and kept[0][0] == 1.0:
            return kept[0][1]
        ordered = tuple(sorted(kept, key=lambda item: item[1].canonical_hash()))
        return Sum(ordered)

    def signature(self) -> object:
        return {
            "k": self.KIND,
            "t": [[weight, expr.signature()] for weight, expr in self.terms],
        }

    def describe(self) -> str:
        return " + ".join(f"{w:g}*{e}" for w, e in self.terms)


@dataclass(frozen=True)
class Mul(Expr):
    """Product of two dimensionless nodes.  Commutative, so canonicalisation sorts the operands.

    Restricted to RATIO x RATIO on purpose.  Dimensionless times dimensionless is dimensionless, which is
    the only product that stays interpretable; a price times a volume is the kind of term a search will
    happily fit and nobody can read.  Two shapes become expressible through it that the first search could
    not express at all: negation (``Mul(Const(-1), x)``, i.e. mean reversion) and gating (a forecast scaled
    by a regime measure).
    """

    KIND: ClassVar[str] = "mul"
    left: Expr
    right: Expr

    def __post_init__(self) -> None:
        for side in (self.left, self.right):
            if side.dim is not Dim.RATIO:
                raise ExprError(f"mul needs dimensionless operands, got {side.dim.value}")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def children(self) -> tuple[Expr, ...]:
        return (self.left, self.right)

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        return self.left.evaluate(panel) * self.right.evaluate(panel)

    def canonical(self) -> Expr:
        left, right = self.left.canonical(), self.right.canonical()
        if isinstance(left, Const) and isinstance(right, Const):
            return Const(left.value * right.value)
        for a, b in ((left, right), (right, left)):
            if isinstance(a, Const) and a.value == 0.0:
                return Const(0.0)
            if isinstance(a, Const) and a.value == 1.0:
                return b
        ordered = sorted((left, right), key=lambda node: node.canonical_hash())
        return Mul(ordered[0], ordered[1])

    def describe(self) -> str:
        return f"({self.left} * {self.right})"


@dataclass(frozen=True)
class RangePosition(Expr):
    """Where the close sits inside its trailing Donchian range, centred so 0 is mid-range.

    ``2*(close - lower)/(upper - lower) - 1``, over the *previous* ``window`` bars (``features.donchian``
    shifts before rolling), so it is causal.  This is the only family that reads high and low, which is
    why it earns a node rather than another parameter on an existing one.
    """

    KIND: ClassVar[str] = "rangepos"
    window: int

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ExprError("range window must be at least two bars")

    @property
    def dim(self) -> Dim:
        return Dim.RATIO

    def evaluate(self, panel: Panel) -> pd.DataFrame:
        upper, lower = features.donchian(panel.high, panel.low, self.window)
        span = upper - lower
        return ((panel.close - lower) / span.where(span > 0)) * 2.0 - 1.0

    def lookback(self) -> int:
        return self.window + 1

    def describe(self) -> str:
        return f"rangepos({self.window})"
