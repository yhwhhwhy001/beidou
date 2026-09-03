"""``beidou research ...`` commands: backtest, validate, list."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import numpy as np
import pandas as pd

from beidou_alpha.backtest import BacktestResult, CostModel, benchmark_returns, run_backtest
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.report import canonical_json, render_markdown
from beidou_alpha.signals import SIGNALS, get_signal
from beidou_alpha.validation.cpcv import cpcv_evaluate, cpcv_splits
from beidou_alpha.validation.metrics import compound, sharpe, yearly_breakdown
from beidou_alpha.validation.multiple_testing import multiple_testing_report
from beidou_alpha.validation.stability import cost_stress, parameter_neighborhood, time_split_sharpes
from beidou_alpha.validation.verdict import decide
from beidou_alpha.validation.walk_forward import param_key, walk_forward_evaluate, walk_forward_folds
from beidou_cli import research
from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import cost_model, load_panel, load_registry, portfolio_params, read_universe
from beidou_shared.config import load_yaml

DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "tsmom": {"vol_window": [100, 200, 400], "entry_threshold": [0.15, 0.20, 0.30], "return_scale": [0.10, 0.20, 0.30]},
}


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
        ]
    ):
        function = option(function)
    return function


def _resolve_symbols(root: str, symbols: str, interval: str) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",") if s.strip()]
    universe = read_universe(root)
    return universe or KlineStore(root).symbols(interval)


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


def _model(entry: StrategyEntry, profile: dict[str, Any], interval: str) -> AlphaModel:
    return AlphaModel(entries=(entry,), portfolio=portfolio_params(profile), interval=interval)


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
    return datetime.now(UTC).strftime("%Y%m%dT%H%MZ")


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
) -> None:
    """Backtest one strategy through the full portfolio pipeline and write a research report."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval)
    panel = _load(root, chosen, interval, start, end, funding)
    model = _model(entry, profile_payload, interval)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    weights, _combined, _per = model.evaluate(panel)
    result = run_backtest(panel, weights, cost, execution=execution)  # type: ignore[arg-type]
    summary = result.summary()
    bench = benchmark_returns(panel, execution, panel.symbols).reindex(result.weights.index)  # type: ignore[arg-type]
    report: dict[str, Any] = {
        "kind": "backtest",
        "strategy": strategy,
        "params": entry.params,
        "portfolio": model.portfolio.__dict__,
        "interval": interval,
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
    grid: str,
    folds: int,
    min_train: int,
    purge: int,
    cpcv_groups: int,
) -> None:
    """Walk-forward + CPCV + DSR/PBO + stability for one strategy; writes the evidence report for the registry."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval)
    panel = _load(root, chosen, interval, start, end, funding)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    combos = _grid(strategy, grid, entry.params)
    bpy = panel.bars_per_year
    nets: dict[str, pd.Series] = {}
    params_by_key: dict[str, dict[str, Any]] = {}
    results: dict[str, BacktestResult] = {}
    click.echo(f"evaluating {len(combos)} parameter sets on {len(panel.symbols)} symbols x {len(panel.index)} bars")
    for combo in combos:
        key = param_key(combo)
        model = _model(StrategyEntry(id=strategy, params=combo), profile_payload, interval)
        weights, _c, _p = model.evaluate(panel)
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
    full_sharpes: dict[str, float] = {key: (sharpe(series, bpy) or -np.inf) for key, series in nets.items()}
    best_key = max(full_sharpes, key=lambda k: full_sharpes[k])
    matrix = np.column_stack([nets[key].to_numpy(dtype=float) for key in nets])
    mt = multiple_testing_report(nets[best_key].to_numpy(dtype=float), matrix, bars_per_year=bpy)

    def evaluate_params(candidate: Mapping[str, Any]) -> float | None:
        model = _model(StrategyEntry(id=strategy, params=dict(candidate)), profile_payload, interval)
        weights, _c, _p = model.evaluate(panel)
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
        "symbols": panel.symbols,
        "range": {"start": str(common_index[0]), "end": str(common_index[-1]), "bars": n_bars},
        "costs": cost.__dict__,
        "execution": execution,
        "grid_size": len(combos),
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


__all__ = ["research_backtest", "research_list", "research_validate"]
