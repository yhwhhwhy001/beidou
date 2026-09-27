"""carry_hedged 预登记第 6 项里报告不直接给的两条判据，以及同一项列为「只报告」的读数。

与预登记同一个 commit 入库（`docs/RESEARCH_LOG.md`「2026-09-27 · 预登记：资金费对冲书 carry_hedged」），
所以读法在数字出来之前就固定了。零 ledger：只读报告、数据与 registry，不写 `trials.jsonl`。

两条判据：

- **相关。** 本书与 registry 里 tsmom 的日净收益相关 < 0.50。按 `research correlate` 的协议：净收益流，
  不套路径依赖的层（护栏、退出），各用各的成本。按 UTC 日复利，只取本书第一根定价 bar 之后的日子。
  恰好 0.50 算挡住。
- **压力窗口。** 七个窗口里每一个的单日最差 ≥ −5.00%。读的是 validate 给最优格定价的那条序列（护栏
  照放，没有退出），按 UTC 日复利。百分数保留两位小数后比较，−5.00% 算过。窗口左闭右开，按 UTC 日。

开读之前先复现：按报告记下的 symbols、区间、参数、成本、护栏重算最优格。全样本 Sharpe 与报告的
`full_sample.annualized_sharpe` 相差超过 1e-9 就停，因为那说明数据或代码已经不是跑 validate 的那一份。

用法（与 validate 同一个 worktree、同一份数据）：

    PYTHONPATH=$PWD .venv/bin/python scratchpad/carry_hedged_criteria.py \\
        --report reports/research/carry_hedged-validation-<stamp>.json \\
        --root /Users/maguannan/beidou/.beidou/data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_alpha.backtest import CostModel, ImpactModel, run_backtest
from beidou_alpha.hedged import spread_panel
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.validation.pipeline import score_book
from beidou_cli.research_panel import _book_weights, _entry, _load, _membership, _model
from beidou_live.composition import cost_model
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "carry_hedged"
CORRELATION_LINE = 0.50
WORST_DAY_LINE_PCT = -5.00
REPRODUCTION_TOLERANCE = 1e-9

#: G4 的六个（`scratchpad/g4_stress_windows_and_var_at_k060.py` 去掉早于归档的 2020-03），加 2026-09-25
#: 审计指出漏掉的 2025-10-10。左闭右开，UTC。
WINDOWS = (
    ("2021-05 leverage flush", "2021-05-10", "2021-06-01"),
    ("2022-05 LUNA/UST", "2022-05-05", "2022-05-25"),
    ("2022-06 3AC/Celsius", "2022-06-10", "2022-07-05"),
    ("2022-11 FTX", "2022-11-05", "2022-11-25"),
    ("2024-08 yen carry unwind", "2024-08-01", "2024-08-15"),
    ("2025-02 (09-08's worst month)", "2025-02-01", "2025-03-01"),
    ("2025-10-10 liquidation cascade", "2025-10-06", "2025-10-20"),
)


def daily(net: pd.Series) -> pd.Series:
    """Compounded per UTC day."""
    return (1.0 + net).groupby(pd.DatetimeIndex(net.index).floor("D")).prod() - 1.0


def stress_rows(days: pd.Series) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in WINDOWS:
        lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
        window = days[(days.index >= lo) & (days.index < hi)]
        if window.empty:
            # 窗口不在序列里就是读不出，不是过。
            rows.append({"window": name, "start": start, "end": end, "days": 0, "worst_day_pct": None, "passes": False})
            continue
        worst = float(window.min())
        rows.append(
            {
                "window": name,
                "start": start,
                "end": end,
                "days": len(window),
                "worst_day": str(window.idxmin().date()),
                "worst_day_pct": worst * 100.0,
                "passes": round(worst * 100.0, 2) >= WORST_DAY_LINE_PCT,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", default=str(ROOT / "config" / "live.demo.yaml"))
    parser.add_argument("--registry", default=str(ROOT / "config" / "alpha_registry.yaml"))
    parser.add_argument("--tsmom-costs", default=str(ROOT / "config" / "costs.yaml"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "research"))
    args = parser.parse_args()

    raw = Path(args.report).read_bytes()
    report = json.loads(raw)
    if report.get("strategy") != STRATEGY:
        raise SystemExit(f"{args.report} is a {report.get('strategy')!r} report, not {STRATEGY!r}")
    interval = str(report["interval"])
    end = pd.Timestamp(report["range"]["end"]) + pd.Timedelta(interval)
    panel = _load(args.root, list(report["symbols"]), interval, None, str(end), True, spot=True)
    membership = _membership(args.root, report["universe_mode"], panel, int(report["min_tenure"]))
    profile = load_yaml(args.profile)
    min_history = int(report["portfolio"]["min_history_bars"])
    spread = spread_panel(panel)
    cost = CostModel(**report["costs"])
    guards = None if report["book_guards"] is None else BookGuardParams(**report["book_guards"])
    impact = ImpactModel(**report["impact_model"])
    execution = str(report["execution"])

    cell = StrategyEntry(id=STRATEGY, params=dict(report["best_params"]))
    weights = _book_weights(cell, profile, interval, min_history, panel, membership)
    priced, _overlaid = score_book(spread, weights, cost, execution=execution, guards=guards, exits=None, impact=impact)
    reproduced = priced.summary()["annualized_sharpe"]
    recorded = report["full_sample"]["annualized_sharpe"]
    if not math.isclose(reproduced, recorded, rel_tol=REPRODUCTION_TOLERANCE, abs_tol=REPRODUCTION_TOLERANCE):
        raise SystemExit(f"not the run the report describes: Sharpe {reproduced!r} here, {recorded!r} in the report")

    # 相关：`research correlate` 的协议，两本书都不套护栏与退出。
    tsmom = _entry("tsmom", args.registry, "", "")
    tsmom_weights, _c, _p = _model(tsmom, profile, interval).evaluate(panel, membership)
    tsmom_cost = cost_model(load_yaml(args.tsmom_costs), use_funding=True)
    bare = run_backtest(spread, weights, cost, execution=execution).portfolio_net  # type: ignore[arg-type]
    tsmom_net = run_backtest(panel, tsmom_weights, tsmom_cost, execution=execution).portfolio_net  # type: ignore[arg-type]
    frame = pd.DataFrame({"carry_hedged": daily(bare), "tsmom": daily(tsmom_net)}).dropna()
    frame = frame[frame.index >= pd.DatetimeIndex(bare.index).floor("D")[0]]
    correlation = float(frame["carry_hedged"].corr(frame["tsmom"]))

    stress = stress_rows(daily(priced.portfolio_net))

    # 只报告：每天持有几个币，以及净收益拆成价差、资金费、手续费三块（年化均值）。
    held = (priced.weights.fillna(0.0) > 0).sum(axis=1)
    held_daily = held.groupby(pd.DatetimeIndex(held.index).floor("D")).last()
    funding = spread.funding[priced.weights.columns].reindex(priced.weights.index).fillna(0.0)  # type: ignore[index]
    funding_cost = (priced.weights * funding).sum(axis=1)
    bpy = panel.bars_per_year
    components = {
        "spread_price": float(priced.portfolio_gross.mean() * bpy),
        "funding_income": float(-funding_cost.mean() * bpy),
        "trading_costs": float(-(priced.costs.sum(axis=1) - funding_cost).mean() * bpy),
        "net": float(priced.portfolio_net.mean() * bpy),
    }
    # 只报告：持有时有一条腿那根 bar 没成交（现货缺 bar；永续缺 bar 或零成交量）。`hedged.py` 按最后成交价
    # 记它、最多一天，所以这些 bar 上书带着另一条腿的裸敞口；这里给出有多少、落在哪些币、毛收益合计多少。
    spot_close = panel.spot_field("close")
    assert spot_close is not None
    idx, cols = priced.weights.index, priced.weights.columns
    stopped = ~(panel.close.notna() & (panel.volume > 0))[cols].reindex(idx) | spot_close[cols].reindex(idx).isna()
    naked = (priced.weights.fillna(0.0) != 0.0) & stopped
    naked_leg = {
        "bars": int(naked.to_numpy().sum()),
        "symbols": sorted(str(symbol) for symbol in cols[naked.any(axis=0).to_numpy()]),
        "gross_return_sum": float(priced.gross.where(naked, 0.0).sum().sum()),
    }

    verdict_reads = {
        "verdict": report.get("verdict"),
        "correlation_with_tsmom": correlation,
        "correlation_passes": correlation < CORRELATION_LINE,
        "stress_passes": all(row["passes"] for row in stress),
    }
    result = {
        "kind": "carry_hedged_criteria",
        "report": {"path": args.report, "sha256": hashlib.sha256(raw).hexdigest()},
        "best_params": report["best_params"],
        "reproduced_sharpe": reproduced,
        "criteria": verdict_reads,
        "correlation_days": len(frame),
        "stress_windows": stress,
        "reported_only": {
            "held_symbols_per_day": {
                "median": float(held_daily.median()),
                "min": int(held_daily.min()),
                "max": int(held_daily.max()),
                "share_of_days_holding_nothing": float((held_daily == 0).mean()),
            },
            "annualized_components": components,
            "naked_leg_bars": naked_leg,
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    out = Path(args.out) / f"carry_hedged-criteria-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    print(f"written: {out} sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
