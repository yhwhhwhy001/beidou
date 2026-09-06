"""``beidou research ...`` commands: backtest, validate, list."""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import numpy as np
import pandas as pd

from beidou_alpha.backtest import BacktestResult, CostModel, benchmark_returns, run_backtest
from beidou_alpha.mining import enumerate_candidates, to_signal
from beidou_alpha.model import AlphaModel, FundingUnavailable
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.overlays.exposure import BookGuardParams, DrawdownThrottleParams, apply_drawdown_throttle
from beidou_alpha.panel import Panel, interval_seconds
from beidou_alpha.portfolio import PortfolioParams, apply_no_trade_band, combine_books
from beidou_alpha.registry import StrategyEntry, registry_fingerprint
from beidou_alpha.report import canonical_json, render_markdown
from beidou_alpha.signals import SIGNALS, get_signal
from beidou_alpha.signals import register as register_signal
from beidou_alpha.signals.base import scores_to_targets
from beidou_alpha.validation.cpcv import cpcv_evaluate, cpcv_splits
from beidou_alpha.validation.decompose import decompose_book
from beidou_alpha.validation.labels import forward_returns
from beidou_alpha.validation.ledger import TrialRecord, dsr_inputs, parse_ledger
from beidou_alpha.validation.metrics import (
    compound,
    information_coefficient,
    max_drawdown,
    newey_west_tstat,
    sharpe,
    sign_bucketed_ic,
    time_series_ic,
    yearly_breakdown,
)
from beidou_alpha.validation.multiple_testing import multiple_testing_report, oos_selection_threshold
from beidou_alpha.validation.stability import cost_stress, parameter_neighborhood, time_split_sharpes
from beidou_alpha.validation.verdict import decide
from beidou_alpha.validation.walk_forward import Fold, param_key, walk_forward_evaluate, walk_forward_folds
from beidou_cli import research
from beidou_data.manifest import build_manifest
from beidou_data.pool import MEMBERSHIP_FILE, membership_at_bars, tenure_mask
from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import build_model, cost_model, load_panel, load_registry, portfolio_params, read_universe
from beidou_shared.config import load_yaml

DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "tsmom": {
        "horizons": [[5, 20, 50], [24, 72, 168], [168, 336, 720], [336, 720, 1440]],
        "entry_threshold": [0.20, 0.30],
        "return_scale": [0.20, 0.30],
        "vol_window": [400],
    },
    # Prior-driven small grids: every extra trial costs DSR power; do not widen these to "find" a pass.
    "xsmom": {"skip_bars": [24, 48], "z_scale": [1.0, 1.5], "entry_threshold": [0.20, 0.30]},
    "carry": {"window_bars": [72, 168], "entry_threshold": [0.20, 0.30]},
    "meanrev": {"window": [24, 48, 96], "z_entry": [1.5, 2.0, 2.5], "trend_gate_z": [1.5, 2.0, 3.0]},
    "breakout": {"window": [24, 48, 96], "distance_scale": [1.0, 2.0, 3.0]},
    "flow": {"window": [24, 72, 168, 336], "scale": [0.03, 0.05, 0.10], "entry_threshold": [0.20, 0.30]},
    "residual": {"horizons": [[24, 72, 168], [168, 336, 720]], "scale": [0.05, 0.10], "beta_window": [336, 720]},
}
# D-017 pre-registered overlay grids: evaluated once on the registry ensemble, never widened after seeing results.
DEFAULT_EXIT_GRID: dict[str, list[Any]] = {
    "stop_loss": [0.0, 2.5, 4.0],
    "trailing_stop": [0.0, 4.0],
    "take_profit": [0.0, 6.0],
}
DEFAULT_THROTTLE_GRID: dict[str, list[Any]] = {"start": [0.05], "stop": [0.20], "floor": [0.25]}
UNIVERSE_MODES = ("static", "pit")


def _common_options(function: Any) -> Any:
    for option in reversed(
        [
            click.option("--strategy", required=True, help="signal id (see `beidou research list`)"),
            click.option("--params", default="", help="JSON overriding the registry/default params"),
            click.option("--root", default=".beidou/data", show_default=True),
            click.option("--symbols", default="", help="comma-separated; default = selected universe or all stored"),
            click.option("--interval", default="1h", show_default=True),
            click.option("--from", "start", default=None, help="YYYY-MM-DD inclusive"),
            click.option("--to", "end", default=None, help="YYYY-MM-DD exclusive"),
            click.option("--profile", default="config/live.demo.yaml", show_default=True),
            click.option("--registry", "registry_path", default="config/alpha_registry.yaml", show_default=True),
            click.option("--costs", "costs_path", default="config/costs.yaml", show_default=True),
            click.option(
                "--execution", type=click.Choice(["open_to_close", "close_to_close"]), default="open_to_close"
            ),
            click.option("--funding/--no-funding", default=True, show_default=True),
            click.option("--out", default="reports/research", show_default=True),
            click.option(
                "--min-history",
                default=None,
                type=int,
                help="bars a symbol must have before it is tradable (default: profile portfolio.min_history_bars)",
            ),
            click.option(
                "--universe",
                "universe_mode",
                type=click.Choice(list(UNIVERSE_MODES)),
                default="static",
                show_default=True,
                help="static = universe.json/all stored; pit = point-in-time membership from `beidou data pool history`",
            ),
            click.option(
                "--min-tenure",
                default=0,
                show_default=True,
                help="pit only: refreshes of prior membership a symbol needs before it is tradable (established names)",
            ),
        ]
    ):
        function = option(function)
    return function


def _membership_table(root: str) -> pd.DataFrame:
    path = Path(root) / MEMBERSHIP_FILE
    if not path.exists():
        raise click.ClickException(f"{path} is missing; run `beidou data pool history` first")
    table = pd.read_parquet(path)
    index = pd.DatetimeIndex(table.index)
    table.index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return table.astype(bool)


def _resolve_symbols(root: str, symbols: str, interval: str, universe_mode: str = "static") -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if universe_mode == "pit":
        table = _membership_table(root)
        union = [str(s) for s in table.columns[table.any(axis=0)]]
        stored = set(KlineStore(root).symbols(interval))
        missing = sorted(set(union) - stored)
        if missing:
            click.echo(f"pit universe: {len(missing)} member symbols have no {interval} klines yet: {missing[:10]}...")
        return [s for s in union if s in stored]
    universe = read_universe(root)
    return universe or KlineStore(root).symbols(interval)


def _membership(root: str, universe_mode: str, panel: Panel, min_tenure: int = 0) -> pd.DataFrame | None:
    """Bars x symbols boolean mask for ``--universe pit``; ``None`` keeps the static behaviour."""
    if universe_mode != "pit":
        return None
    return membership_at_bars(tenure_mask(_membership_table(root), min_tenure), panel.index)


def _resolve_mined(strategy: str) -> None:
    """Make a ``mined_<hash>`` id addressable in this process by re-deriving it from the search.

    This is what the canonical hash is for.  Enumeration is deterministic and touches no data, so a
    candidate does not need persisting to be referred to across commands - and re-deriving rather than
    storing means a hash that no longer enumerates is reported as gone instead of silently resolving to
    a stale definition.
    """
    if not strategy.startswith("mined_"):
        return
    wanted = strategy.removeprefix("mined_")
    for candidate in enumerate_candidates().candidates:
        if candidate.hash == wanted:
            register_signal(to_signal(candidate))
            return
    raise click.ClickException(f"no candidate hashes to {wanted} in the current search space")


def _entry(strategy: str, registry_path: str, params: str) -> StrategyEntry:
    # Every command resolves its strategy id through here, so a mined candidate is addressable wherever
    # a hand-written one is.  It used to be wired into `correlate` alone: `research validate --strategy
    # mined_<hash>` raised a bare KeyError, which is precisely the wall an operator hits the moment the
    # shortlist hands them something worth validating.  A no-op for every id that is not `mined_`.
    _resolve_mined(strategy)
    get_signal(strategy)
    base: dict[str, Any] = dict(SIGNALS[strategy].default_params)
    registry_file = Path(registry_path)
    if registry_file.exists():
        for candidate in load_registry(registry_file).strategies:
            if candidate.id == strategy:
                base.update(candidate.params)
    if params:
        base.update(json.loads(params))
    return StrategyEntry(id=strategy, params=base)


def _model(entry: StrategyEntry, profile: dict[str, Any], interval: str, min_history: int | None = None) -> AlphaModel:
    if min_history is None:
        min_history = int((profile.get("portfolio", {}) or {}).get("min_history_bars", 720))
    return AlphaModel(
        entries=(entry,), portfolio=portfolio_params(profile), interval=interval, min_history_bars=min_history
    )


def _load(root: str, symbols: list[str], interval: str, start: str | None, end: str | None, funding: bool) -> Panel:
    store = KlineStore(root)
    return load_panel(
        store, symbols, interval, funding_store=FundingStore(root) if funding else None, start=start, end=end
    )


def _funding_consumers(entries: Sequence[StrategyEntry]) -> list[str]:
    return sorted({entry.id for entry in entries if get_signal(entry.id).needs_funding(entry.params)})


def _require_funding(entries: Sequence[StrategyEntry], panel: Panel) -> None:
    """Refuse a run whose signals consume funding against a panel that carries none (E-040 / KILL-027).

    ``AlphaModel.strategy_targets`` refuses the same thing and is the guard that cannot be forgotten;
    this one exists for three reasons it cannot cover.  ``research diagnose`` computes the signal directly
    and never builds a model, so nothing else would stop it.  The operator asked for ``--no-funding``, so
    the answer belongs at the flag - which strategy, which flag - rather than in a library traceback.

    And ``--funding`` is the DEFAULT, which is the case the library guard is blind to by construction.
    ``FundingStore.load`` returns an empty frame for a symbol with no archive, so an unsynced root yields
    a funding frame of all zeros rather than ``None``; tsmom's crowding rank then reads every symbol as
    uncrowded and the run writes the exact report E-040 is about, at exit 0, with nothing said anywhere.
    A signal that reads no settlement at all is as inert as one handed no frame, so it is refused alike.
    """
    hungry = _funding_consumers(entries)
    if not hungry:
        return
    settled, total = panel.settled_symbols, len(panel.symbols)
    if panel.funding is None:
        raise click.ClickException(
            f"{', '.join(hungry)} consumes funding history under these params, so --no-funding would run the "
            "signal on inputs it was never judged on (E-040 / KILL-027). Pass --funding, or choose params "
            "that read none (tsmom: crowding_window 0) to run the control arm deliberately."
        )
    if settled == 0:
        raise click.ClickException(
            f"{', '.join(hungry)} consumes funding history, but the archive under this root holds no "
            f"settlement for any of the {total} symbols in the panel, so the signal would read zeros and be "
            "as inert as it is under --no-funding (E-040 / KILL-027). Run `beidou data sync` first."
        )
    if settled < total:
        click.echo(
            f"warning: {', '.join(hungry)} reads funding and only {settled}/{total} symbols have any "
            "settlement stored; the rest read as zero, which the signal cannot tell from calm funding."
        )


def _funding_facts(entries: Sequence[StrategyEntry], panel: Panel) -> dict[str, Any]:
    """What the signals required of funding, beside what the panel actually carried.

    Recorded as ``funding_inputs``, deliberately not ``funding``: a report already carries
    ``dataset.funding`` (D-040), which counts FILES IN THE ARCHIVE, and both blocks would then hold a
    ``symbols`` key meaning different things - 2 files on disk against a 4-symbol panel.  On a
    partially-synced root the two numbers even coincide by accident, which is the worst kind of
    collision to leave in the artifact an operator reads to decide whether to trust a strategy.

    Reports recorded the *cost model's* ``use_funding`` and nothing about the signals' own requirement, so
    a reader could not tell a modifier that was absent from one that ran on nothing.  ``_require_funding``
    now refuses both of the wholly-inert cases, which leaves this to record the partial one it lets run:
    a symbol with no archive contributes a zero column that tsmom's crowding rank reads as uncrowded, so
    ``symbols_settled`` is what keeps a thinner modifier legible on disk rather than merely quieter.
    """
    return {
        "required_by": _funding_consumers(entries),
        "panel_carried": panel.funding is not None,
        "symbols_settled": panel.settled_symbols,
        "panel_symbols": len(panel.symbols),
    }


def _write(out: str, name: str, payload: dict[str, Any], markdown: str) -> tuple[Path, str]:
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{name}.json"
    rendered = canonical_json(payload)
    json_path.write_text(rendered, encoding="utf-8")
    (directory / f"{name}.md").write_text(markdown, encoding="utf-8")
    return json_path, hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _stamp() -> str:
    # seconds resolution: two runs inside the same minute must never overwrite each other's evidence
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


@research.command("list")
def research_list() -> None:
    """List available signals and their default parameters."""
    for spec in SIGNALS.values():
        click.echo(f"{spec.id}: {spec.description} (warmup {spec.warmup_bars} bars)")
        click.echo(f"    defaults: {json.dumps(spec.default_params)}")


@research.command("backtest")
@_common_options
@click.option(
    "--guards/--no-guards",
    default=True,
    show_default=True,
    help="replay the book-level guards the live loop applies (gross cap + daily-loss pause)",
)
def research_backtest(
    guards: bool,
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
) -> None:
    """Backtest one strategy through the full portfolio pipeline and write a research report."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    _require_funding([entry], panel)
    membership = _membership(root, universe_mode, panel, min_tenure)
    model = _model(entry, profile_payload, interval, min_history)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    weights, _combined, _per = model.evaluate(panel, membership)
    # The guards are part of the construction the loop runs, not an extra: they are provably inert
    # wherever neither binds (`tests/alpha/test_book_guard_replay.py`), so leaving them on keeps a
    # report describing the book that would actually be held.  `--no-guards` reproduces older reports.
    book_guards = (
        BookGuardParams(
            max_weight=float((profile_payload.get("portfolio", {}) or {}).get("max_weight", 0.15)),
            max_gross=float((profile_payload.get("portfolio", {}) or {}).get("max_gross", 2.0)),
            daily_loss_pause=float((profile_payload.get("guards", {}) or {}).get("daily_loss_pause", -0.05)),
        )
        if guards
        else None
    )
    result = run_backtest(panel, weights, cost, execution=execution, guards=book_guards)  # type: ignore[arg-type]
    summary = result.summary()
    bench = benchmark_returns(panel, execution, panel.symbols).reindex(result.weights.index)  # type: ignore[arg-type]
    report: dict[str, Any] = {
        "kind": "backtest",
        "strategy": strategy,
        "params": entry.params,
        "portfolio": model.portfolio.__dict__,
        "book_guards": None if book_guards is None else book_guards.__dict__,
        "interval": interval,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "range": {"start": str(result.weights.index[0]), "end": str(result.weights.index[-1]), "bars": summary["bars"]},
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts([entry], panel),
        "execution": execution,
        "summary": summary,
        "benchmark": {"gross_return": compound(bench), "sharpe": sharpe(bench, panel.bars_per_year)},
        "per_symbol": result.per_symbol_summary(),
        "yearly": yearly_breakdown(result.portfolio_net, panel.bars_per_year),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    markdown = render_markdown(
        f"Backtest: {strategy}",
        [
            ("Range", report["range"]),
            ("Params", entry.params),
            ("Summary", summary),
            ("Benchmark (equal-weight long, zero cost)", report["benchmark"]),
            (
                "Yearly",
                {year: f"ret={v['return']:.3f} sharpe={_fmt(v['sharpe'])}" for year, v in report["yearly"].items()},
            ),
            (
                "Per symbol",
                {
                    s: f"net={v['net_return']:.3f} sharpe={_fmt(v['annualized_sharpe'])} turnover={v['turnover_units']:.1f}"
                    for s, v in report["per_symbol"].items()
                },
            ),
        ],
    )
    path, digest = _write(out, f"{strategy}-backtest-{_stamp()}", report, markdown)
    _echo_summary(summary, report["benchmark"])
    click.echo(f"report: {path} sha256={digest}")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


COST_SHARE_LIMIT = 0.40  # KILL-013: above this, turnover is eating the edge and the result is not investable


def _cost_flag(cost_share: float | None) -> str:
    """KILL-013 asks for costs above 40% of gross to be flagged, not merely printed."""
    if cost_share is None or cost_share <= COST_SHARE_LIMIT:
        return ""
    return f"  ** COSTS EAT {cost_share:.0%} OF GROSS (> {COST_SHARE_LIMIT:.0%}, KILL-013) **"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:+.3f}%"


def _echo_summary(summary: dict[str, Any], benchmark: dict[str, Any] | None = None) -> None:
    click.echo(
        f"bars={summary['bars']} gross={summary['gross_return']:.4f} net={summary['net_return']:.4f} "
        f"sharpe={_fmt(summary['annualized_sharpe'])} mdd={summary['max_drawdown']:.4f} "
        f"turnover={summary['turnover_units']:.1f} exposure={summary['average_absolute_exposure']:.3f} "
        f"cost_share={_fmt(summary['cost_share_of_gross'])}{_cost_flag(summary['cost_share_of_gross'])}"
    )
    if benchmark:
        click.echo(f"benchmark: gross={benchmark['gross_return']:.4f} sharpe={_fmt(benchmark['sharpe'])}")


def _grid(strategy: str, grid_json: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    grid = json.loads(grid_json) if grid_json else DEFAULT_GRIDS.get(strategy, {})
    if not grid:
        return [dict(base)]
    keys = sorted(grid)
    combos: list[dict[str, Any]] = []
    for values in itertools.product(*(grid[key] for key in keys)):
        combos.append({**base, **dict(zip(keys, values, strict=True))})
    return combos


@research.command("validate")
@_common_options
@click.option("--grid", default="", help="JSON {param: [values...]} (default grid per strategy)")
@click.option("--folds", default=5, show_default=True)
@click.option("--min-train", default=4000, show_default=True, help="bars before the first test fold")
@click.option("--purge", default=50, show_default=True)
@click.option("--cpcv-groups", default=6, show_default=True)
@click.option(
    "--prior-trials",
    default=0,
    show_default=True,
    help="configurations of this strategy already tried in earlier rounds (added to the DSR denominator)",
)
@click.option(
    "--holdout-months",
    default=0,
    show_default=True,
    help="reserve the last N months (KILL-006), cut before folds; unused by choice, see docs/RESEARCH_LOG.md",
)
def research_validate(
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    grid: str,
    folds: int,
    min_train: int,
    purge: int,
    cpcv_groups: int,
    prior_trials: int,
    holdout_months: int,
) -> None:
    """Walk-forward + CPCV + DSR/PBO + stability for one strategy; writes the evidence report for the registry."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    # KILL-006: the reserved tail is cut here, before folds, membership or costs touch it, so nothing in this
    # run can see it.  It is recorded in the report, which is what makes the reservation checkable later:
    # a promise in prose is not a holdout, and every OOS number produced without one has been selected on.
    holdout: dict[str, Any] | None = None
    if holdout_months > 0:
        last = pd.Timestamp(panel.index[-1])
        cutoff = last - pd.DateOffset(months=holdout_months)
        held = panel.slice(start=cutoff)
        panel = panel.slice(end=cutoff)
        if len(panel.index) < 2:
            raise click.ClickException(f"--holdout-months {holdout_months} leaves no training data")
        holdout = {
            "months": holdout_months,
            "start": str(cutoff),
            "end": str(last),
            "bars_reserved": len(held.index),
            "bars_used": len(panel.index),
        }
        click.echo(
            f"holdout: reserving {holdout['bars_reserved']} bars from {cutoff.date()} to {last.date()} "
            f"({holdout_months} months); this run sees {holdout['bars_used']} bars"
        )
    membership = _membership(root, universe_mode, panel, min_tenure)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    combos = _grid(strategy, grid, entry.params)
    # the combos, not `entry.params`: a grid may set the funding term to 0 in every arm it evaluates
    _require_funding([StrategyEntry(id=strategy, params=combo) for combo in combos], panel)
    bpy = panel.bars_per_year
    nets: dict[str, pd.Series] = {}
    params_by_key: dict[str, dict[str, Any]] = {}
    results: dict[str, BacktestResult] = {}
    click.echo(f"evaluating {len(combos)} parameter sets on {len(panel.symbols)} symbols x {len(panel.index)} bars")
    for combo in combos:
        key = param_key(combo)
        model = _model(StrategyEntry(id=strategy, params=combo), profile_payload, interval, min_history)
        weights, _c, _p = model.evaluate(panel, membership)
        result = run_backtest(panel, weights, cost, execution=execution)  # type: ignore[arg-type]
        results[key] = result
        nets[key] = result.portfolio_net
        params_by_key[key] = combo
    common_index = None
    for series in nets.values():
        common_index = series.index if common_index is None else common_index.intersection(series.index)
    assert common_index is not None
    nets = {key: series.reindex(common_index).fillna(0.0) for key, series in nets.items()}
    n_bars = len(common_index)
    fold_list = walk_forward_folds(n_bars, folds, min_train=min(min_train, max(n_bars // 2, 2)), purge=purge)
    wf = walk_forward_evaluate(nets, params_by_key, fold_list, bpy)
    wf_summary = wf.summary(bpy)
    cpcv = cpcv_evaluate(
        nets, cpcv_splits(n_bars, n_groups=cpcv_groups, n_test_groups=2, purge=purge, embargo=purge), bpy
    )
    full_sharpes_raw: dict[str, float | None] = {key: sharpe(series, bpy) for key, series in nets.items()}
    full_sharpes: dict[str, float] = {
        key: (value if value is not None else -np.inf) for key, value in full_sharpes_raw.items()
    }
    best_key = max(full_sharpes, key=lambda k: full_sharpes[k])
    matrix = np.column_stack([nets[key].to_numpy(dtype=float) for key in nets])
    ledger_path = Path(out) / "trials.jsonl"
    prior_records = (
        parse_ledger(ledger_path.read_text(encoding="utf-8").splitlines(), strategy) if ledger_path.exists() else []
    )
    period_sharpes = {
        key: (None if value is None else value / math.sqrt(bpy)) for key, value in full_sharpes_raw.items()
    }
    pooled = dsr_inputs(
        prior_records,
        period_sharpes,
        bpy,
        manual_prior_trials=prior_trials,
        current_range=(str(common_index[0]), str(common_index[-1]), len(panel.symbols)),
    )
    mt = multiple_testing_report(
        nets[best_key].to_numpy(dtype=float),
        matrix,
        bars_per_year=bpy,
        prior_trials=prior_trials,
        pooled_n_trials=pooled["n_trials"],
        pooled_sharpe_variance=pooled["sharpe_variance"] if pooled["pooled_sharpes"] >= 2 else None,
    )
    mt["ledger_trials"] = pooled["ledger_trials"]

    def evaluate_params(candidate: Mapping[str, Any]) -> float | None:
        model = _model(StrategyEntry(id=strategy, params=dict(candidate)), profile_payload, interval, min_history)
        weights, _c, _p = model.evaluate(panel, membership)
        return sharpe(run_backtest(panel, weights, cost, execution=execution).portfolio_net, bpy)  # type: ignore[arg-type]

    neighbourhood = parameter_neighborhood(
        evaluate_params, params_by_key[best_key], numeric_keys=tuple(sorted(DEFAULT_GRIDS.get(strategy, {})))
    )
    best_weights = results[best_key].weights
    stress = cost_stress(
        {
            multiplier: run_backtest(
                panel,
                best_weights.shift(-1).reindex(panel.close.index),
                CostModel(cost.turnover_bps * multiplier, cost.carry_bps_per_bar * multiplier, cost.use_funding),
                execution=execution,  # type: ignore[arg-type]
            ).portfolio_net
            for multiplier in (1.0, 1.5, 2.0)
        },
        bpy,
    )
    report: dict[str, Any] = {
        "kind": "validation",
        "strategy": strategy,
        "interval": interval,
        "universe_mode": universe_mode,
        "min_tenure": min_tenure,
        "holdout": holdout,
        "symbols": panel.symbols,
        "range": {"start": str(common_index[0]), "end": str(common_index[-1]), "bars": n_bars},
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts([StrategyEntry(id=strategy, params=c) for c in combos], panel),
        "execution": execution,
        "grid_size": len(combos),
        "grid": json.loads(grid) if grid else DEFAULT_GRIDS.get(strategy, {}),
        # D-024: a strategy's numbers are produced by a portfolio construction, so the report has to say which
        # one.  Without this a band or half-life change in the profile silently detaches the live book from its
        # cited evidence, and the startup gate cannot see it because it compares signal params only.
        "portfolio": _model(
            StrategyEntry(id=strategy, params=combos[0]), profile_payload, interval, min_history
        ).portfolio.__dict__,
        # The data this verdict was computed from.  Everything else here already names itself - the report
        # has a digest, the registry a fingerprint, the construction another - but the dataset did not, and
        # on 2026-09-04 the membership table was rebuilt monthly -> daily while the profile still described
        # it as monthly.  A verdict that cannot say which data produced it is a pointer waiting to go stale.
        "dataset": build_manifest(root, interval).to_dict(),
        "folds": folds,
        "min_train": min_train,
        "purge": purge,
        "cpcv_groups": cpcv_groups,
        "prior_trials_declared": prior_trials,
        "trial_sharpes": {key: full_sharpes_raw[key] for key in nets},
        "ledger": {
            key: pooled[key]
            for key in ("ledger_trials", "ledger_rows", "duplicate_rows", "replayed_rows", "pooled_sharpes")
        },
        "best_params": params_by_key[best_key],
        "full_sample": results[best_key].summary(),
        # F3 (KILL-Q2): `best_params` is the full-sample argmax and is what reaches the registry,
        # while `walk_forward.oos_sharpe` belongs to whatever each fold chose.  When the two differ
        # the headline describes a mixture no configuration ever was, so the shipped configuration's
        # own walk-forward number is recorded next to it rather than left for a reader to assume.
        "best_key_oos_sharpe": wf.oos_sharpe_for(best_key, fold_list, bpy),
        "walk_forward": wf_summary,
        # D-028: the OOS Sharpe a strategy must clear given how many configurations were tried on it.
        "oos_selection": oos_selection_threshold(
            wf.oos_returns.to_numpy(dtype=float), n_trials=pooled["n_trials"], bars_per_year=bpy
        ),
        "cpcv": cpcv,
        "multiple_testing": mt,
        "stability": {
            "time_split_sharpes": time_split_sharpes(wf.oos_returns, 4, bpy),
            "parameter_neighborhood": neighbourhood,
        },
        "cost_stress": stress,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    verdict, reasons = decide(report)
    report["verdict"] = verdict
    report["reasons"] = reasons
    markdown = render_markdown(
        f"Validation: {strategy} — {verdict}",
        [
            ("Range", report["range"]),
            (
                "Holdout (KILL-006)",
                holdout or {"months": 0, "note": "no tail reserved: every bar was available to this run"},
            ),
            ("Best params (full sample)", params_by_key[best_key]),
            ("Full sample", report["full_sample"]),
            (
                "Walk-forward (out of sample)",
                {
                    **{k: v for k, v in wf_summary.items() if k != "chosen_params"},
                    "best_key_oos_sharpe": report["best_key_oos_sharpe"],
                },
            ),
            ("CPCV", {k: v for k, v in cpcv.items() if k != "chosen"}),
            ("Multiple testing", mt),
            ("Selection-deflated OOS threshold (D-028)", report["oos_selection"]),
            (
                "Stability",
                {
                    "time_split_sharpes": report["stability"]["time_split_sharpes"],
                    "worst_neighbour_degradation": neighbourhood["worst_degradation"],
                    "parameter_neighbourhood": {
                        key: f"down={_fmt(value.get('down'))} base={_fmt(neighbourhood['base'])} up={_fmt(value.get('up'))}"
                        for key, value in (neighbourhood.get("neighbours") or {}).items()
                    },
                },
            ),
            ("Grid (full-sample Sharpe per configuration)", _grid_table(params_by_key, full_sharpes_raw)),
            ("Cost stress (Sharpe)", stress),
            ("Verdict", {"verdict": verdict, "reasons": reasons or ["-"]}),
        ],
    )
    path, digest = _write(out, f"{strategy}-validation-{_stamp()}", report, markdown)
    stamp = datetime.now(UTC).isoformat()
    with ledger_path.open("a", encoding="utf-8") as handle:
        for key in nets:
            handle.write(
                TrialRecord(
                    strategy=strategy,
                    param_key=key,
                    sharpe_annual=full_sharpes_raw[key],
                    bars_per_year=bpy,
                    recorded_at=stamp,
                    range_start=str(common_index[0]),
                    range_end=str(common_index[-1]),
                    symbols=len(panel.symbols),
                    run_id=path.stem,
                ).to_json()
                + "\n"
            )
    click.echo(
        f"trials ledger: {ledger_path} (+{len(nets)}; {pooled['ledger_trials']} already in ledger, {prior_trials} declared pre-ledger)"
    )
    click.echo(f"best params: {params_by_key[best_key]}")
    click.echo(
        f"walk-forward OOS sharpe={_fmt(wf_summary['oos_sharpe'])} return={wf_summary['oos_return']:.4f} consistency={_fmt(wf_summary['fold_consistency'])}"
    )
    if wf_summary["oos_is_full_sample_tail"]:
        # KILL-Q2: said out loud, because the number above reads like an out-of-sample estimate of a
        # selection procedure and here no fold had a choice to make.
        click.echo(
            "  NOTE: no fold had a choice (single configuration, or every fold picked the same one), "
            "so this OOS is the tail of one full-sample series, not a selection's out-of-sample record"
        )
    elif report["best_key_oos_sharpe"] is not None:
        click.echo(
            f"  the shipped configuration's own walk-forward OOS is {_fmt(report['best_key_oos_sharpe'])} "
            "(the headline above is the fold-selected mixture)"
        )
    click.echo(
        f"cpcv mean={_fmt(cpcv['oos_sharpe_mean'])} q05={_fmt(cpcv['oos_sharpe_q05'])} negative={_fmt(cpcv['fraction_negative'])}"
    )
    click.echo(
        f"dsr p={_fmt(mt['dsr_p_value'])} pbo={_fmt(mt['pbo'])} cost_stress={ {k: _fmt(v) for k, v in stress.items()} }"
    )
    click.echo(
        f"oos selection threshold={_fmt(report['oos_selection']['threshold_annual'])} "
        f"at {report['oos_selection']['n_trials']} trials, alpha={report['oos_selection']['alpha']}, "
        f"p_family={_fmt(report['oos_selection']['p_family'])} (D-028)"
    )
    click.echo(f"VERDICT: {verdict} {reasons if reasons else ''}")
    click.echo(f"report: {path} sha256={digest}")
    click.echo("registry evidence block:")
    click.echo(f"    evidence: {{report: {path}, sha256: {digest}, verdict: {verdict}}}")


__all__ = [
    "research_backtest",
    "research_book",
    "research_correlate",
    "research_decompose",
    "research_diagnose",
    "research_list",
    "research_overlay",
    "research_validate",
]


@research.command("diagnose")
@_common_options
@click.option("--horizons", default="1,4,24,72,168", show_default=True, help="forward-return horizons (bars) for IC")
def research_diagnose(
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    horizons: str,
) -> None:
    """Signal-level diagnostics before any portfolio construction: IC by horizon, signal-only backtest, flips."""
    del out  # diagnostics write nothing
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    _require_funding([entry], panel)
    if min_history is None:
        min_history = int((load_yaml(profile).get("portfolio", {}) or {}).get("min_history_bars", 720))
    eligible = panel.close.notna().cumsum() >= min_history
    membership = _membership(root, universe_mode, panel, min_tenure)
    if membership is not None:
        eligible &= membership
    scores = get_signal(strategy).compute(panel, entry.params).where(eligible)
    coverage = float(scores.notna().mean().mean())
    click.echo(f"{strategy} on {len(panel.symbols)} symbols x {len(panel.index)} bars; score coverage={coverage:.2f}")
    click.echo("horizon | ts-IC mean (spearman, per-symbol avg) | xs-IC mean | NW t | NW p")
    for horizon in [int(h) for h in horizons.split(",") if h.strip()]:
        fwd = forward_returns(panel.close, horizon)
        ts = [time_series_ic(scores[s], fwd[s]) for s in panel.symbols]
        ts_values = [v for v in ts if v is not None]
        xs = information_coefficient(scores, fwd)
        nw = newey_west_tstat(xs, max_lags=horizon) if len(xs) > 10 else {"t_stat": None, "p_value": None}
        ts_mean = sum(ts_values) / len(ts_values) if ts_values else float("nan")
        click.echo(
            f"{horizon:>7} | {ts_mean:+.4f} | {float(xs.mean()) if len(xs) else float('nan'):+.4f} | "
            f"{_fmt(nw['t_stat'])} | {_fmt(nw['p_value'])}"
        )
    # KILL-042: a negative time-series IC alongside a profitable book.  Non-overlapping labels, split by
    # the sign of the score, so "the magnitude is uninformative" and "the signal is wrong" are separable.
    click.echo("horizon | non-overlapping IC | long bucket: n / IC / mean fwd | short bucket: n / IC / mean fwd")
    for horizon in [int(h) for h in horizons.split(",") if h.strip()]:
        block = sign_bucketed_ic(
            scores, forward_returns(panel.close, horizon), horizon, threshold=entry.entry_threshold
        )
        long_side, short_side = block["long"], block["short"]
        click.echo(
            f"{horizon:>7} | {_fmt(block['overall_ic'])} | "
            f"{long_side['n']} / {_fmt(long_side['ic'])} / {_fmt_pct(long_side['mean_forward_return'])} | "
            f"{short_side['n']} / {_fmt(short_side['ic'])} / {_fmt_pct(short_side['mean_forward_return'])}"
        )
    targets = scores_to_targets(scores, entry.entry_threshold, hold=True)
    flips = int((targets.fillna(0.0).apply(np.sign).diff().abs() > 0).sum().sum())
    click.echo(f"target flips: {flips} ({flips / max(1, targets.notna().sum().sum()):.4f} per symbol-bar)")
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    equal = targets / max(1, len(panel.symbols))
    for label, model in (
        ("signal-only equal-notional, zero cost", CostModel(0.0, 0.0, False)),
        ("signal-only equal-notional, full cost", cost),
    ):
        summary = run_backtest(panel, equal, model, execution=execution).summary()  # type: ignore[arg-type]
        click.echo(f"{label}: ")
        _echo_summary(summary)


@research.command("correlate")
@click.option("--strategies", required=True, help="comma-separated signal ids (registry params are used)")
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--symbols", default="")
@click.option("--interval", default="1h", show_default=True)
@click.option("--from", "start", default=None)
@click.option("--to", "end", default=None)
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--registry", "registry_path", default="config/alpha_registry.yaml", show_default=True)
@click.option("--costs", "costs_path", default="config/costs.yaml", show_default=True)
@click.option("--funding/--no-funding", default=True, show_default=True)
@click.option("--out", default="reports/research", show_default=True)
@click.option("--universe", "universe_mode", type=click.Choice(list(UNIVERSE_MODES)), default="static")
def research_correlate(
    strategies: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    funding: bool,
    out: str,
    universe_mode: str,
) -> None:
    """Correlation of strategy net-return streams and the marginal Sharpe of each strategy in an equal-weight mix."""
    ids = [s.strip() for s in strategies.split(",") if s.strip()]
    profile_payload = load_yaml(profile)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    entries = {strategy: _entry(strategy, registry_path, "") for strategy in ids}
    _require_funding(list(entries.values()), panel)
    membership = _membership(root, universe_mode, panel)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    nets: dict[str, pd.Series] = {}
    for strategy in ids:
        entry = entries[strategy]
        weights, _c, _p = _model(entry, profile_payload, interval).evaluate(panel, membership)
        nets[strategy] = run_backtest(panel, weights, cost).portfolio_net
    frame = pd.DataFrame(nets).dropna(how="all").fillna(0.0)
    bpy = panel.bars_per_year
    corr = frame.corr()
    individual = {k: sharpe(frame[k], bpy) for k in frame.columns}
    combined = sharpe(frame.mean(axis=1), bpy)
    marginal: dict[str, float | None] = {}
    for k in frame.columns:
        rest = [c for c in frame.columns if c != k]
        without = sharpe(frame[rest].mean(axis=1), bpy) if rest else None
        marginal[k] = None if combined is None or without is None else combined - without
    report: dict[str, Any] = {
        "kind": "correlation",
        "strategies": ids,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        # every Sharpe and correlation below is NET of these costs, so a report without them cannot be
        # reproduced or compared - and `use_funding` is the field that says how the run was produced.
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts(list(entries.values()), panel),
        "range": {"start": str(frame.index[0]), "end": str(frame.index[-1]), "bars": len(frame)},
        "correlation": corr.round(4).to_dict(),
        "individual_sharpe": individual,
        "equal_weight_sharpe": combined,
        "marginal_sharpe": marginal,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    markdown = render_markdown(
        "Strategy correlation: " + ", ".join(ids),
        [
            ("Range", report["range"]),
            ("Individual Sharpe", individual),
            ("Equal-weight mix Sharpe", {"sharpe": combined}),
            ("Marginal Sharpe (mix minus mix-without)", marginal),
            ("Correlation", {f"{a}~{b}": corr.loc[a, b] for a in corr.index for b in corr.columns if a < b}),
        ],
    )
    path, digest = _write(out, f"correlate-{'-'.join(ids)}-{_stamp()}", report, markdown)
    click.echo(markdown)
    click.echo(f"report: {path} sha256={digest}")


def _overlay_metrics(result: BacktestResult, folds: int, min_train: int, bars_per_year: float) -> dict[str, Any]:
    net = result.portfolio_net
    n = len(net)
    fold_list = walk_forward_folds(n, folds, min_train=min(min_train, max(n // 2, 2)))
    oos = net.iloc[fold_list[0].test_start :]
    return {
        "full_sharpe": sharpe(net, bars_per_year),
        "full_mdd": max_drawdown(net),
        "oos_sharpe": sharpe(oos, bars_per_year),
        "oos_mdd": max_drawdown(oos),
        "oos_return": compound(oos),
        "fold_sharpes": [sharpe(net.iloc[f.test_slice], bars_per_year) for f in fold_list],
        "turnover_units": float(result.turnover.sum()),
        "average_absolute_exposure": float(result.weights.abs().sum(axis=1).mean()),
    }


@research.command("overlay")
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--symbols", default="", help="comma-separated; default = selected universe or all stored")
@click.option("--interval", default="1h", show_default=True)
@click.option("--from", "start", default=None, help="YYYY-MM-DD inclusive")
@click.option("--to", "end", default=None, help="YYYY-MM-DD exclusive")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--registry", "registry_path", default="config/alpha_registry.yaml", show_default=True)
@click.option("--costs", "costs_path", default="config/costs.yaml", show_default=True)
@click.option("--funding/--no-funding", default=True, show_default=True)
@click.option("--out", default="reports/research", show_default=True)
@click.option("--min-history", default=None, type=int)
@click.option("--universe", "universe_mode", type=click.Choice(list(UNIVERSE_MODES)), default="static")
@click.option("--exits-grid", default="", help="JSON {param: [values...]} (default: the pre-registered grid)")
@click.option("--throttle-grid", default="", help="JSON {param: [values...]} (default: the pre-registered grid)")
@click.option("--folds", default=5, show_default=True)
@click.option("--min-train", default=4000, show_default=True)
@click.option(
    "--max-sharpe-loss",
    default=0.10,
    show_default=True,
    help="D-017: a candidate qualifies only if OOS max drawdown improves and OOS Sharpe loses at most this much",
)
def research_overlay(
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    exits_grid: str,
    throttle_grid: str,
    folds: int,
    min_train: int,
    max_sharpe_loss: float,
) -> None:
    """Evidence for the exit overlay and the drawdown throttle on the registry ensemble (D-012/D-015/D-017)."""
    profile_payload = load_yaml(profile)
    registry = load_registry(registry_path)
    model = build_model(registry, profile_payload)
    if min_history is not None:
        # `replace`, not a hand-listed constructor: the hand-listed one omitted `books=`, so
        # `--min-history` died on any registry declaring a sleeve - the shipped one does (D-018) - before
        # it could reach the data.  Re-listing fields is the bug; carrying them all is the fix.
        model = replace(model, min_history_bars=min_history)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    _require_funding(model.entries, panel)
    membership = _membership(root, universe_mode, panel)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    bpy = panel.bars_per_year
    weights, _combined, _per = model.evaluate(panel, membership)
    base = run_backtest(panel, weights, cost)
    baseline = _overlay_metrics(base, folds, min_train, bpy)
    bars_per_day = max(1, 86_400 // interval_seconds(interval))
    exit_combos = _grid("exits", exits_grid, {}) if exits_grid else _grid_of(DEFAULT_EXIT_GRID)
    throttle_combos = _grid("throttle", throttle_grid, {}) if throttle_grid else _grid_of(DEFAULT_THROTTLE_GRID)
    rows: list[dict[str, Any]] = []
    click.echo(
        f"ensemble {[e.id for e in model.entries]} on {len(panel.symbols)} symbols x {len(panel.index)} bars; "
        f"baseline oos_sharpe={_fmt(baseline['oos_sharpe'])} oos_mdd={baseline['oos_mdd']:.3f}"
    )
    for combo in exit_combos:
        params = ExitParams.from_mapping({**combo, "bars_per_day": bars_per_day})
        if not params.enabled:
            continue
        overlay = apply_exits(weights, panel.close, params)
        metrics = _overlay_metrics(run_backtest(panel, overlay.weights, cost), folds, min_train, bpy)
        rows.append({"kind": "exits", "params": combo, **metrics, "events": overlay.summary()})
    for combo in throttle_combos:
        throttle = DrawdownThrottleParams.from_mapping({**combo, "enabled": True})
        scaled, scalars = apply_drawdown_throttle(weights, base.portfolio_net.reindex(weights.index), throttle)
        metrics = _overlay_metrics(run_backtest(panel, scaled, cost), folds, min_train, bpy)
        rows.append({"kind": "throttle", "params": combo, **metrics, "mean_scalar": float(scalars.mean())})

    def qualifies(row: dict[str, Any]) -> bool:
        if row["oos_sharpe"] is None or baseline["oos_sharpe"] is None:
            return False
        return bool(
            row["oos_mdd"] > baseline["oos_mdd"] and row["oos_sharpe"] >= baseline["oos_sharpe"] - max_sharpe_loss
        )

    recommendation: dict[str, Any] = {}
    for kind in ("exits", "throttle"):
        qualified = [row for row in rows if row["kind"] == kind and qualifies(row)]
        best = max(qualified, key=lambda row: float(row["oos_sharpe"])) if qualified else None
        recommendation[kind] = {
            "enable": best is not None,
            "params": None if best is None else best["params"],
            "qualified": len(qualified),
            "tested": sum(1 for row in rows if row["kind"] == kind),
            "rule": f"oos_mdd improves and oos_sharpe >= baseline - {max_sharpe_loss}; tie-break: highest oos_sharpe",
        }
    report: dict[str, Any] = {
        "kind": "overlay",
        "strategies": [e.id for e in model.entries],
        # D-024: an overlay decides on the ensemble, so it is only valid for the registry it ran against.
        "registry": registry_fingerprint(registry, lambda sid, params: get_signal(sid).canonical_params(params)),
        "portfolio": model.portfolio.__dict__,
        "interval": interval,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "range": {"start": str(base.weights.index[0]), "end": str(base.weights.index[-1]), "bars": len(base.weights)},
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts(model.entries, panel),
        "folds": folds,
        "min_train": min_train,
        "baseline": baseline,
        "candidates": rows,
        "recommendation": recommendation,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    lines = [
        f"{row['kind']} {json.dumps(row['params'])}: oos_sharpe={_fmt(row['oos_sharpe'])} "
        f"oos_mdd={row['oos_mdd']:.3f} full_sharpe={_fmt(row['full_sharpe'])} full_mdd={row['full_mdd']:.3f} "
        f"folds={[round(x, 2) if x is not None else None for x in row['fold_sharpes']]} "
        f"{'QUALIFIES' if qualifies(row) else '-'}"
        for row in rows
    ]
    markdown = render_markdown(
        "Overlay evidence: " + ", ".join(report["strategies"]),
        [
            ("Range", report["range"]),
            (
                "Registry this was computed against (D-024)",
                {
                    "digest": report["registry"]["digest"],
                    "ensemble": report["registry"]["ensemble_method"],
                    "books": json.dumps(report["registry"]["books"]),
                    **{
                        f"{sid} params": json.dumps(row["params"], sort_keys=True)
                        for sid, row in report["registry"]["strategies"].items()
                    },
                },
            ),
            ("Baseline (no overlay)", {k: v for k, v in baseline.items() if k != "fold_sharpes"}),
            ("Candidates", lines),
            ("Recommendation (D-017)", {k: json.dumps(v) for k, v in recommendation.items()}),
        ],
    )
    path, digest = _write(out, f"overlay-{_stamp()}", report, markdown)
    for line in lines:
        click.echo(line)
    click.echo(f"recommendation: {json.dumps(recommendation)}")
    click.echo(f"registry digest: {report['registry']['digest']}")
    click.echo(f"report: {path} sha256={digest}")


# D-018: pre-registered acceptance for running a strategy as an independent sleeve (a "small book") next to the
# main book.  Fixed before the first run and never widened after seeing results; every threshold is written into
# the report.  Passing this rule is a *portfolio* decision; it does not create a registry verdict for the sleeve.
BOOK_RULE: dict[str, float] = {
    "min_delta_oos_sharpe": 0.10,  # total book OOS Sharpe minus main-only OOS Sharpe
    "max_oos_mdd_worsening": 0.01,  # total OOS max drawdown may not be worse than main-only by more than 1pp
    "min_fold_win_rate": 0.6,  # total beats main-only in >= 3 of 5 walk-forward folds
    "max_cpcv_negative": 0.10,  # sleeve alone: share of CPCV paths with a negative Sharpe
    "min_cost_x2_sharpe": 0.5,  # sleeve alone: Sharpe with doubled costs
    "min_robustness_delta": 0.0,  # on the robustness universe the sleeve must not subtract OOS Sharpe
}


def _fold_metrics(net: pd.Series, fold_list: Sequence[Fold], bpy: float) -> dict[str, Any]:
    oos = net.iloc[fold_list[0].test_start :]
    return {
        "full_sharpe": sharpe(net, bpy),
        "full_mdd": max_drawdown(net),
        "oos_sharpe": sharpe(oos, bpy),
        "oos_return": compound(oos),
        "oos_mdd": max_drawdown(oos),
        "fold_sharpes": [sharpe(net.iloc[f.test_slice], bpy) for f in fold_list],
    }


def _combine_books(
    main: pd.DataFrame, sleeve: pd.DataFrame, params: PortfolioParams
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Sum two books (``combine_books``: the main book's caps and band on the total) plus cap-binding diagnostics."""
    total = combine_books({"main": main, "sleeve": sleeve}, params)
    columns = main.columns.union(sleeve.columns)
    b = sleeve.reindex(columns=columns)
    raw = main.reindex(columns=columns).fillna(0.0) + b.fillna(0.0)
    gross = raw.clip(-params.max_weight, params.max_weight).abs().sum(axis=1)
    active = b.fillna(0.0) != 0.0
    n_active = max(1, int(active.to_numpy().sum()))
    binding = {
        "symbol_cap_share": float(((raw.abs() > params.max_weight + 1e-12) & active).to_numpy().sum() / n_active),
        "gross_cap_share": float((gross > params.max_gross + 1e-12).mean()),
    }
    return total, binding


def _netting(main: pd.DataFrame, sleeve: pd.DataFrame) -> dict[str, float]:
    """How much of the sleeve's exposure cancels or stacks on the main book (diagnostic)."""
    columns = main.columns.union(sleeve.columns)
    a = main.reindex(columns=columns).fillna(0.0)
    b = sleeve.reindex(columns=columns).fillna(0.0)
    active = b != 0.0
    n_active = max(1, int(active.to_numpy().sum()))
    opposing = int(((np.sign(a) == -np.sign(b)) & active & (a != 0.0)).to_numpy().sum())
    same = int(((np.sign(a) == np.sign(b)) & active).to_numpy().sum())
    cancelled = float((a.abs() + b.abs() - (a + b).abs()).to_numpy().sum())
    sleeve_gross = max(float(b.abs().to_numpy().sum()), 1e-12)
    return {
        "sleeve_symbol_bars": float(n_active),
        "opposing_main_share": opposing / n_active,
        "same_direction_share": same / n_active,
        "main_flat_share": 1.0 - (opposing + same) / n_active,
        "cancelled_gross_share": cancelled / sleeve_gross,
    }


def _trial_signature(record: TrialRecord) -> tuple[str, str, str, int]:
    return (record.param_key, record.range_start, record.range_end, record.symbols)


def _standalone_block(
    strategy: str,
    params: Mapping[str, Any],
    decision_weights: pd.DataFrame,
    result: BacktestResult,
    panel: Panel,
    cost: CostModel,
    fold_list: Sequence[Fold],
    *,
    purge: int,
    cpcv_groups: int,
    prior_trials: int,
    ledger_path: Path,
) -> tuple[dict[str, Any], TrialRecord]:
    """The sleeve on its own, exactly as `research validate` would score a single configuration."""
    bpy = panel.bars_per_year
    net = result.portfolio_net
    key = param_key(dict(params))
    nets = {key: net}
    wf = walk_forward_evaluate(nets, {key: dict(params)}, list(fold_list), bpy).summary(bpy)
    cpcv = cpcv_evaluate(
        nets, cpcv_splits(len(net), n_groups=cpcv_groups, n_test_groups=2, purge=purge, embargo=purge), bpy
    )
    full = sharpe(net, bpy)
    record = TrialRecord(
        strategy=strategy,
        param_key=key,
        sharpe_annual=full,
        bars_per_year=bpy,
        recorded_at=datetime.now(UTC).isoformat(),
        range_start=str(net.index[0]),
        range_end=str(net.index[-1]),
        symbols=len(panel.symbols),
        run_id="",
    )
    records = (
        parse_ledger(ledger_path.read_text(encoding="utf-8").splitlines(), strategy) if ledger_path.exists() else []
    )
    # an exact replay of a recorded configuration on the same data is one trial, not two
    prior_records = [r for r in records if _trial_signature(r) != _trial_signature(record)]
    pooled = dsr_inputs(
        prior_records, {key: None if full is None else full / math.sqrt(bpy)}, bpy, manual_prior_trials=prior_trials
    )
    values = net.to_numpy(dtype=float)
    mt = multiple_testing_report(
        values,
        values.reshape(-1, 1),
        bars_per_year=bpy,
        prior_trials=prior_trials,
        pooled_n_trials=pooled["n_trials"],
        pooled_sharpe_variance=pooled["sharpe_variance"] if pooled["pooled_sharpes"] >= 2 else None,
    )
    mt["ledger_trials"] = pooled["ledger_trials"]
    mt["replayed_trial"] = len(prior_records) != len(records)
    stress = cost_stress(
        {
            multiplier: run_backtest(
                panel,
                decision_weights,
                CostModel(cost.turnover_bps * multiplier, cost.carry_bps_per_bar * multiplier, cost.use_funding),
            ).portfolio_net
            for multiplier in (1.0, 1.5, 2.0)
        },
        bpy,
    )
    block: dict[str, Any] = {
        "params": dict(params),
        "full_sample": result.summary(),
        "walk_forward": {k: v for k, v in wf.items() if k != "chosen_params"},
        "cpcv": {k: v for k, v in cpcv.items() if k != "chosen"},
        "multiple_testing": mt,
        "cost_stress": stress,
    }
    verdict, reasons = decide(block)
    block["verdict"] = verdict
    block["reasons"] = reasons
    return block, record


def _evaluate_book(
    universe_mode: str,
    panel: Panel,
    membership: pd.DataFrame | None,
    main_entry: StrategyEntry,
    sleeve_entry: StrategyEntry,
    portfolio: PortfolioParams,
    fractions: Sequence[float],
    cost: CostModel,
    *,
    interval: str,
    min_history: int,
    folds: int,
    min_train: int,
    purge: int,
    cpcv_groups: int,
    prior_trials: int,
    ledger_path: Path,
) -> tuple[dict[str, Any], TrialRecord]:
    """Main book alone vs main + fraction x sleeve on one universe; fractions[0] is the decision fraction."""
    bpy = panel.bars_per_year
    bare = replace(portfolio, no_trade_band=0.0, no_trade_rel_band=0.0)
    main_model = AlphaModel(entries=(main_entry,), portfolio=bare, interval=interval, min_history_bars=min_history)
    sleeve_model = AlphaModel(entries=(sleeve_entry,), portfolio=bare, interval=interval, min_history_bars=min_history)
    w_main, _mc, _mp = main_model.evaluate(panel, membership)
    w_sleeve, _sc, _sp = sleeve_model.evaluate(panel, membership)

    def banded(weights: pd.DataFrame) -> pd.DataFrame:
        if portfolio.no_trade_band > 0 or portfolio.no_trade_rel_band > 0:
            return apply_no_trade_band(weights, portfolio.no_trade_band, portfolio.no_trade_rel_band)
        return weights

    main_result = run_backtest(panel, banded(w_main), cost)
    sleeve_decision = banded(w_sleeve)
    sleeve_result = run_backtest(panel, sleeve_decision, cost)
    totals: dict[float, tuple[BacktestResult, dict[str, float]]] = {}
    for fraction in fractions:
        combined, binding = _combine_books(w_main, w_sleeve * fraction, portfolio)
        totals[fraction] = (run_backtest(panel, combined, cost), binding)
    index = main_result.portfolio_net.index
    for total_result, _binding in totals.values():
        index = index.intersection(total_result.portfolio_net.index)
    index = index.intersection(sleeve_result.portfolio_net.index)
    n_bars = len(index)
    fold_list = walk_forward_folds(n_bars, folds, min_train=min(min_train, max(n_bars // 2, 2)), purge=purge)
    main_net = main_result.portfolio_net.reindex(index).fillna(0.0)
    sleeve_net = sleeve_result.portfolio_net.reindex(index).fillna(0.0)
    main_metrics = _fold_metrics(main_net, fold_list, bpy)
    standalone, record = _standalone_block(
        sleeve_entry.id,
        sleeve_entry.params,
        sleeve_decision,
        sleeve_result,
        panel,
        cost,
        fold_list,
        purge=purge,
        cpcv_groups=cpcv_groups,
        prior_trials=prior_trials,
        ledger_path=ledger_path,
    )
    oos_start = fold_list[0].test_start
    by_fraction: dict[str, dict[str, Any]] = {}
    for fraction, (total_result, binding) in totals.items():
        total_net = total_result.portfolio_net.reindex(index).fillna(0.0)
        total_metrics = _fold_metrics(total_net, fold_list, bpy)
        deltas = [
            None if t is None or m is None else t - m
            for t, m in zip(total_metrics["fold_sharpes"], main_metrics["fold_sharpes"], strict=True)
        ]
        wins = [d for d in deltas if d is not None]
        main_oos, total_oos = main_metrics["oos_sharpe"], total_metrics["oos_sharpe"]
        by_fraction[f"{fraction:.4f}"] = {
            "fraction": fraction,
            "total": {**total_metrics, "summary": total_result.summary()},
            "cap_binding": binding,
            "marginal": {
                "delta_full_sharpe": (
                    None
                    if total_metrics["full_sharpe"] is None or main_metrics["full_sharpe"] is None
                    else total_metrics["full_sharpe"] - main_metrics["full_sharpe"]
                ),
                "delta_oos_sharpe": None if total_oos is None or main_oos is None else total_oos - main_oos,
                "oos_mdd_worsening": main_metrics["oos_mdd"] - total_metrics["oos_mdd"],
                "delta_oos_return": total_metrics["oos_return"] - main_metrics["oos_return"],
                "fold_deltas": deltas,
                "fold_win_rate": float(np.mean([d > 0 for d in wins])) if wins else None,
            },
        }
    sleeve_scaled = run_backtest(panel, banded(w_sleeve * fractions[0]), cost)
    payload: dict[str, Any] = {
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "range": {"start": str(index[0]), "end": str(index[-1]), "bars": n_bars, "oos_start": str(index[oos_start])},
        "main_only": {**main_metrics, "summary": main_result.summary()},
        "sleeve_standalone": standalone,
        "sleeve_scaled_summary": sleeve_scaled.summary(),
        "correlation": {
            "full": float(main_net.corr(sleeve_net)),
            "oos": float(main_net.iloc[oos_start:].corr(sleeve_net.iloc[oos_start:])),
        },
        "netting": _netting(w_main, w_sleeve * fractions[0]),
        "by_fraction": by_fraction,
        "yearly_marginal": {
            year: {"main": main_row["return"], "total": total_row["return"], "sleeve_alone": sleeve_row["return"]}
            for (year, main_row), total_row, sleeve_row in zip(
                yearly_breakdown(main_net, bpy).items(),
                yearly_breakdown(totals[fractions[0]][0].portfolio_net.reindex(index).fillna(0.0), bpy).values(),
                yearly_breakdown(sleeve_net * fractions[0], bpy).values(),
                strict=True,
            )
        },
    }
    return payload, record


def _record_trial(ledger_path: Path, record: TrialRecord) -> bool:
    """Append a trial unless the identical configuration on the identical data is already in the ledger."""
    existing = (
        parse_ledger(ledger_path.read_text(encoding="utf-8").splitlines(), record.strategy)
        if ledger_path.exists()
        else []
    )
    if any(_trial_signature(r) == _trial_signature(record) for r in existing):
        return False
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json() + "\n")
    return True


@research.command("book")
@click.option("--main", "main_id", default="tsmom", show_default=True, help="main book strategy id (registry params)")
@click.option("--sleeve", "sleeve_id", required=True, help="candidate sleeve strategy id")
@click.option("--sleeve-params", default="", help="JSON overriding the sleeve's registry/default params")
@click.option(
    "--fraction",
    default=1.0 / 3.0,
    show_default=True,
    type=float,
    help="sleeve risk budget as a fraction of the main book's vol target and caps (one pre-registered value, no grid)",
)
@click.option(
    "--sensitivity",
    default="0.2,0.5",
    show_default=True,
    help="extra fractions reported for context only; never a decision input and never a ledger trial",
)
@click.option("--root", default=".beidou/data", show_default=True)
@click.option("--symbols", default="", help="comma-separated; default = selected universe or all stored")
@click.option("--interval", default="1h", show_default=True)
@click.option("--from", "start", default=None, help="YYYY-MM-DD inclusive")
@click.option("--to", "end", default=None, help="YYYY-MM-DD exclusive")
@click.option("--profile", default="config/live.demo.yaml", show_default=True)
@click.option("--registry", "registry_path", default="config/alpha_registry.yaml", show_default=True)
@click.option("--costs", "costs_path", default="config/costs.yaml", show_default=True)
@click.option("--funding/--no-funding", default=True, show_default=True)
@click.option("--out", default="reports/research", show_default=True)
@click.option("--min-history", default=None, type=int)
@click.option("--universe", "universe_mode", type=click.Choice(list(UNIVERSE_MODES)), default="pit", show_default=True)
@click.option(
    "--robustness",
    "robustness_mode",
    type=click.Choice(["none", *UNIVERSE_MODES]),
    default="none",
    show_default=True,
    help="second universe on which the sleeve must not subtract OOS Sharpe (D-018)",
)
@click.option("--folds", default=5, show_default=True)
@click.option("--min-train", default=4000, show_default=True)
@click.option("--purge", default=50, show_default=True)
@click.option("--cpcv-groups", default=6, show_default=True)
@click.option(
    "--prior-trials",
    default=0,
    show_default=True,
    help="sleeve configurations evaluated before the ledger existed (charged in the standalone DSR)",
)
def research_book(
    main_id: str,
    sleeve_id: str,
    sleeve_params: str,
    fraction: float,
    sensitivity: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    robustness_mode: str,
    folds: int,
    min_train: int,
    purge: int,
    cpcv_groups: int,
    prior_trials: int,
) -> None:
    """Evidence for running SLEEVE as an independent small book next to MAIN (D-018).

    Books are summed (each with its own portfolio construction; the sleeve scaled by --fraction), then the
    main book's caps and no-trade band apply to the total.  The report compares main-only with main+sleeve
    out of sample and scores the sleeve on its own exactly like `research validate`.
    """
    if not 0 < fraction <= 1:
        raise click.ClickException("--fraction must be in (0, 1]")
    extra = [float(v) for v in sensitivity.split(",") if v.strip()]
    fractions = [fraction, *[f for f in extra if f != fraction]]
    profile_payload = load_yaml(profile)
    portfolio = portfolio_params(profile_payload)
    history = (
        min_history
        if min_history is not None
        else int((profile_payload.get("portfolio", {}) or {}).get("min_history_bars", 720))
    )
    main_entry = _entry(main_id, registry_path, "")
    sleeve_entry = _entry(sleeve_id, registry_path, sleeve_params)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    ledger_path = Path(out) / "trials.jsonl"
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    _require_funding([main_entry, sleeve_entry], panel)
    universes: list[tuple[str, Panel, pd.DataFrame | None]] = [
        (universe_mode, panel, _membership(root, universe_mode, panel))
    ]
    if robustness_mode not in {"none", universe_mode}:
        if robustness_mode == "static":
            static = [s for s in read_universe(root) if s in panel.close.columns]
            if not static:
                raise click.ClickException("robustness universe 'static' needs a selected universe in the store")
            universes.append(("static", panel.select(static), None))
        else:
            pit_panel = _load(root, _resolve_symbols(root, "", interval, "pit"), interval, start, end, funding)
            universes.append(("pit", pit_panel, _membership(root, "pit", pit_panel)))
    evaluated: dict[str, dict[str, Any]] = {}
    records: list[TrialRecord] = []
    for mode, mode_panel, membership in universes:
        click.echo(
            f"[{mode}] {main_id} + {fraction:.3f} x {sleeve_id} on {len(mode_panel.symbols)} symbols x "
            f"{len(mode_panel.index)} bars"
        )
        payload, record = _evaluate_book(
            mode,
            mode_panel,
            membership,
            main_entry,
            sleeve_entry,
            portfolio,
            fractions,
            cost,
            interval=interval,
            min_history=history,
            folds=folds,
            min_train=min_train,
            purge=purge,
            cpcv_groups=cpcv_groups,
            prior_trials=prior_trials,
            ledger_path=ledger_path,
        )
        evaluated[mode] = payload
        records.append(record)
    decision = evaluated[universe_mode]
    chosen_fraction = decision["by_fraction"][f"{fraction:.4f}"]
    marginal = chosen_fraction["marginal"]
    standalone = decision["sleeve_standalone"]
    robustness = None if robustness_mode in {"none", universe_mode} else evaluated[robustness_mode]
    robustness_delta = (
        None if robustness is None else robustness["by_fraction"][f"{fraction:.4f}"]["marginal"]["delta_oos_sharpe"]
    )
    checks: dict[str, bool | None] = {
        "delta_oos_sharpe": (
            marginal["delta_oos_sharpe"] is not None
            and marginal["delta_oos_sharpe"] >= BOOK_RULE["min_delta_oos_sharpe"]
        ),
        "oos_mdd_worsening": marginal["oos_mdd_worsening"] <= BOOK_RULE["max_oos_mdd_worsening"],
        "fold_win_rate": (
            marginal["fold_win_rate"] is not None and marginal["fold_win_rate"] >= BOOK_RULE["min_fold_win_rate"]
        ),
        "cpcv_negative": (
            standalone["cpcv"]["fraction_negative"] is not None
            and standalone["cpcv"]["fraction_negative"] <= BOOK_RULE["max_cpcv_negative"]
        ),
        "cost_x2_sharpe": (
            standalone["cost_stress"]["x2"] is not None
            and standalone["cost_stress"]["x2"] >= BOOK_RULE["min_cost_x2_sharpe"]
        ),
        "robustness_delta": (
            None if robustness_delta is None else bool(robustness_delta >= BOOK_RULE["min_robustness_delta"])
        ),
    }
    reasons = [name for name, ok in checks.items() if ok is False]
    notes = ["robustness universe not evaluated"] if checks["robustness_delta"] is None else []
    book_verdict = "ACCEPT" if not reasons else "REJECT"
    report: dict[str, Any] = {
        "kind": "book",
        "main": {"strategy": main_id, "params": main_entry.params},
        "sleeve": {"strategy": sleeve_id, "params": sleeve_entry.params},
        "fraction": fraction,
        "sensitivity_fractions": [f for f in fractions if f != fraction],
        "combination": {
            "rule": "total = main + fraction x sleeve (each book vol-targeted on its own), then the main book's "
            "per-symbol cap, gross cap and no-trade band on the total",
            "max_weight": portfolio.max_weight,
            "max_gross": portfolio.max_gross,
            "no_trade_band": portfolio.no_trade_band,
            "no_trade_rel_band": portfolio.no_trade_rel_band,
        },
        "interval": interval,
        "universe_mode": universe_mode,
        "robustness_universe": None if robustness is None else robustness_mode,
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts([main_entry, sleeve_entry], panel),
        "folds": folds,
        "min_train": min_train,
        "purge": purge,
        "prior_trials": prior_trials,
        "universes": evaluated,
        "rule": BOOK_RULE,
        "checks": checks,
        "book_verdict": book_verdict,
        "reasons": reasons,
        "notes": notes,
        "sleeve_standalone_verdict": standalone["verdict"],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    main_only = decision["main_only"]
    total = chosen_fraction["total"]
    sections: list[tuple[str, Any]] = [
        ("Range", decision["range"]),
        (
            "Books",
            {
                "main": main_id,
                "sleeve": sleeve_id,
                "sleeve_params": json.dumps(sleeve_entry.params, sort_keys=True),
                "fraction": fraction,
                "universe": universe_mode,
                "robustness_universe": report["robustness_universe"] or "none",
            },
        ),
        ("Main book alone", {k: v for k, v in main_only.items() if k != "summary"}),
        (
            "Sleeve alone (as `research validate` would score it)",
            {
                "full_sharpe": standalone["full_sample"]["annualized_sharpe"],
                "full_mdd": standalone["full_sample"]["max_drawdown"],
                "average_absolute_exposure": standalone["full_sample"]["average_absolute_exposure"],
                "oos_sharpe": standalone["walk_forward"]["oos_sharpe"],
                "fold_sharpes": standalone["walk_forward"]["fold_sharpes"],
                "cpcv_mean_q05_negative": [
                    standalone["cpcv"]["oos_sharpe_mean"],
                    standalone["cpcv"]["oos_sharpe_q05"],
                    standalone["cpcv"]["fraction_negative"],
                ],
                "dsr_p_value": standalone["multiple_testing"]["dsr_p_value"],
                "n_trials": standalone["multiple_testing"]["n_trials"],
                "cost_x2_sharpe": standalone["cost_stress"]["x2"],
                "standalone_verdict": standalone["verdict"],
                "standalone_reasons": standalone["reasons"] or ["-"],
            },
        ),
        ("Total book (main + fraction x sleeve)", {k: v for k, v in total.items() if k != "summary"}),
        (
            "Marginal",
            {
                **marginal,
                "correlation_full": decision["correlation"]["full"],
                "correlation_oos": decision["correlation"]["oos"],
                "sleeve_scaled_exposure": decision["sleeve_scaled_summary"]["average_absolute_exposure"],
            },
        ),
        ("Netting and caps", {**decision["netting"], **chosen_fraction["cap_binding"]}),
        (
            "Sensitivity (context only)",
            {
                key: {
                    "delta_oos_sharpe": row["marginal"]["delta_oos_sharpe"],
                    "oos_mdd_worsening": row["marginal"]["oos_mdd_worsening"],
                    "fold_win_rate": row["marginal"]["fold_win_rate"],
                }
                for key, row in decision["by_fraction"].items()
            },
        ),
        (
            "Robustness universe",
            {"universe": robustness_mode, "delta_oos_sharpe": robustness_delta}
            if robustness is not None
            else {"universe": "none"},
        ),
        ("Yearly returns (main / total / sleeve alone at fraction)", decision["yearly_marginal"]),
        ("Rule (D-018)", BOOK_RULE),
        ("Checks", checks),
        ("Verdict", {"book_verdict": book_verdict, "reasons": reasons or ["-"], "notes": notes or ["-"]}),
    ]
    markdown = render_markdown(f"Book evidence: {main_id} + {fraction:.3f} x {sleeve_id} — {book_verdict}", sections)
    path, digest = _write(out, f"book-{main_id}-{sleeve_id}-{_stamp()}", report, markdown)
    for record in records:
        recorded = _record_trial(ledger_path, replace(record, run_id=path.stem))
        click.echo(
            f"trials ledger: {sleeve_id} standalone on {record.symbols} symbols "
            f"{'recorded' if recorded else 'already recorded (exact replay, not charged twice)'}"
        )
    click.echo(
        f"main-only oos_sharpe={_fmt(main_only['oos_sharpe'])} oos_mdd={main_only['oos_mdd']:.3f} | "
        f"total oos_sharpe={_fmt(total['oos_sharpe'])} oos_mdd={total['oos_mdd']:.3f} | "
        f"delta={_fmt(marginal['delta_oos_sharpe'])} folds_won={_fmt(marginal['fold_win_rate'])}"
    )
    click.echo(
        f"sleeve alone: oos_sharpe={_fmt(standalone['walk_forward']['oos_sharpe'])} "
        f"dsr_p={_fmt(standalone['multiple_testing']['dsr_p_value'])} "
        f"(n_trials {standalone['multiple_testing']['n_trials']}) verdict={standalone['verdict']}"
    )
    if robustness_delta is not None:
        click.echo(f"robustness [{robustness_mode}] delta_oos_sharpe={_fmt(robustness_delta)}")
    click.echo(f"checks: {json.dumps(checks)}")
    click.echo(f"BOOK VERDICT: {book_verdict} {reasons if reasons else ''} {notes if notes else ''}")
    click.echo(f"report: {path} sha256={digest}")


def _grid_table(params_by_key: Mapping[str, Mapping[str, Any]], sharpes: Mapping[str, float | None]) -> list[str]:
    """Every configuration tried in this run with its full-sample Sharpe, varying parameters only.

    The plan asked for a parameter stability view; the report printed a single
    worst-neighbour number and hid the grid it already had in ``trial_sharpes``.
    Only parameters that actually differ across the grid are shown, so a
    one-configuration run renders a single line instead of thirteen defaults.
    """
    if not params_by_key:
        return []
    keys = sorted({key for params in params_by_key.values() for key in params})
    varying = [k for k in keys if len({str(p.get(k)) for p in params_by_key.values()}) > 1]
    ranked = sorted(params_by_key, key=lambda k: (sharpes.get(k) is None, -(sharpes.get(k) or 0.0)))
    lines = []
    for key in ranked:
        params = params_by_key[key]
        shown = ", ".join(f"{k}={params.get(k)}" for k in varying) if varying else "single configuration"
        lines.append(f"{shown}: sharpe={_fmt(sharpes.get(key))}")
    return lines


def _grid_of(grid: Mapping[str, list[Any]]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*(grid[key] for key in keys))]


@research.command("decompose")
@_common_options
@click.option("--folds", default=5, show_default=True)
@click.option("--min-train", default=4000, show_default=True)
@click.option("--purge", default=50, show_default=True)
def research_decompose(
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    folds: int,
    min_train: int,
    purge: int,
) -> None:
    """Signal-vs-construction attribution (D-024): the same pipeline on controlled convictions; not a ledger trial."""
    del execution  # the decomposition uses the open_to_close convention of the validation reports
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    _require_funding([entry], panel)
    membership = _membership(root, universe_mode, panel, min_tenure)
    model = _model(entry, profile_payload, interval, min_history)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    payload = decompose_book(model, panel, cost, membership=membership, folds=folds, min_train=min_train, purge=purge)
    report: dict[str, Any] = {
        "kind": "decompose",
        "strategy": strategy,
        "params": entry.params,
        "portfolio": model.portfolio.__dict__,
        "interval": interval,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts([entry], panel),
        **payload,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    lines = [
        f"{name}: sharpe={_fmt(row['full_sharpe'])} oos={_fmt(row['oos_sharpe'])} net={row['net_return']:.3f} "
        f"mdd={row['max_drawdown']:.3f} turnover={row['turnover_units']:.0f} "
        f"exposure={row['average_absolute_exposure']:.3f} corr_full={_fmt(row['correlation_with_full'])} "
        f"folds={[round(x, 2) if x is not None else None for x in row['fold_sharpes']]}"
        for name, row in payload["variants"].items()
    ]
    markdown = render_markdown(
        f"Decomposition: {strategy}",
        [
            ("Range", payload["range"]),
            ("Params", entry.params),
            ("Variants (same portfolio construction, different convictions)", lines),
            ("Increments (Sharpe)", payload["increments"]),
            ("Legs of the full book", payload["legs"]),
            ("Benchmark (equal-weight long, zero cost)", payload["benchmark"]),
        ],
    )
    path, digest = _write(out, f"decompose-{strategy}-{_stamp()}", report, markdown)
    for line in lines:
        click.echo(line)
    click.echo(f"increments: {json.dumps(payload['increments'])}")
    click.echo(f"report: {path} sha256={digest}")


@research.command("mine")
@_common_options
@click.option("--top", default=12, show_default=True, help="candidates to print, ranked by full-sample Sharpe")
@click.option("--max-complexity", default=10, show_default=True)
@click.option("--max-lookback", default=1400, show_default=True, help="more than the loop can fetch is a bug (E-042)")
@click.option(
    "--include-funding/--no-include-funding",
    default=True,
    show_default=True,
    help="search the carry family (needs the funding panel)",
)
@click.option("--baseline", default="", help="strategy id to compare each candidate against (registry params)")
@click.option("--baseline-params", default="", help="JSON overriding the baseline's registry params")
@click.option("--grids", default="", help="JSON of enumerate_candidates grids, e.g. '{\"horizons\": [1, 3, 7]}'")
def research_mine(
    strategy: str,
    params: str,
    root: str,
    symbols: str,
    interval: str,
    start: str | None,
    end: str | None,
    profile: str,
    registry_path: str,
    costs_path: str,
    execution: str,
    funding: bool,
    out: str,
    min_history: int | None,
    universe_mode: str,
    min_tenure: int,
    top: int,
    max_complexity: int,
    max_lookback: int,
    include_funding: bool,
    baseline: str,
    baseline_params: str,
    grids: str,
) -> None:
    """Enumerate candidate expressions and rank them full-sample.  This produces a SHORTLIST, not evidence.

    Nothing here touches the trials ledger, because nothing here is a verdict: ranking hundreds of
    expressions on the full sample *is* selection, and filing the winner as a result would be the mistake
    D-020 exists to prevent.  What it prints instead is how many distinct expressions the search
    evaluated - pass that to ``research validate --prior-trials`` when promoting one, or the DSR
    denominator will never learn the search happened.  ``--strategy`` is inherited from the shared
    options and ignored here; the candidates are the strategies.
    """
    profile_payload = load_yaml(profile)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    membership = _membership(root, universe_mode, panel, min_tenure)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    portfolio = portfolio_params(profile_payload)
    history = (
        min_history
        if min_history is not None
        else int((profile_payload.get("portfolio", {}) or {}).get("min_history_bars", 720))
    )
    # F-1.  The search space narrows to what this panel can actually answer; it does not refuse, because a
    # refusal makes `mine` unrunnable on the OHLCV-only panels the rest of the suite uses.
    #
    # The predicate is the panel, never the flag, and never `panel.funding is not None` either.  `--funding`
    # against a store with no funding archive yields an all-zero frame rather than None, so both of those
    # weaker tests pass while every carry candidate evaluates to a constant: 42 expressions kept, charged to
    # `declared_trials`, scored on zeros, and a report recording `include_funding: true`.  That is KILL-027
    # standing inside the guard written to prevent it - an artefact asserting a family was searched when it
    # was not.  `panel.settled_symbols` is the quantity that answers it, and `_require_funding` below is the
    # backstop for anything this narrowing lets through - a `--baseline` in particular.
    searched_funding = include_funding and panel.settled_symbols > 0
    # Every grid is a bar COUNT, so the same search at another interval needs them rescaled: at 1d the
    # default `horizons` of 24..720 mean 24..720 DAYS, and `max_lookback` 1400 outruns the sample.  Keys
    # are checked against the signature rather than splatted blind - a typo would search the default
    # space while the report's own `run.grids` claimed otherwise, which is the failure this block exists
    # to make impossible.
    grid_overrides: dict[str, Any] = json.loads(grids) if grids else {}
    allowed = set(inspect.signature(enumerate_candidates).parameters)
    unknown = sorted(set(grid_overrides) - allowed)
    if unknown:
        raise click.ClickException(f"--grids has no such parameter(s): {', '.join(unknown)}; known: {sorted(allowed)}")
    # A key that is also a flag would be passed twice, and the interface should say which one to use
    # rather than let the call fail on a duplicate keyword - the run block records `grids` verbatim, so
    # an instruction the tool cannot obey must be refused where it is written.
    collide = sorted(set(grid_overrides) & {"max_complexity", "max_lookback", "include_funding"})
    if collide:
        raise click.ClickException(
            f"--grids must not set {', '.join(collide)}; each has its own flag (--{collide[0].replace('_', '-')})"
        )
    if include_funding and not searched_funding:
        click.echo(
            "no settlement in this panel: narrowing the search space, the carry family is neither searched "
            "nor charged to --prior-trials (pass --funding, or --no-include-funding to silence this)"
        )
    baseline_net: pd.Series | None = None
    if baseline:
        _resolve_mined(baseline)  # a mined candidate is addressable by its hash, like any other id
        # The baseline every marginal is measured against must be expressible at this interval too:
        # `--baseline tsmom --interval 1d` on registry params would compare each candidate to a
        # two-year-horizon book, because tsmom's horizons are bar counts.
        baseline_entry = _entry(baseline, registry_path, baseline_params)
        # Every candidate's marginal is measured against this book, so a baseline running its modifier
        # inert would corrupt the whole column (E-040 / KILL-027).  Same refusal every other command uses.
        _require_funding([baseline_entry], panel)
        baseline_model = _model(baseline_entry, profile_payload, interval, history)
        baseline_weights, _bc, _bp = baseline_model.evaluate(panel, membership)
        # Guard-free on purpose, byte-identical to the candidates' call below: the shortlist buys internal
        # consistency, not realism.  The daily-loss pause reads the equity path it is producing, so
        # replaying it on one leg only would leak that replay into every candidate's correlation.
        baseline_net = run_backtest(panel, baseline_weights, cost, execution=execution).portfolio_net  # type: ignore[arg-type]
    search = enumerate_candidates(
        max_complexity=max_complexity,
        max_lookback=max_lookback,
        include_funding=searched_funding,
        **grid_overrides,
    )
    click.echo(
        f"search: evaluated {search.evaluated} distinct expressions, kept {len(search.candidates)} "
        f"({json.dumps(search.rejected)}) on {len(panel.symbols)} symbols x {len(panel.index)} bars"
    )
    # The candidates ARE the strategies here, so the funding check belongs after enumeration rather than
    # on `--strategy` (which this command ignores).  It has to happen before the loop: a family that reads
    # funding would otherwise land in the `error` rows below and the command would still exit 0, writing a
    # shortlist whose `declared_trials` counts candidates that were never scored - and that count is what
    # `research validate --prior-trials` feeds into the DSR denominator.
    specs = [register_signal(to_signal(candidate)) for candidate in search.candidates]
    entries = [StrategyEntry(id=spec.id, params=dict(spec.default_params)) for spec in specs]
    _require_funding(entries, panel)
    rows: list[dict[str, Any]] = []
    for candidate, entry in zip(search.candidates, entries, strict=True):
        model = AlphaModel(entries=(entry,), portfolio=portfolio, interval=interval, min_history_bars=history)
        try:
            weights, _combined, _per = model.evaluate(panel, membership)
            result = run_backtest(panel, weights, cost, execution=execution)  # type: ignore[arg-type]
        except FundingUnavailable:
            raise  # belt and braces: the run has no funding, which is not this candidate being unscoreable
        except Exception as exc:  # a candidate that cannot be evaluated is dropped, never silently scored
            rows.append({**candidate.to_dict(), "error": f"{type(exc).__name__}: {exc}"})
            continue
        net = result.portfolio_net
        row = {
            **candidate.to_dict(),
            "sharpe": sharpe(net, panel.bars_per_year),
            "net_return": compound(net),
            "max_drawdown": max_drawdown(net),
            "turnover": float(result.turnover.sum()),
        }
        if baseline_net is not None:
            # Aligned per candidate, not once: lookbacks run from 25 to ~1,400 bars, so a baseline Sharpe
            # precomputed over its own index would subtract two numbers measured over different regimes.
            frame = pd.DataFrame({"baseline": baseline_net, "candidate": net}).dropna(how="all").fillna(0.0)
            correlation = float(frame["baseline"].corr(frame["candidate"]))
            combined = sharpe(frame.mean(axis=1), panel.bars_per_year)
            alone = sharpe(frame["baseline"], panel.bars_per_year)
            row["baseline_correlation"] = None if pd.isna(correlation) else correlation
            row["baseline_marginal_sharpe"] = None if combined is None or alone is None else combined - alone
            row["baseline_bars"] = len(frame)
        rows.append(row)
    scored = [row for row in rows if row.get("sharpe") is not None]
    # With a baseline the question is a SECOND, uncorrelated book, so the shortlist ranks on the marginal.
    # Ranking on the full-sample Sharpe is the exact quantity that produces a false "the space is empty"
    # verdict: the best absolute candidate is usually the one most correlated with the book already
    # running, and it is also the maximum of a few hundred noisy draws.  Rows whose marginal could not be
    # computed sort last rather than falling back to a number on a different scale.
    ranked_by = "baseline_marginal_sharpe" if baseline else "sharpe"
    scored.sort(
        key=lambda row: float(row[ranked_by]) if row.get(ranked_by) is not None else float("-inf"), reverse=True
    )
    # A candidate whose net never varies gets `sharpe` None and no "error" key, so it leaves the table
    # without leaving a trace while still being charged to `declared_trials`.  Counted rather than
    # inferred: a family that enumerated but never traded should be visible in the artefact.
    never_traded = [row for row in rows if "error" not in row and row.get("sharpe") is None]
    failed = [row for row in rows if "error" in row]
    # Every counted expression lands in exactly one bucket, checked rather than described.  The
    # pre-registered rule asked for `scored == evaluated`, which no run can satisfy: `evaluated` fires
    # before the complexity and lookback caps, so anything they drop is charged and never scored.  This
    # is the achievable form of the same intent - nothing disappears without a bucket - and it is
    # enforced here because an artefact whose own arithmetic does not close should not be written.
    dropped_by_caps = search.rejected["too_complex"] + search.rejected["too_long"]
    accounted = dropped_by_caps + len(scored) + len(failed) + len(never_traded)
    if accounted != search.evaluated:
        raise click.ClickException(
            f"the run does not account for itself: {accounted} bucketed against {search.evaluated} evaluated"
        )
    payload: dict[str, Any] = {
        "kind": "mine-shortlist",
        "generated_at": datetime.now(UTC).isoformat(),
        # Everything that moves a number in this report, so the JSON alone rebuilds the command line.
        # P14's shortlist recorded none of it, and recovering what it actually ran - vol_target 0.15 with
        # funding charged - took a four-arm reproduction rather than a read (D-024 applied to `mine`).
        "run": {
            "funding": funding,
            # What was searched against what was asked for; the settlement count that decides between
            # them is `funding_inputs.symbols_settled`, recorded once at the top level like every report.
            "include_funding": searched_funding,
            "include_funding_requested": include_funding,
            "execution": execution,
            "universe_mode": universe_mode,
            "min_tenure": min_tenure,
            "interval": interval,
            "start": start,
            "end": end,
            "min_history_bars": history,  # resolved, not the raw option, which is None by default
            "portfolio": portfolio.__dict__,  # the cost model is the top-level `costs`, as in every report
            "max_complexity": max_complexity,
            "max_lookback": max_lookback,
            "grids": grid_overrides,  # {} means the declared defaults; the space searched, stated
            "baseline_params": json.loads(baseline_params) if baseline_params else {},
            "top": top,
            "ranked_by": ranked_by,
            "baseline": baseline or None,
            "profile": profile,
            "costs_path": costs_path,
            "registry_path": registry_path,
            "root": root,
            "symbols_arg": symbols,
        },
        "universe_mode": universe_mode,
        "declared_trials": search.declared_trials,
        "evaluated": search.evaluated,
        "rejected": search.rejected,
        "symbols": panel.symbols,
        "range": [str(panel.index[0]), str(panel.index[-1])],
        "costs": cost.__dict__,
        "funding_inputs": _funding_facts(entries, panel),
        "dataset": build_manifest(root, interval).to_dict(),
        "outcomes": {
            "evaluated": search.evaluated,
            "dropped_by_caps": dropped_by_caps,  # counted before the complexity/lookback checks fire
            "scored": len(scored),
            "errored": len(failed),
            "never_traded": len(never_traded),  # enumerated, charged, but no Sharpe to rank
            "accounted": accounted,  # == evaluated, enforced above
        },
        "candidates": rows,
        "baseline": (
            None
            if not baseline
            else {
                "strategy": baseline,
                # Which configuration the baseline actually ran under.  Naming the strategy is not enough:
                # the registry moves, and a marginal measured against tsmom-with-crowding is a different
                # number from one measured against tsmom-without, with nothing in the artefact to tell
                # them apart.
                "params": dict(baseline_entry.params),
                "sharpe": sharpe(baseline_net, panel.bars_per_year) if baseline_net is not None else None,
                "net_return": compound(baseline_net) if baseline_net is not None else None,
                "max_drawdown": max_drawdown(baseline_net) if baseline_net is not None else None,
                "marginal": (
                    "equal-weight two-stream: sharpe(mean(baseline, candidate)) - sharpe(baseline); NOT the "
                    "risk-budgeted, fold-aware D-018 marginal that `research book` computes"
                ),
            }
        ),
        "not_evidence": (
            "full-sample ranking over a search; promote a candidate only through `research validate "
            f"--prior-trials {search.declared_trials}` on the point-in-time universe (D-013/D-020)"
        ),
    }
    lines = [
        f"| {row['hash']} | {row['sharpe']:.3f} | {row['max_drawdown']:.3f} | "
        f"{_fmt(row.get('baseline_marginal_sharpe'))} | {_fmt(row.get('baseline_correlation'))} | "
        f"{row['expression']} |"
        for row in scored[:top]
    ]
    markdown = "\n".join(
        [
            "| hash | sharpe | mdd | marginal | corr | expression |",
            "| --- | --- | --- | --- | --- | --- |",
            *lines,
        ]
    )
    # Stamped, because `mine` was the only unstamped name here: once the payload records the flags, two
    # runs differing only in them would overwrite each other's evidence at one path.
    path, digest = _write(out, f"mine-shortlist-{_stamp()}", payload, markdown)
    for row in scored[:top]:
        click.echo(
            f"  {row['hash']}  sharpe={row['sharpe']:6.3f}  mdd={row['max_drawdown']:7.3f}  "
            f"turnover={row['turnover']:8.1f}  lookback={row['lookback']:>4}  {row['expression']}"
        )
    if failed:
        click.echo(f"{len(failed)} candidate(s) could not be evaluated; see the report")
    if never_traded:
        click.echo(
            f"{len(never_traded)} candidate(s) enumerated but never traded (zero-variance net, so no Sharpe): "
            "charged to --prior-trials, absent from the table"
        )
    click.echo(f"declare --prior-trials {search.declared_trials} when validating any of these")
    click.echo(f"report: {path} sha256={digest}")
