"""``beidou research ...`` commands: backtest, validate, list."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
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
from beidou_alpha.signals.base import scores_to_targets
from beidou_alpha.validation.cpcv import cpcv_evaluate, cpcv_splits
from beidou_alpha.validation.labels import forward_returns
from beidou_alpha.validation.ledger import TrialRecord, dsr_inputs, parse_ledger
from beidou_alpha.validation.metrics import (
    compound,
    information_coefficient,
    newey_west_tstat,
    sharpe,
    time_series_ic,
    yearly_breakdown,
)
from beidou_alpha.validation.multiple_testing import multiple_testing_report
from beidou_alpha.validation.stability import cost_stress, parameter_neighborhood, time_split_sharpes
from beidou_alpha.validation.verdict import decide
from beidou_alpha.validation.walk_forward import param_key, walk_forward_evaluate, walk_forward_folds
from beidou_cli import research
from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import cost_model, load_panel, load_registry, portfolio_params, read_universe
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
    min_history: int | None,
) -> None:
    """Backtest one strategy through the full portfolio pipeline and write a research report."""
    profile_payload = load_yaml(profile)
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval)
    panel = _load(root, chosen, interval, start, end, funding)
    model = _model(entry, profile_payload, interval, min_history)
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
        model = _model(StrategyEntry(id=strategy, params=combo), profile_payload, interval, min_history)
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
    pooled = dsr_inputs(prior_records, period_sharpes, bpy, manual_prior_trials=prior_trials)
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


__all__ = ["research_backtest", "research_list", "research_validate"]


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
    horizons: str,
) -> None:
    """Signal-level diagnostics before any portfolio construction: IC by horizon, signal-only backtest, flips."""
    del out  # diagnostics write nothing
    entry = _entry(strategy, registry_path, params)
    chosen = _resolve_symbols(root, symbols, interval)
    panel = _load(root, chosen, interval, start, end, funding)
    if min_history is None:
        min_history = int((load_yaml(profile).get("portfolio", {}) or {}).get("min_history_bars", 720))
    eligible = panel.close.notna().cumsum() >= min_history
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
) -> None:
    """Correlation of strategy net-return streams and the marginal Sharpe of each strategy in an equal-weight mix."""
    ids = [s.strip() for s in strategies.split(",") if s.strip()]
    profile_payload = load_yaml(profile)
    chosen = _resolve_symbols(root, symbols, interval)
    panel = _load(root, chosen, interval, start, end, funding)
    cost = cost_model(load_yaml(costs_path), use_funding=funding)
    nets: dict[str, pd.Series] = {}
    for strategy in ids:
        entry = _entry(strategy, registry_path, "")
        weights, _c, _p = _model(entry, profile_payload, interval).evaluate(panel)
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
