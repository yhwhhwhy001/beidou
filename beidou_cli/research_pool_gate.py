"""The pool entry gate's research half: the point-in-time membership recomputed with the gate on.

Operator ruling 2026-09-30, the "faithful" option.  A refresh skips a name the book could not open - its would-be
weight, the book's risk per name over the name's own sigma, misses the band's entry line (`beidou_alpha.portfolio`)
- and the next name by volume takes the slot.  Which names the book could open depends on the book, and the book
depends on the pool, so the table is a fixed point: build the book on a membership, read its risk per name,
recompute the membership with the gate, and repeat until nothing moves.  Live, the loop reads the same two numbers
off the book it holds (`LiveEngine._entry_gate`); at the fixed point the pool and the book agree the same way.

`beidou data pool gate` writes the table once, with a record of what it was computed from.  While the profile turns
the gate on, every research command that reads the point-in-time table reads this one (`research_panel._pit_table`),
and refuses when the record no longer describes the construction, the main book or the base table it re-ranked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import click
import pandas as pd

from beidou_alpha.model import AlphaModel
from beidou_alpha.portfolio import PortfolioParams, as_recorded, asset_vol, enterable, entry_line, per_name_risk
from beidou_alpha.registry import MAIN_BOOK
from beidou_data.pool import membership_at_bars, point_in_time_membership
from beidou_data.store import FundingStore, KlineStore, write_parquet_atomically
from beidou_data.universe import UniverseConfig
from beidou_live.composition import load_panel, load_registry, portfolio_params

GATED_FILE = "membership.gated.parquet"
GATED_RECORD = "membership.gated.json"
ROUNDS = 5

#: One reading of the book on a membership table: its main book's weights (unbanded), and the sigma of every
#: symbol it is asked about - members, and every name judged in an earlier round.
BookReader = Callable[[pd.DataFrame, list[str]], tuple[pd.DataFrame, pd.DataFrame]]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _table_digest(table: pd.DataFrame) -> str:
    cells = hashlib.sha256(table.to_numpy(dtype=bool).tobytes()).hexdigest()
    return _digest([list(map(str, table.columns)), list(map(str, table.index)), cells])


def _aligned(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = sorted(set(a.columns) | set(b.columns))
    index = a.index.union(b.index)
    return (
        a.reindex(index=index, columns=columns, fill_value=False).astype(bool),
        b.reindex(index=index, columns=columns, fill_value=False).astype(bool),
    )


def fixed_point(
    base: pd.DataFrame,
    volume: pd.DataFrame,
    config: UniverseConfig,
    eligible: Collection[str],
    read_book: BookReader,
    line: float,
    *,
    rounds: int = ROUNDS,
) -> tuple[pd.DataFrame, list[dict[str, int]]]:
    """Re-rank ``volume`` with the gate until the membership stops moving.  Refuses a base it cannot rebuild.

    The gate-off rebuild must reproduce ``base`` cell for cell, so that the gate is the ONLY difference between
    the evidence and the stored table; 2026-09-30 reproduced all 2,063 daily refreshes with zero cells apart.
    """
    rebuilt, stored = _aligned(point_in_time_membership(volume, config, eligible=eligible, refresh="D"), base)
    if not rebuilt.equals(stored):
        raise click.ClickException(
            f"the stored membership cannot be rebuilt from today's inputs ({int((rebuilt != stored).to_numpy().sum())} "
            "cells apart): rebuild it with `beidou data pool history` before gating it"
        )
    # Every name ever judged stays judged.  Sigma does not depend on the pool, only the book's risk per name does,
    # and a name the gate removed is no longer a member - asked only about members, the next round would find it
    # unjudged, let it back in, and the table would flip between the two answers forever.
    table, log, judged = base, [], {str(s) for s in base.columns[base.any(axis=0)]}
    for round_ in range(1, rounds + 1):
        judged |= {str(s) for s in table.columns[table.any(axis=0)]}
        weights, sigma = read_book(table, sorted(judged))
        gate = enterable(per_name_risk(weights, sigma), sigma, line)
        fresh = point_in_time_membership(volume, config, eligible=eligible, refresh="D", enterable=gate)
        before, after = _aligned(table, fresh)
        log.append({"round": round_, "cells_moved": int((before != after).to_numpy().sum())})
        table = after
        if log[-1]["cells_moved"] == 0:
            break
    return table, log


def gate_key(profile: Mapping[str, Any], base: pd.DataFrame) -> dict[str, str]:
    """What a gated table is valid for: the construction with the gate on, the main book, and the table it re-ranked."""
    portfolio = as_recorded(replace(portfolio_params(profile), pool_entry_gate=True).__dict__)
    registry = load_registry(str(profile.get("registry", "config/alpha_registry.yaml")))
    main = sorted((entry.id, dict(entry.params)) for entry in registry.enabled if entry.book == MAIN_BOOK)
    return {"construction": _digest(portfolio), "main_book": _digest(main), "base": _table_digest(base)}


def gated_membership(
    profile: Mapping[str, Any],
    base: pd.DataFrame,
    volume: pd.DataFrame,
    config: UniverseConfig,
    eligible: Collection[str],
    *,
    root: str,
    interval: str = "1h",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The fixed point on the stored data, read with the registry's model; the record says how it was reached."""
    portfolio: PortfolioParams = replace(portfolio_params(profile), pool_entry_gate=True)
    min_history = int((profile.get("portfolio") or {}).get("min_history_bars", 720))
    registry = load_registry(str(profile.get("registry", "config/alpha_registry.yaml")))
    model = AlphaModel.from_registry(registry, portfolio, interval, min_history_bars=min_history)
    klines, stored = KlineStore(root), set(KlineStore(root).symbols(interval))

    def read_book(table: pd.DataFrame, judged: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
        # Non-members in the panel change nothing the book holds: they are outside `membership_at_bars`, so outside
        # the reference population too (D-042).  They are there for their sigma.
        panel = load_panel(klines, [s for s in judged if s in stored], interval, funding_store=FundingStore(root))
        per_strategy = model.strategy_targets(panel, membership_at_bars(table, panel.index))
        main = model.book_weights(per_strategy, panel.close, panel.bars_per_year)[MAIN_BOOK]
        return main, asset_vol(panel.close, portfolio, panel.bars_per_year)

    table, log = fixed_point(base, volume, config, eligible, read_book, entry_line(portfolio))
    was, now = _aligned(base, table)
    record = {
        **gate_key(profile, base),
        "rounds": log,
        "converged": log[-1]["cells_moved"] == 0,
        "entry_line": entry_line(portfolio),
        "member_days_removed": int((was & ~now).to_numpy().sum()),
        "member_days_added": int((now & ~was).to_numpy().sum()),
        "eligible": {"count": len(eligible), "digest": _digest(sorted(eligible))},
    }
    return table, record


def write_gated(root: str, table: pd.DataFrame, record: Mapping[str, Any]) -> Path:
    path = Path(root) / GATED_FILE
    write_parquet_atomically(table, path, index=None)
    staging = Path(root) / f"{GATED_RECORD}.tmp"
    staging.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staging.replace(Path(root) / GATED_RECORD)
    return path


def read_gated(root: str, profile: Mapping[str, Any], base: pd.DataFrame) -> pd.DataFrame:
    """The gated table, if its record still describes this construction, main book and base table; else refuse."""
    path, record_path = Path(root) / GATED_FILE, Path(root) / GATED_RECORD
    if not path.exists() or not record_path.exists():
        raise click.ClickException(f"pool_entry_gate is on and {path} is missing: run `beidou data pool gate`")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    stale = [name for name, value in gate_key(profile, base).items() if record.get(name) != value]
    if stale or not record.get("converged"):
        why = f"it was computed for another {', '.join(stale)}" if stale else "it did not converge"
        raise click.ClickException(f"{path} does not describe this run: {why}; run `beidou data pool gate` again")
    table = pd.read_parquet(path)
    index = pd.DatetimeIndex(table.index)
    table.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return table.astype(bool)
