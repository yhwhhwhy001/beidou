"""``beidou research ...`` commands: backtest, validate, list."""

from __future__ import annotations

import hashlib
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
from beidou_alpha.model import AlphaModel
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.overlays.exposure import DrawdownThrottleParams, apply_drawdown_throttle
from beidou_alpha.panel import Panel, interval_seconds
from beidou_alpha.portfolio import PortfolioParams, apply_no_trade_band, combine_books
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.report import canonical_json, render_markdown
from beidou_alpha.signals import SIGNALS, get_signal
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
    time_series_ic,
    yearly_breakdown,
)
from beidou_alpha.validation.multiple_testing import multiple_testing_report
from beidou_alpha.validation.stability import cost_stress, parameter_neighborhood, time_split_sharpes
from beidou_alpha.validation.verdict import decide
from beidou_alpha.validation.walk_forward import Fold, param_key, walk_forward_evaluate, walk_forward_folds
from beidou_cli import research
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


def _entry(strategy: str, registry_path: str, params: str) -> StrategyEntry:
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
def research_backtest(
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
    membership = _membership(root, universe_mode, panel, min_tenure)
    model = _model(entry, profile_payload, interval, min_history)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    weights, _combined, _per = model.evaluate(panel, membership)
    result = run_backtest(panel, weights, cost, execution=execution)  # type: ignore[arg-type]
    summary = result.summary()
    bench = benchmark_returns(panel, execution, panel.symbols).reindex(result.weights.index)  # type: ignore[arg-type]
    report: dict[str, Any] = {
        "kind": "backtest",
        "strategy": strategy,
        "params": entry.params,
        "portfolio": model.portfolio.__dict__,
        "interval": interval,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "range": {"start": str(result.weights.index[0]), "end": str(result.weights.index[-1]), "bars": summary["bars"]},
        "costs": cost.__dict__,
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


def _echo_summary(summary: dict[str, Any], benchmark: dict[str, Any] | None = None) -> None:
    click.echo(
        f"bars={summary['bars']} gross={summary['gross_return']:.4f} net={summary['net_return']:.4f} "
        f"sharpe={_fmt(summary['annualized_sharpe'])} mdd={summary['max_drawdown']:.4f} "
        f"turnover={summary['turnover_units']:.1f} exposure={summary['average_absolute_exposure']:.3f} "
        f"cost_share={_fmt(summary['cost_share_of_gross'])}"
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
) -> None:
    """Walk-forward + CPCV + DSR/PBO + stability for one strategy; writes the evidence report for the registry."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
    membership = _membership(root, universe_mode, panel, min_tenure)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    combos = _grid(strategy, grid, entry.params)
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
        "symbols": panel.symbols,
        "range": {"start": str(common_index[0]), "end": str(common_index[-1]), "bars": n_bars},
        "costs": cost.__dict__,
        "execution": execution,
        "grid_size": len(combos),
        "grid": json.loads(grid) if grid else DEFAULT_GRIDS.get(strategy, {}),
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
        "walk_forward": wf_summary,
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
            ("Best params (full sample)", params_by_key[best_key]),
            ("Full sample", report["full_sample"]),
            ("Walk-forward (out of sample)", {k: v for k, v in wf_summary.items() if k != "chosen_params"}),
            ("CPCV", {k: v for k, v in cpcv.items() if k != "chosen"}),
            ("Multiple testing", mt),
            (
                "Stability",
                {
                    "time_split_sharpes": report["stability"]["time_split_sharpes"],
                    "worst_neighbour_degradation": neighbourhood["worst_degradation"],
                },
            ),
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
    click.echo(
        f"cpcv mean={_fmt(cpcv['oos_sharpe_mean'])} q05={_fmt(cpcv['oos_sharpe_q05'])} negative={_fmt(cpcv['fraction_negative'])}"
    )
    click.echo(
        f"dsr p={_fmt(mt['dsr_p_value'])} pbo={_fmt(mt['pbo'])} cost_stress={ {k: _fmt(v) for k, v in stress.items()} }"
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
    membership = _membership(root, universe_mode, panel)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    nets: dict[str, pd.Series] = {}
    for strategy in ids:
        entry = _entry(strategy, registry_path, "")
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
        model = AlphaModel(
            entries=model.entries,
            portfolio=model.portfolio,
            interval=model.interval,
            ensemble_method=model.ensemble_method,
            min_history_bars=min_history,
        )
    chosen = _resolve_symbols(root, symbols, interval, universe_mode)
    panel = _load(root, chosen, interval, start, end, funding)
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
        "interval": interval,
        "universe_mode": universe_mode,
        "symbols": panel.symbols,
        "range": {"start": str(base.weights.index[0]), "end": str(base.weights.index[-1]), "bars": len(base.weights)},
        "costs": cost.__dict__,
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
            ("Baseline (no overlay)", {k: v for k, v in baseline.items() if k != "fold_sharpes"}),
            ("Candidates", lines),
            ("Recommendation (D-017)", {k: json.dumps(v) for k, v in recommendation.items()}),
        ],
    )
    path, digest = _write(out, f"overlay-{_stamp()}", report, markdown)
    for line in lines:
        click.echo(line)
    click.echo(f"recommendation: {json.dumps(recommendation)}")
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
