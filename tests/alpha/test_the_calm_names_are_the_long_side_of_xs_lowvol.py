"""G12's signal does what its pre-registration says it does, before anything is run on real data.

Causality and the [-1, 1] bound come from `test_signal_suite.py`, which parametrises over every registered
signal.  This file holds the three things that suite cannot know: which side the calm names are on, that the
rank is taken over the reference population and not over whatever columns were loaded (DL-Q1), and that the
grid a stage-1 run would charge is the pre-registered one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from beidou_alpha.features import realized_vol_warmup
from beidou_alpha.signals import SIGNALS
from beidou_alpha.signals.xs_lowvol import XsLowvolParams, xs_lowvol_scores
from beidou_cli.research_grids import DEFAULT_GRIDS

N_BARS = 400
WINDOW = 48


def _closes(vols: dict[str, float], seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", periods=N_BARS, freq="1h", tz="UTC")
    return pd.DataFrame(
        {name: 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, size=N_BARS))) for name, vol in vols.items()},
        index=index,
    )


def test_the_calmest_name_scores_plus_one_and_the_wildest_minus_one() -> None:
    close = _closes({"CALM": 0.002, "MID": 0.01, "WILD": 0.05})
    last = xs_lowvol_scores(close, XsLowvolParams(window=WINDOW)).iloc[-1]
    assert last["CALM"] == 1.0
    assert last["WILD"] == -1.0
    assert last["MID"] == 0.0


def test_the_rank_is_over_the_reference_not_over_the_loaded_columns() -> None:
    """A non-member's vol must not move a member's rank, and the non-member gets no score (DL-Q1)."""
    close = _closes({"A": 0.002, "B": 0.01, "C": 0.03, "OUTSIDER": 0.0001})
    reference = pd.DataFrame(True, index=close.index, columns=close.columns)
    reference["OUTSIDER"] = False
    scores = xs_lowvol_scores(close, XsLowvolParams(window=WINDOW), reference)
    without = xs_lowvol_scores(close.drop(columns="OUTSIDER"), XsLowvolParams(window=WINDOW))
    assert scores["OUTSIDER"].isna().all()
    pd.testing.assert_frame_equal(scores[["A", "B", "C"]], without, check_exact=True)


def test_too_few_members_with_a_reading_is_no_action_rather_than_a_rank_of_two() -> None:
    close = _closes({"A": 0.002, "B": 0.01})
    scores = xs_lowvol_scores(close, XsLowvolParams(window=WINDOW, min_symbols=3))
    assert scores.isna().all().all()


def test_the_first_score_lands_where_the_declared_warmup_says() -> None:
    close = _closes({"A": 0.002, "B": 0.01, "C": 0.03})
    params = XsLowvolParams(window=WINDOW)
    scores = xs_lowvol_scores(close, params)
    first = int(np.argmax(scores.notna().any(axis=1).to_numpy()))
    assert first == realized_vol_warmup(WINDOW) - 1 == params.warmup_bars - 1


def test_the_stage_one_grid_and_the_stage_zero_cell_are_the_pre_registered_ones() -> None:
    assert DEFAULT_GRIDS["xs_lowvol"] == {"window": [168, 720], "entry_threshold": [0.20, 0.30]}
    defaults = SIGNALS["xs_lowvol"].default_params
    assert defaults == {"window": 720, "entry_threshold": 0.20, "min_symbols": 3}
