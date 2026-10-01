"""The pool entry gate's research half: the point-in-time membership re-ranked day by day with the gate on.

Operator ruling 2026-09-30, the "faithful" option.  A refresh skips a name the book could not open - its would-be
weight, the book's risk per name over the name's own sigma, misses `entry_line` (`beidou_alpha.portfolio`) - and
the next name by volume takes the slot.

The live loop decides each refresh from the book it holds that day, so research does the same, in one pass: walk
the refresh dates in order and, at each one, price the book that the members chosen so far produced
(`ForwardGate`).  That book cannot come from `AlphaModel.strategy_targets` run on a finished table - the table is
what is being decided - so `MainBook` advances it refresh by refresh.  Two shapes that did use the finished-table
model failed on the real store on 2026-10-01, and are why it is built this way.  A global fixed point (re-rank the
whole history on the last round's book) did not converge in five rounds.  A forward walk that re-read the signals
on the previous pass's table verified about one more day per pass: the first day two passes differ is the first
day the older one was wrong, and each day's members move the next day's median risk.

`beidou data pool gate` writes the table once, with a record of what it was computed from and two checks against
the real model on that table: its main book, cell by cell, and every decision.  While the profile turns the gate
on, every research command that reads the point-in-time table reads this one (`research_panel._pit_table`), and
refuses when the record no longer describes the construction, the main book or the base table, or when either
check found a difference.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import click
import numpy as np
import pandas as pd

from beidou_alpha.ensemble import combine_targets
from beidou_alpha.features import cross_sectional_rank
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, as_recorded, asset_vol, entry_line, per_name_risk
from beidou_alpha.registry import MAIN_BOOK
from beidou_alpha.signals.tsmom import TsmomParams, apply_conviction_mode, tsmom_scores
from beidou_data.pool import membership_at_bars, point_in_time_membership
from beidou_data.store import FundingStore, KlineStore, write_parquet_atomically
from beidou_data.universe import UniverseConfig
from beidou_live.composition import load_panel, load_registry, portfolio_params

GATED_FILE = "membership.gated.parquet"
GATED_RECORD = "membership.gated.json"


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


class MainBook:
    """The main book's conviction bar by bar, advanced on the members each refresh chose.

    What in `AlphaModel.strategy_targets` does not depend on membership is computed once, by the signal's own
    functions: tsmom's score, and the trailing funding its crowding modifier ranks.  What does is advanced one
    refresh at a time, the way the model computes it over a whole table: the crowding rank over the reference
    population (``history & member``), the hold of the last actionable score (`scores_to_targets`), the exit
    mask, the ensemble.  Only a main book of tsmom alone under the mean ensemble is reproduced; anything else is
    refused rather than approximated.
    """

    def __init__(self, panel: Panel, model: AlphaModel) -> None:
        entries = model.entries_of(MAIN_BOOK)
        if [entry.id for entry in entries] != ["tsmom"] or model.ensemble_method != "mean":
            raise click.ClickException(
                "the forward simulation reproduces a main book of tsmom alone under the mean ensemble; this one is "
                f"{[entry.id for entry in entries]} under {model.ensemble_method!r}"
            )
        self.entry, self.hold = entries[0], model.hold_on_no_action
        self.params = p = TsmomParams.from_mapping(self.entry.params)
        close = panel.close
        self._score = tsmom_scores(close, p)
        self._trailing: pd.DataFrame | None = None
        if p.crowding_window > 0 and panel.funding is not None and p.crowding_penalty > 0:  # as `crowding_mask`
            aligned = panel.funding.reindex(index=close.index, columns=close.columns)
            trailing = aligned.rolling(p.crowding_window, min_periods=p.crowding_window).sum()
            observed = aligned.abs().rolling(p.crowding_window, min_periods=p.crowding_window).sum() > 0
            self._trailing = trailing.where(observed)
        self._history = (close.notna().cumsum() >= model.min_history_bars).to_numpy()
        self._column = {str(symbol): j for j, symbol in enumerate(close.columns)}
        self._last = np.full(len(self._column), np.nan)  # the hold: the last actionable score while eligible
        self._seen = np.zeros(len(self._column), dtype=bool)
        self.targets = np.full(close.shape, np.nan)
        self.advanced = 0  # bars before this one are done

    def advance(self, stop: int, members: Collection[str]) -> None:
        """The bars up to and including ``stop``, all covered by one refresh that chose ``members``."""
        if stop < self.advanced:
            return
        rows, p = slice(self.advanced, stop + 1), self.params
        member = np.zeros(len(self._column), dtype=bool)
        member[[self._column[s] for s in members if s in self._column]] = True
        eligible = self._history[rows] & member
        score = self._score.iloc[rows]
        if self._trailing is not None:
            reference = pd.DataFrame(eligible, index=score.index, columns=score.columns)
            rank = cross_sectional_rank(self._trailing.iloc[rows], reference).fillna(0.0)
            crowded = ((score > 0) & (rank >= p.crowding_cut)) | ((score < 0) & (rank <= -p.crowding_cut))
            score = score.mask(crowded, score * (1.0 - p.crowding_penalty))
        values = apply_conviction_mode(score, p).to_numpy(dtype=float)
        held = np.empty_like(values)
        for r in range(len(values)):  # `scores_to_targets` on `scores.where(eligible)`, then the exit mask
            s = np.where(eligible[r], values[r], np.nan)
            with np.errstate(invalid="ignore"):
                actionable = np.where(s == 0.0, 0.0, np.where(np.abs(s) >= self.entry.entry_threshold, s, np.nan))
            self._seen |= ~np.isnan(s)
            if self.hold:
                self._last = np.where(np.isnan(actionable), self._last, actionable)
            kept = self._last if self.hold else actionable
            row = np.where(self._seen, np.where(np.isnan(kept), 0.0, kept), np.nan)
            held[r] = np.where(~eligible[r] & ~np.isnan(row), 0.0, row)
        frame = pd.DataFrame(held, index=score.index, columns=score.columns)
        combined = combine_targets({self.entry.id: frame}, {self.entry.id: self.entry.weight}, method="mean")
        self.targets[rows] = combined.to_numpy(dtype=float)
        self.advanced = stop + 1


@dataclass
class ForwardGate:
    """Asked by `point_in_time_membership` once per refresh, in date order: which names the book could not open.

    It first advances the main book over the bars the previous refresh covered, held by the members that refresh
    chose, and the EWMA covariance to the last of them.  The covariance is `ewma_portfolio_vol`'s recursion and
    depends on returns only.  The risk per name is what stage 2 gives a held name - ``vol_target x scalar``,
    after `max_weight` and the gross cap - read as `per_name_risk` reads the model's book: the median
    ``|w| x sigma`` over held names.
    """

    panel: Panel
    model: AlphaModel
    decisions: dict[pd.Timestamp, tuple[float | None, frozenset[str]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        close, self.portfolio = self.panel.close, self.model.portfolio
        self.book = MainBook(self.panel, self.model)
        self._returns = close.pct_change().fillna(0.0).to_numpy(dtype=float)
        sigma = asset_vol(close, self.portfolio, self.panel.bars_per_year)
        self._sigma = sigma.reindex(index=close.index, columns=close.columns).to_numpy(dtype=float)
        self._column = {str(symbol): j for j, symbol in enumerate(close.columns)}
        self._lam = math.exp(-math.log(2.0) / max(self.portfolio.covariance_halflife, 1))
        self._cov = np.zeros((len(self._column), len(self._column)))
        self._t = -1

    def __call__(self, date: pd.Timestamp, previous: list[str]) -> set[str]:
        stop = int(self.panel.close.index.searchsorted(date, side="left")) - 1
        self.book.advance(stop, previous)
        while self._t < stop:  # `ewma_portfolio_vol`'s recursion, advanced to the last bar before the refresh
            self._t += 1
            row = self._returns[self._t]
            self._cov = self._lam * self._cov + (1.0 - self._lam) * np.outer(row, row)
        risk = self.risk(stop, previous)
        blocked: frozenset[str] = frozenset()
        if risk is not None:
            with np.errstate(divide="ignore", invalid="ignore"):
                would_be = risk / self._sigma[stop]
            blocked = frozenset(s for s, j in self._column.items() if would_be[j] < entry_line(self.portfolio))
        self.decisions[date] = (risk, blocked)
        return set(blocked)

    def risk(self, t: int, members: list[str]) -> float | None:
        """The book's risk per name at bar ``t`` holding ``members``; None when it holds nothing measurable yet."""
        p = self.portfolio
        if t < 0 or t + 1 < max(p.covariance_halflife, 2):
            return None
        idx = [self._column[s] for s in members if s in self._column]
        sigma = self._sigma[t, idx]
        with np.errstate(divide="ignore", invalid="ignore"):
            stage1 = np.nan_to_num(self.book.targets[t, idx] * (p.vol_target / sigma), nan=0.0)
        vol = math.sqrt(max(float(stage1 @ self._cov[np.ix_(idx, idx)] @ stage1), 0.0) * self.panel.bars_per_year)
        if vol <= 1e-12:
            return None
        weights = np.clip(stage1 * min(p.vol_target / vol, p.max_scalar), -p.max_weight, p.max_weight)
        gross = float(np.abs(weights).sum())
        weights = weights * (p.max_gross / gross if gross > p.max_gross else 1.0)
        held = weights != 0.0
        return float(np.median(np.abs(weights[held]) * sigma[held])) if held.any() else None


@dataclass
class Shortlist:
    """What `LivePool.select` never measures at a refresh: names outside its 24h-volume shortlist that are not
    previous members (pins are the caller's: `point_in_time_membership` never blocks one).  Live reads the ticker
    just after midnight UTC, so the last daily bar before the refresh stands in for it.  The stored table has no such cut, and 24 of its 36,256 member-days fall outside it
    (2026-10-01).  A gated walk without it put 773 there: the deep names a blocked top lets in, never measured live.
    """

    volume: pd.DataFrame
    eligible: Collection[str]
    size: int

    def __post_init__(self) -> None:
        self._eligible = set(self.eligible)

    def __call__(self, date: pd.Timestamp, previous: list[str]) -> set[str]:
        before = self.volume.loc[: date - pd.Timedelta(nanoseconds=1)]
        if before.empty:
            return set()
        day = before.iloc[-1].dropna().sort_values(ascending=False, kind="stable")
        kept = {*[s for s in day.index if s in self._eligible][: self.size], *previous}
        return {str(s) for s in self.volume.columns if s not in kept}


def shortlist_size(profile: Mapping[str, Any], config: UniverseConfig) -> int:
    """`LivePool`'s shortlist, read as `beidou_live.config.build_pool` reads it: ``pool.candidates`` or 3 x top_n."""
    return int((profile.get("pool") or {}).get("candidates", 0)) or 3 * config.top_n


def gate_key(profile: Mapping[str, Any], base: pd.DataFrame) -> dict[str, str]:
    """What a gated table is valid for: the construction with the gate on, the main book, the pool's shortlist, and
    the table it re-ranked."""
    portfolio = as_recorded(replace(portfolio_params(profile), pool_entry_gate=True).__dict__)
    registry = load_registry(str(profile.get("registry", "config/alpha_registry.yaml")))
    main = sorted((entry.id, dict(entry.params)) for entry in registry.enabled if entry.book == MAIN_BOOK)
    shortlist = (profile.get("pool") or {}).get("candidates", 0)
    return {
        "construction": _digest(portfolio),
        "main_book": _digest(main),
        "shortlist": _digest(shortlist),
        "base": _table_digest(base),
    }


def candidates(volume: pd.DataFrame, eligible: Collection[str], base: pd.DataFrame, size: int) -> list[str]:
    """Every name live could have measured: each that ever made the shortlist, plus every base member."""
    ranks = volume[[s for s in volume.columns if s in set(eligible)]].rank(axis=1, ascending=False, method="min")
    listed = ranks.le(size).any(axis=0)
    return sorted({str(s) for s in listed[listed].index} | {str(s) for s in base.columns[base.any(axis=0)]})


def walk(
    base: pd.DataFrame,
    volume: pd.DataFrame,
    config: UniverseConfig,
    eligible: Collection[str],
    panel: Panel,
    model: AlphaModel,
    *,
    shortlist: int | None = None,
) -> tuple[pd.DataFrame, ForwardGate, int]:
    """The gated pool, one refresh at a time; and the member-days it admitted with no bars to judge them by."""
    rebuilt, stored = _aligned(point_in_time_membership(volume, config, eligible=eligible, refresh="D"), base)
    if not rebuilt.equals(stored):
        raise click.ClickException(
            f"the stored membership cannot be rebuilt from today's inputs ({int((rebuilt != stored).to_numpy().sum())} "
            "cells apart): rebuild it with `beidou data pool history` before gating it"
        )
    gate = ForwardGate(panel, model)
    unlisted = Shortlist(volume, eligible, shortlist or 3 * config.top_n)
    table = point_in_time_membership(
        volume,
        config,
        eligible=eligible,
        refresh="D",
        gate=lambda date, previous: gate(date, previous) | unlisted(date, previous),
    )
    loaded = set(map(str, panel.close.columns))
    return table, gate, int(table[[s for s in table.columns if s not in loaded]].to_numpy().sum())


def checked(table: pd.DataFrame, panel: Panel, model: AlphaModel, gate: ForwardGate) -> dict[str, int]:
    """The walk against the real model run on the table it produced: the main book cell by cell, every decision."""
    per_strategy = model.strategy_targets(panel, membership_at_bars(table, panel.index))
    real = model.book_targets(per_strategy)[MAIN_BOOK].reindex(index=panel.index, columns=panel.close.columns)
    done = gate.book.advanced
    a, b = real.to_numpy(dtype=float)[:done], gate.book.targets[:done]
    differing = int((~((a == b) | (np.isnan(a) & np.isnan(b)))).sum())
    main = model.book_weights(per_strategy, panel.close, panel.bars_per_year)[MAIN_BOOK]
    sigma = asset_vol(panel.close, model.portfolio, panel.bars_per_year)
    risk = per_name_risk(main, sigma)
    line, disagreeing = entry_line(model.portfolio), 0
    for date, (_simulated, blocked) in gate.decisions.items():
        stop = int(panel.index.searchsorted(date, side="left")) - 1
        actual = risk.iloc[stop] if stop >= 0 else float("nan")
        if math.isnan(actual):
            disagreeing += len(blocked)
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            would_be = actual / sigma.iloc[stop]
        disagreeing += len(blocked ^ {str(s) for s, value in would_be.items() if value < line})
    return {"targets_cells_differing": differing, "decisions_disagreeing": disagreeing}


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
    """The walk on the stored data with the registry's model, checked against that model; the record says how."""
    portfolio: PortfolioParams = replace(portfolio_params(profile), pool_entry_gate=True)
    min_history = int((profile.get("portfolio") or {}).get("min_history_bars", 720))
    registry = load_registry(str(profile.get("registry", "config/alpha_registry.yaml")))
    model = AlphaModel.from_registry(registry, portfolio, interval, min_history_bars=min_history)
    size, stored = shortlist_size(profile, config), set(KlineStore(root).symbols(interval))
    names = [s for s in candidates(volume, eligible, base, size) if s in stored]
    panel = load_panel(KlineStore(root), names, interval, funding_store=FundingStore(root))
    table, gate, unjudged = walk(base, volume, config, eligible, panel, model, shortlist=size)
    checks = checked(table, panel, model, gate)
    was, now = _aligned(base, table)
    record = {
        **gate_key(profile, base),
        **checks,
        "admitted_unjudged": unjudged,
        "usable": not any(checks.values()) and unjudged == 0,
        "bars_checked": gate.book.advanced,
        "shortlist_size": size,
        "entry_line": entry_line(portfolio),
        "candidates": len(names),
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
    """The gated table, if its record still describes this run and says it is usable; else refuse."""
    path, record_path = Path(root) / GATED_FILE, Path(root) / GATED_RECORD
    if not path.exists() or not record_path.exists():
        raise click.ClickException(f"pool_entry_gate is on and {path} is missing: run `beidou data pool gate`")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    stale = [name for name, value in gate_key(profile, base).items() if record.get(name) != value]
    if stale or not record.get("usable"):
        why = f"it was computed for another {', '.join(stale)}" if stale else "its walk is not usable (see the record)"
        raise click.ClickException(f"{path} does not describe this run: {why}; run `beidou data pool gate` again")
    table = pd.read_parquet(path)
    index = pd.DatetimeIndex(table.index)
    table.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return table.astype(bool)
