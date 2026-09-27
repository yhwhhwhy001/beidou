"""M-011 vouches for the columns a candidate can read, to the precision the two sources publish.

Ruled by the operator on 2026-09-27, the day the archive was first refreshed and M-011 compared the
four ratio columns at all (RESEARCH_LOG, "M-011 读了十九天的 09-07"):

* the three account and position ratios agree bucket for bucket and differ around 1e-4 relative - REST
  prints four decimals where the archive keeps six - so they are compared at a RELATIVE 1e-3.  Measured
  on 174k same-bucket pairs across the 17 symbols of the universe: the largest was 3.9e-4;
* `sum_taker_long_short_vol_ratio` is stamped five minutes early in the snapshot and no leaf reads it,
  so M-011 compares the columns some leaf reads and nothing else.  The offset is fixed separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from beidou_alpha.mining.expr import METRICS_COLUMNS, METRICS_LEAVES, Expr
from beidou_data.metrics_snapshot import metrics_parity
from beidou_data.store import MetricsStore
from beidou_live.report_data import metrics_parity_status


def _subclasses(cls: type) -> set[type]:
    direct = set(cls.__subclasses__())
    return direct.union(*(_subclasses(sub) for sub in direct)) if direct else direct


def test_the_columns_m011_compares_are_the_ones_a_leaf_reads() -> None:
    """A node that says which metrics it reads is a leaf, and every leaf is listed.

    Listed rather than discovered at run time, so this is what keeps the list honest: a new metrics
    leaf left out of `METRICS_LEAVES` would reach live on a column M-011 never compared.
    """
    declares = {cls for cls in _subclasses(Expr) if "reads_metrics" in vars(cls)}

    assert declares == set(METRICS_LEAVES)
    assert tuple(leaf.COLUMN for leaf in METRICS_LEAVES) == METRICS_COLUMNS
    assert METRICS_COLUMNS == ("sum_open_interest", "count_long_short_ratio")


def test_a_ratio_is_compared_to_the_precision_rest_publishes_and_open_interest_is_not() -> None:
    """The 2026-09-25T23:50Z BTCUSDT bucket: REST 1.3031 against the archive's 1.302937."""
    archive = pd.DataFrame(
        {"open_time": [1, 2], "count_long_short_ratio": [1.302937, 1.302937], "sum_open_interest": [107239.507] * 2}
    )
    snapshot = pd.DataFrame(
        {"open_time": [1, 2], "count_long_short_ratio": [1.3031, 1.3050], "sum_open_interest": [107239.507] * 2}
    )
    assert metrics_parity(snapshot, archive)["differing"] == 1, "1.25e-4 agrees; 1.6e-3 does not"

    moved = snapshot.assign(count_long_short_ratio=1.302937, sum_open_interest=107239.6)
    assert metrics_parity(moved, archive)["differing"] == 2, "8.7e-7 relative is still a disagreement for open interest"


def test_a_column_no_leaf_reads_does_not_decide_parity(tmp_path: Path) -> None:
    buckets = [int(datetime(2026, 9, 26, 23, minute, tzinfo=UTC).timestamp() * 1000) for minute in (50, 55)]
    for kind, taker in (("metrics", 0.9), ("metrics_snapshot", 1.4)):
        frame = pd.DataFrame(
            {
                "open_time": buckets,
                "symbol": "LSKUSDT",
                "sum_open_interest": 5.0,
                "count_long_short_ratio": 1.3,
                "sum_taker_long_short_vol_ratio": taker,
            }
        )
        MetricsStore(tmp_path, kind=kind).append("LSKUSDT", frame)

    status = metrics_parity_status(["LSKUSDT"], tmp_path, now=datetime(2026, 9, 27, 12, tzinfo=UTC))

    assert status["columns"] == list(METRICS_COLUMNS)
    assert status["worst_differing_rate"] == 0.0, "the taker ratio disagrees on every bucket and is not asked"
    assert status["met"] is True, status["why"]
