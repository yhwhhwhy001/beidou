"""Append-only ledger of every configuration ever evaluated per strategy (the DSR denominator).

Selection bias does not reset between research rounds.  Every ``validate``
run records each grid point's full-sample Sharpe here; the next run charges
all of them (plus any manually declared pre-ledger trials) in its Deflated
Sharpe Ratio and uses their dispersion as the Sharpe variance.  An exact
replay of a recorded configuration on the same data (same parameters, same
range, same symbol count) is one trial, not two (D-024).  Pure functions over
a list of records; the CLI does the file I/O.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

# KILL-Q5: the ledger's address used to be `Path(--out) / "trials.jsonl"`, and `--out` is a flag
# passed for ordinary reasons - point the reports at a scratch directory and the run was charged to a
# fresh, empty ledger.  A loophole nobody has to intend is the kind worth closing, so the address is
# fixed and moving it takes an environment variable whose only possible purpose is to not be charged.
LEDGER_ENV = "BEIDOU_TRIALS_LEDGER"
DEFAULT_LEDGER = Path("reports/research/trials.jsonl")


def _checkout_root(start: Path) -> Path | None:
    """The checkout ``start`` sits in, or None.  A worktree's ``.git`` is a file, and still a root."""
    here = start.resolve()
    for directory in (here, *here.parents):
        if (directory / ".git").exists():
            return directory
    return None


def resolve_ledger_path(*, out: str | Path | None = None, root: Path | None = None, start: Path | None = None) -> Path:
    """The one ledger.  ``out`` is accepted and ignored, which is the entire point of this function.

    Anchored to the checkout rather than to the working directory.  A relative path is L1-07's shape -
    run from ``beidou_alpha/`` and ``reports/research/trials.jsonl`` names a file that does not exist,
    so the run is charged to a fresh empty book and reports ``ledger_trials: 0``.  That is the state
    KILL-Q5 was about, reached by walking into a subdirectory instead of by typing a flag; closing one
    and leaving the other would have been closing the one that is harder to trip over.

    Per checkout, deliberately: the ledger is a tracked file, so a worktree's rows reach the others by
    merging, the same path every other artefact here takes.
    """
    override = os.environ.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override)
    anchor = root if root is not None else _checkout_root(start or Path.cwd())
    return (anchor / DEFAULT_LEDGER) if anchor is not None else DEFAULT_LEDGER


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
    # DL-K1.  Optional, defaulting to "", because 145 rows predate them and a ledger that cannot read
    # its own history is not a ledger.  A legacy row therefore never folds into a modern one: folding
    # would assert that the old run used today's construction, which is the ledger claiming knowledge
    # it does not have - worse than charging a trial twice (KILL-P5).
    construction_digest: str = ""  # the portfolio construction the weights were built under (D-026)
    overlay_digest: str = ""  # exits / throttle applied on top of them
    symbol_set_hash: str = ""  # WHICH symbols, not how many: `symbols` is a count and cannot tell two apart
    search_space_version: str = ""  # for mined ids: how wide the search that produced this was

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> TrialRecord | None:
        """A missing REQUIRED field is a broken row; a missing optional one is a legacy row.

        That distinction is the whole of DL-K1's backward compatibility - 145 rows predate the four
        optional fields, and reading them has to yield "" rather than an error or a guess - which is
        why the mapping is spelled out here instead of derived from `fields()`.
        """
        try:
            payload = json.loads(line)
            return cls(
                strategy=payload["strategy"],
                param_key=payload["param_key"],
                sharpe_annual=payload["sharpe_annual"],
                bars_per_year=payload["bars_per_year"],
                recorded_at=payload["recorded_at"],
                range_start=payload["range_start"],
                range_end=payload["range_end"],
                symbols=payload["symbols"],
                run_id=payload["run_id"],
                construction_digest=str(payload.get("construction_digest", "")),
                overlay_digest=str(payload.get("overlay_digest", "")),
                symbol_set_hash=str(payload.get("symbol_set_hash", "")),
                search_space_version=str(payload.get("search_space_version", "")),
            )
        except (ValueError, KeyError, TypeError):
            return None

    @property
    def signature(self) -> tuple[str, str, str, int, str, str, str, str]:
        """What makes two records the same trial.

        Parameters, data range and symbol count were not enough: they cannot see the construction the
        weights were built under, the overlay applied to them, WHICH symbols those were, or how wide
        the search a mined id came from.  Two runs differing in any of those are two trials, and
        folding them charged the DSR denominator less than the selection actually cost (KILL-Q5).
        """
        return (
            self.param_key,
            self.range_start,
            self.range_end,
            self.symbols,
            self.construction_digest,
            self.overlay_digest,
            self.symbol_set_hash,
            self.search_space_version,
        )


# DL-K2: every candidate a `research mine` round evaluated lands here, under one key rather than one
# key each.  The search IS the family - a mined candidate was selected by ranking the whole space on
# the full sample - so its denominator has to be able to read the space back.
MINED_SEARCH_STRATEGY = "mined"


def ledger_scope(strategy: str) -> tuple[str, ...]:
    """Which ledger keys a strategy's DSR denominator draws from.

    A hand-written strategy pays for its own grid.  A mined candidate pays for the search that found
    it as well: nothing about `mined_594a12f9307a15d9` is remarkable until you know it was the best of
    514, and that number used to be copied off a terminal into `--prior-trials` by hand.
    """
    if strategy.startswith("mined_"):
        return (strategy, MINED_SEARCH_STRATEGY)
    return (strategy,)


def parse_ledger(lines: Iterable[str], strategy: str | Iterable[str]) -> list[TrialRecord]:
    wanted = {strategy} if isinstance(strategy, str) else set(strategy)
    records: list[TrialRecord] = []
    for line in lines:
        record = TrialRecord.from_json(line)
        if record is not None and record.strategy in wanted:
            records.append(record)
    return records


def unique_trials(records: Iterable[TrialRecord], *, exclude: Iterable[tuple[Any, ...]] = ()) -> list[TrialRecord]:
    """First record per signature; signatures in ``exclude`` (the current run's own grid) are dropped."""
    skip = set(exclude)
    seen: set[tuple[Any, ...]] = set()
    out: list[TrialRecord] = []
    for record in records:
        signature = record.signature
        if signature in skip or signature in seen:
            continue
        seen.add(signature)
        out.append(record)
    return out


def dsr_inputs(
    prior: list[TrialRecord],
    current_sharpes_period: Mapping[str, float | None],
    bars_per_year: float,
    *,
    manual_prior_trials: int = 0,
    current_range: tuple[str, str, int] | None = None,
    current_context: tuple[str, str, str, str] = ("", "", "", ""),
) -> dict[str, Any]:
    """n_trials and per-period Sharpe variance pooled over the ledger and the current grid.

    Exact replays are counted once: duplicate ledger rows collapse to one, and a
    ledger row that is the current grid re-run on the same range and symbols
    (``current_range`` = (range_start, range_end, symbols)) is not charged twice.

    ``current_context`` is the other half of the signature - construction, overlay, symbol set,
    search space - and it must be built here rather than left to default, because an exclusion set
    shorter than a signature matches nothing at all and silently charges every replay twice.
    """
    scale = float(np.sqrt(bars_per_year))
    exclude = (
        [
            (key, current_range[0], current_range[1], current_range[2], *current_context)
            for key in current_sharpes_period
        ]
        if current_range is not None
        else []
    )
    distinct = unique_trials(prior)
    unique = unique_trials(distinct, exclude=exclude)
    pooled: list[float] = [r.sharpe_annual / scale for r in unique if r.sharpe_annual is not None]
    pooled.extend(v for v in current_sharpes_period.values() if v is not None)
    n_trials = len(unique) + len(current_sharpes_period) + max(0, int(manual_prior_trials))
    variance = float(np.var(pooled, ddof=1)) if len(pooled) >= 2 else 0.0
    return {
        "n_trials": n_trials,
        "sharpe_variance": variance,
        "ledger_trials": len(unique),
        "ledger_rows": len(prior),
        "duplicate_rows": len(prior) - len(distinct),  # exact copies of an earlier row
        "replayed_rows": len(distinct) - len(unique),  # the current grid re-run on the same data
        "pooled_sharpes": len(pooled),
    }
