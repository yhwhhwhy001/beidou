"""Carry enters the search space (DL-P17-01/02/03), and the 225 that were there before do not move.

Three things are asserted here that the rest of the suite cannot assert.

``T-P17-01`` is the identity guard.  ``tests/fixtures/mining_baseline_hashes.json`` was generated from
unmodified ``db9efd9`` **before** a ``Funding`` node existed, which is the only moment it could be
generated honestly, and every edit in this delivery re-runs against it.  It is an ORDERED comparison:
the fixture's array is ``(complexity, hash)`` order, so a set comparison would silently stop testing the
invariant that P14's shortlist is still reproducible.

``T-P17-02`` onward pin the leaf's dimension contract.  ``Funding`` is ``Dim.RETURN``, which is what
forces every shape in ``_funding_family`` through ``Ratio(Funding, Vol)`` instead of squashing a rate
directly - the dimension system refuses the shortcut rather than merely discouraging it.

``T-P17-06`` is the one with a bug behind it.  ``to_signal`` hardcoded ``uses_funding=lambda params:
False`` at ``search.py:242``, so a mined candidate that read ``panel.funding`` would have told the live
loop it needed no funding history and then run on whatever it was handed.  That is the KILL-027 shape,
and D-023 exists to refuse it at startup rather than discover it in attribution.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from beidou_alpha import features
from beidou_alpha.mining import (
    Const,
    CrossSectional,
    Dim,
    ExprError,
    Funding,
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
    enumerate_candidates,
    to_signal,
)
from beidou_alpha.mining.search import Candidate
from beidou_alpha.panel import Panel

BARS = 900
SYMBOLS = ("A", "B", "C")


def _panel(*, with_funding: bool) -> Panel:
    """The suite's usual synthetic panel, optionally carrying settlements every eight bars."""
    index = pd.date_range("2024-01-01", periods=BARS, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    frames = {}
    for offset, symbol in enumerate(SYMBOLS):
        steps = rng.normal(0.0, 0.004, BARS).cumsum()
        close = pd.Series(100.0 * np.exp(steps), index=index) * (1 + offset * 0.1)
        volume = pd.Series(rng.lognormal(10.0, 0.3, BARS), index=index)
        frames[symbol] = pd.DataFrame(
            {
                "open": close.shift(1).fillna(close.iloc[0]),
                "high": close * 1.002,
                "low": close * 0.998,
                "close": close,
                "volume": volume,
                "quote_volume": volume * close,
                "taker_buy_quote": volume * close * rng.uniform(0.4, 0.6, BARS),
            },
            index=index,
        )
    if not with_funding:
        return Panel.from_frames(frames, "1h")
    # Settlement stamps land a few milliseconds past the hour, as the venue's really do (D-034).
    stamps = index[::8] + pd.Timedelta(milliseconds=17)
    funding = pd.DataFrame(
        {symbol: rng.normal(0.0001, 0.0003, len(stamps)) for symbol in SYMBOLS},
        index=pd.DatetimeIndex(stamps),
    )
    return Panel.from_frames(frames, "1h", funding=funding)


@pytest.fixture(scope="module")
def funding_panel() -> Panel:
    return _panel(with_funding=True)


def test_the_existing_search_space_is_bit_for_bit_what_p14_recorded(fixtures_dir: Path) -> None:
    """T-P17-01: the 225 do not move, in value or in order, however the miner grows around them."""
    baseline = json.loads((fixtures_dir / "mining_baseline_hashes.json").read_text(encoding="utf-8"))

    # Both caps, because the delivery raised one of them: at the recorded cap of 8 and at today's 10, the
    # pre-funding space must be the same 225.  That is what makes the raise provably inert rather than
    # merely believed to be.
    for result in (
        enumerate_candidates(include_funding=False),
        enumerate_candidates(include_funding=False, max_complexity=8),
    ):
        assert [candidate.hash for candidate in result.candidates] == baseline["hashes"]
        assert {candidate.hash: str(candidate.expr) for candidate in result.candidates} == baseline["expressions"]
        assert result.evaluated == baseline["evaluated"]
        assert result.rejected == baseline["rejected"]

    # The grids are part of the recorded search space: widening one silently would move the 225 without
    # touching a line of expr.py.  Iterating the FIXTURE's keys tolerates parameters added later.
    parameters = inspect.signature(enumerate_candidates).parameters
    pinned = {name: value for name, value in baseline["defaults"].items() if name != "max_complexity"}
    declared = {
        name: list(parameter.default) if isinstance(parameter.default, tuple) else parameter.default
        for name, parameter in parameters.items()
        if name in pinned
    }
    assert declared == pinned

    # max_complexity is the one recorded default this delivery deliberately moves, so it is asserted
    # explicitly rather than pinned to the fixture.  The baseline records rejected["too_complex"] == 0,
    # so nothing that existed sat near the cap; the loop above proves the raise moves nothing.
    assert baseline["defaults"]["max_complexity"] == 8
    assert parameters["max_complexity"].default == 10


# --- the cost of the new family ---------------------------------------------------------------------


def test_the_funding_family_is_on_by_default_and_costs_what_was_pre_registered() -> None:
    """T-P17-08: the delta IS the trial charge, and an equal `rejected` is what catches a truncation."""
    without = enumerate_candidates(include_funding=False)
    full = enumerate_candidates()

    assert full.evaluated > without.evaluated  # on by default: _resolve_mined enumerates bare
    assert full.evaluated - without.evaluated <= 60  # the pre-registered budget
    assert {c.hash for c in without.candidates} <= {c.hash for c in full.candidates}
    # One ExprError closes a generator and silently discards the rest of the family while counting a
    # single `malformed`, so an unchanged rejected dict is the assertion that the family ran to the end.
    assert full.rejected == without.rejected
    assert full.declared_trials == full.evaluated


def test_all_three_shapes_are_reachable_in_both_signs() -> None:
    """T-P17-09: this is where the max_complexity decision becomes visible instead of implicit."""
    carry = {str(c.expr) for c in enumerate_candidates().candidates if "funding(" in str(c.expr)}
    assert carry

    def present(predicate: Callable[[str], bool]) -> bool:
        return any(predicate(text) for text in carry)

    assert present(lambda t: t.startswith("squash((funding(") and "-1 *" not in t)
    assert present(lambda t: t.startswith("squash((-1 * (funding("))
    assert present(lambda t: t.startswith("cs_rank((funding("))
    assert present(lambda t: t.startswith("cs_rank((-1 * (funding("))
    # The negated product is a ten-node tree: at max_complexity=8 it vanishes into rejected["too_complex"]
    # after being charged as a trial, which is exactly what this assertion refuses to allow silently.
    assert present(lambda t: "*" in t and "funding(" in t and "ret(" in t and "-1 *" in t)
    assert present(lambda t: "*" in t and "funding(" in t and "ret(" in t and "-1 *" not in t)
    assert present(lambda t: "+" in t and "funding(" in t and "ret(" in t)

    # Funding never reaches the top of a tree un-normalised: Dim.RETURN is what forbids it.
    assert not any(t.startswith(("cs_rank(funding(", "squash(funding(")) for t in carry)


def test_every_funding_window_is_a_whole_number_of_settlements() -> None:
    """T-P17-10: funding settles every eight hours; a straddling window averages a fraction of one."""
    windows = [
        int(match)
        for candidate in enumerate_candidates().candidates
        for match in re.findall(r"funding\((\d+)\)", str(candidate.expr))
    ]
    assert windows
    assert all(window % 8 == 0 for window in windows)


def test_enumeration_is_deterministic_and_keyword_only() -> None:
    """T-P17-11: the property `_resolve_mined` relies on to re-derive an id without persisting one."""
    first, second = enumerate_candidates(), enumerate_candidates()
    assert [c.hash for c in first.candidates] == [c.hash for c in second.candidates]
    assert first.evaluated == second.evaluated
    assert first.rejected == second.rejected
    parameters = inspect.signature(enumerate_candidates).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())


# --- the leaf ---------------------------------------------------------------------------------------


def test_funding_is_a_return_so_every_shape_must_divide_it_by_volatility() -> None:
    """T-P17-02: the dimension system refuses the shortcut rather than discouraging it."""
    assert Funding(24).dim is Dim.RETURN
    assert Funding(24).lookback() == 24
    assert str(Funding(24)) == "funding(24)"

    carry = Ratio(Funding(24), Vol(48))
    assert carry.dim is Dim.RATIO
    assert carry.lookback() == 25  # Vol(48) needs 25; the funding window is shorter
    assert Ratio(Funding(168), Vol(48)).lookback() == 168

    with pytest.raises(ExprError, match="dimensionless ratio"):
        Squash(Funding(24), 1.0)  # a rate is not a score
    with pytest.raises(ExprError, match="dimensionless operands"):
        Mul(Const(-1.0), Funding(24))
    with pytest.raises(ExprError, match="cannot divide"):
        Ratio(Funding(24), VolumeRatio(24))
    with pytest.raises(ExprError, match="at least one bar"):
        Funding(0)


def test_the_leaf_is_exactly_the_shared_feature(funding_panel: Panel) -> None:
    """T-P17-03: values, not structure - and the declared lookback is deliberately not NaN-backed."""
    assert funding_panel.funding is not None
    pd.testing.assert_frame_equal(
        Funding(24).evaluate(funding_panel),
        features.funding_per_bar_to_8h(funding_panel.funding, 24),
    )
    # min_periods=1, so the first bar already carries a number.  Pinned so a change to the feature shows
    # up here as a visible test change rather than a silent shift in every mined carry candidate.
    assert bool(Funding(168).evaluate(funding_panel).iloc[0].notna().all())


def test_a_candidate_refuses_a_panel_that_carries_no_funding() -> None:
    """T-P17-04: KILL-027 at the leaf - refuse the input rather than evaluate something else."""
    panel = _panel(with_funding=False)
    assert panel.funding is None
    with pytest.raises(ExprError, match=r"panel\.funding"):
        Funding(24).evaluate(panel)


def test_reads_funding_recurses_through_every_composite() -> None:
    """T-P17-05: the predicate is a property of the tree, not of the node it happens to start at."""
    carry = Ratio(Funding(24), Vol(48))
    momentum = Ratio(Ret(72), Vol(48))
    assert Funding(24).reads_funding() is True
    assert Squash(carry, 1.0).reads_funding() is True
    assert CrossSectional(carry, "rank").reads_funding() is True
    assert Mul(Const(-1.0), carry).reads_funding() is True
    assert Squash(Sum(((0.5, momentum), (0.5, carry))), 1.0).reads_funding() is True
    assert ZScore(Funding(24), 72).reads_funding() is True

    assert Squash(momentum, 1.0).reads_funding() is False
    for leaf in (Const(1.0), Ret(24), Vol(48), VolumeRatio(12), TakerBuy(4), RangePosition(24)):
        assert leaf.reads_funding() is False


# --- the signal contract ----------------------------------------------------------------------------


def test_the_compiled_spec_declares_the_funding_it_actually_reads() -> None:
    """T-P17-06: ``to_signal`` hardcoded False, so a carry candidate lied to the D-023 startup gate."""
    carry = Candidate.of(Squash(Ratio(Funding(24), Vol(48)), 1.0))
    momentum = Candidate.of(Squash(Ratio(Ret(72), Vol(48)), 1.0))
    assert to_signal(carry).needs_funding({}) is True
    assert to_signal(momentum).needs_funding({}) is False


def test_a_carry_candidate_compiles_to_the_ordinary_signal_contract(funding_panel: Panel) -> None:
    """T-P17-07: the judge applies to a carry candidate unchanged, exactly as to a momentum one."""
    candidate = Candidate.of(Squash(Ratio(Funding(72), Vol(48)), 1.0))
    spec = to_signal(candidate)
    assert spec.id == f"mined_{candidate.hash}"
    assert spec.warmup_for({}) == candidate.lookback
    targets = spec.compute(funding_panel, spec.default_params)
    assert list(targets.columns) == list(funding_panel.close.columns)
    assert targets.abs().max().max() <= 1.0
    assert targets.notna().any().any()
