"""Append-only ledger of every configuration ever evaluated per strategy (the DSR denominator).

Selection bias does not reset between research rounds.  Every ``validate``
run records each grid point's full-sample Sharpe here; the next run charges
all of them (plus any manually declared pre-ledger trials) in its Deflated
Sharpe Ratio and uses their dispersion as the Sharpe variance.  Pure
functions over a list of records; the CLI does the file I/O.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class TrialRecord:
    strategy: str
    param_key: str
    sharpe_annual: float | None
    bars_per_year: float
    recorded_at: str
    range_start: str
    range_end: str
    symbols: int
    run_id: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> TrialRecord | None:
        try:
            payload = json.loads(line)
            return cls(**{key: payload[key] for key in cls.__dataclass_fields__})
        except (ValueError, KeyError, TypeError):
            return None


def parse_ledger(lines: Iterable[str], strategy: str) -> list[TrialRecord]:
    records: list[TrialRecord] = []
    for line in lines:
        record = TrialRecord.from_json(line)
        if record is not None and record.strategy == strategy:
            records.append(record)
    return records


def dsr_inputs(
    prior: list[TrialRecord],
    current_sharpes_period: Mapping[str, float | None],
    bars_per_year: float,
    *,
    manual_prior_trials: int = 0,
) -> dict[str, Any]:
    """n_trials and per-period Sharpe variance pooled over the ledger and the current grid."""
    scale = float(np.sqrt(bars_per_year))
    pooled: list[float] = [r.sharpe_annual / scale for r in prior if r.sharpe_annual is not None]
    pooled.extend(v for v in current_sharpes_period.values() if v is not None)
    n_trials = len(prior) + len(current_sharpes_period) + max(0, int(manual_prior_trials))
    variance = float(np.var(pooled, ddof=1)) if len(pooled) >= 2 else 0.0
    return {
        "n_trials": n_trials,
        "sharpe_variance": variance,
        "ledger_trials": len(prior),
        "pooled_sharpes": len(pooled),
    }
