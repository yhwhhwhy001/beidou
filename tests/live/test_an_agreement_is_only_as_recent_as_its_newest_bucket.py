"""M-011 said "agree" for nineteen days about one frozen window of 2026-09-07.

The archive store (`.beidou/data/metrics/`) was filled once, by hand, on 2026-09-09, and nothing ran
`data metrics` again: `deploy/run_data.sh` held it out.  Every symbol's archive ended at
2026-09-07T23:55Z while the loop's snapshot kept growing.  The fifteen symbols in the universe since
09-07 shared the 155-156 buckets of 09-07 10:55-23:55Z with it, so every daily report from 09-09
re-compared those buckets and printed "N symbols agree" - and only the two open-interest columns, since
the snapshot's four ratio columns were NaN until 09-12.  LSKUSDT and NEARUSDT entered on 09-16 and
shared nothing, which is why M-011 read unmet from then on: no disagreement, no overlap.

Measured 2026-09-27 on the stores themselves.  This file holds the half of the fix that keeps the
reading honest whatever happens to the other half: the status says which bucket its comparison reaches,
and the report's `met` is the gate's own answer at the report's clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from beidou_data.store import MetricsStore
from beidou_governance.scheduler import parity_satisfied
from beidou_live.report_data import metrics_parity_status


def _bucket(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


def _hold(root: Path, kind: str, symbol: str, stamps: list[str]) -> None:
    frame = pd.DataFrame({"open_time": [_bucket(s) for s in stamps], "symbol": symbol, "sum_open_interest": 1.0})
    MetricsStore(root, kind=kind).append(symbol, frame)


def test_the_status_names_the_newest_bucket_its_stalest_symbol_shares(tmp_path: Path) -> None:
    """Folded to the stalest symbol, the way the rate is folded to the worst one: a book trades a universe."""
    both = ["2026-09-25T23:50:00+00:00", "2026-09-25T23:55:00+00:00"]
    _hold(tmp_path, "metrics", "AAAUSDT", both)
    _hold(tmp_path, "metrics_snapshot", "AAAUSDT", [*both, "2026-09-26T12:00:00+00:00"])
    _hold(tmp_path, "metrics", "BBBUSDT", ["2026-09-07T23:55:00+00:00", *both])
    _hold(tmp_path, "metrics_snapshot", "BBBUSDT", ["2026-09-07T23:55:00+00:00"])

    status = metrics_parity_status(["AAAUSDT", "BBBUSDT"], tmp_path, now=datetime(2026, 9, 26, 20, tzinfo=UTC))

    assert status["compared_through"] == "2026-09-07T23:55:00+00:00"
    assert status["stalest_symbol"] == "BBBUSDT"
    assert status["worst_differing_rate"] == 0.0, "both agree on every bucket they share"
    assert status["met"] is False, "and one of them has not been compared since 09-07"


def test_a_frozen_window_is_not_parity_however_well_it_agrees(tmp_path: Path) -> None:
    """The 2026-09-26 shape, reduced: the archive stops at 09-07, the snapshot runs on to 09-26."""
    frozen = ["2026-09-07T23:50:00+00:00", "2026-09-07T23:55:00+00:00"]
    _hold(tmp_path, "metrics", "BTCUSDT", ["2026-09-07T10:55:00+00:00", *frozen])
    _hold(tmp_path, "metrics_snapshot", "BTCUSDT", [*frozen, "2026-09-26T19:50:00+00:00"])

    later = metrics_parity_status(["BTCUSDT"], tmp_path, now=datetime(2026, 9, 26, 20, tzinfo=UTC))
    then = metrics_parity_status(["BTCUSDT"], tmp_path, now=datetime(2026, 9, 8, 12, tzinfo=UTC))

    assert later["met"] is False and "2026-09-07T23:55Z" in later["why"]
    assert then["met"] is True, "the same comparison was parity the day after it was made"
    # One rule, read in two places: `governance next` asks the gate about the block the report wrote.
    for status, now in ((later, datetime(2026, 9, 26, 20, tzinfo=UTC)), (then, datetime(2026, 9, 8, 12, tzinfo=UTC))):
        assert parity_satisfied(status, now=now) == (status["met"], status["why"])
