"""lsr_timing 预登记第 6 项里报告不直接给的两条判据，以及同一项列为「只报告」的读数。

与预登记同一个 commit 入库（`docs/RESEARCH_LOG.md`「2026-09-28 · 预登记：全市场多空比择时 lsr_timing」），
所以读法在数字出来之前就固定了。零 ledger：只读报告、数据与 registry，不写 `trials.jsonl`。

**开读之前先复现。** 按报告记下的 symbols、区间、universe、参数、成本、护栏与退出层重算最优格；全样本
Sharpe 与报告的 `full_sample.annualized_sharpe` 相差超过 1e-9 就停：数据或代码已经不是跑 validate 的那一份。

**十一个对照，与书走同一台机器。** 同一个 `AlphaModel`（同一个 population、同一条入场线与 hold、离开成员
即平仓、同一套定仓与 band），同样的成本、资金费、护栏与退出层，只把分数换掉：

- 大盘：population 里每个币恒定 +1，即恒定持有同一个篮子（D-024 `constant_long` 的写法）。
- 简单趋势跟随，十条：净敞口跟着篮子过去一段的涨跌走，两种常见写法各取五个窗口（24、72、168、336、720 根，
  一天到一个月）。「涨跌」：population 里每个币都取篮子过去 w 根收益之和的符号。「均线」：篮子的累计收益
  （逐 bar 相加）在自己过去 w 根均值之上取 +1、之下取 −1。篮子是 #199 的写法：构造自己的 `asset_vol` 取倒数，
  在 population 上归一；每根 bar 的篮子收益是上一根的比例乘这一根的 close / open − 1。

为什么是十条而不是第一稿的一条（最优格自己窗口的「涨跌」）：人群若只是价格的镜像，−M 就是一条均线趋势，而
它不必落在书自己的窗口上。合成的「只有趋势」世界里正是这样，一条对照放过了书，十条挡住了；择时世界两种写法
都过（`lsr_timing_criteria_synthetic.py`，读数在预登记里）。

换分数的那条路径先喂书自己的分数，出来的权重要与 `_book_weights` 逐位相同，否则停：对照走的必须是同一台
机器，差别只能在分数上。

**A 段与 B 段。** 切点是 M 第一次由两个以上的币平均出来的那根决策 bar（population 里有多空比读数的币 ≥ 2）。
收益按持仓算，t 的收益来自 t − 1 定下的仓位，所以 B 段从切点的下一根 bar 起；A 段从书的第一个决定起到切点。
书从 M 第一次有值的那根起就在做决定，第一个强读数之前的决定是空仓，那几根算进 A 段，收益为 0。

**两条判据：**

1. **B 段超出对照的部分。** 书在 B 段的逐 bar 净收益，对十一个对照在 B 段的逐 bar 毛收益（只有价格，不含
   资金费与手续费）回归，β 只在 B 段上拟合；截距（净收益减去 β 乘因子之后的序列）的 Newey-West t 用
   `newey_west_tstat` 的默认带宽，高于 2.0 才算过，差不到 1e-9 读作未过。因子取毛收益，是不把对照自己付的
   成本记到书的账上：只会让截距变小。
2. **相关。** 书与 registry 里 tsmom 的日净收益相关 < 0.50。按 `research correlate` 的协议：净收益流，不套
   护栏与退出层，各用各的成本，按 UTC 日复利；只取书第一次持仓（第一根仓位不为 0 的 bar）那天起的日子。
   恰好 0.50 算挡住。

**只报告：** A 段的同一套读数（β 在 A 段上拟合；长窗口的对照在 A 段开头还没有读数，那几根不进回归，bar 数照报）；
书与各对照在各段的 Sharpe、t 与年化收益；书在各段占全样本净收益的份额；M 由几个币平均出来；强读数的占比、
每年翻面几次、做多、做空与空仓各占多少 bar、最长多少天没有强读数；书的方向与最优格窗口那条「涨跌」对照方向的相关；|M| 的分位数；
B 段在十一个对照之外再加 tsmom 毛收益的截距；书的逐年净收益（逐 bar 相加）。

在 Mac 上要把书与十一个对照各过一遍模型与定价，比 validate 的一格慢十来倍。

用法（与 validate 同一个 worktree、同一份数据；环境里不能有 `BEIDOU_TRIALS_LEDGER` 与 `BEIDOU_FEATURE_STORE`）：

    PYTHONPATH=$PWD /Users/maguannan/beidou/.venv/bin/python scratchpad/lsr_timing_criteria.py \\
        --report reports/research/lsr_timing-validation-<stamp>.json \\
        --root /Users/maguannan/beidou/.beidou/data
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_alpha.backtest import BacktestResult, CostModel, ImpactModel, run_backtest
from beidou_alpha.features import within_reference
from beidou_alpha.model import AlphaModel
from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, asset_vol
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals import get_signal
from beidou_alpha.signals.lsr_timing import LsrTimingParams, held_side, market_deviation
from beidou_alpha.validation.metrics import newey_west_tstat, sharpe
from beidou_alpha.validation.pipeline import score_book
from beidou_cli.research_panel import _book_weights, _entry, _load, _membership, _model
from beidou_live.composition import cost_model
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "lsr_timing"
REPRODUCTION_TOLERANCE = 1e-9
ALPHA_T_LINE = 2.0
LINE_TOLERANCE = 1e-9
CORRELATION_LINE = 0.50
PASSING_VERDICTS = ("PASS", "WEAK_PASS")
MIN_BARS = 30  # fewer bars than this in a segment and a t is not read at all
TREND_WINDOWS = (24, 72, 168, 336, 720)  # a day to a month


def daily(net: pd.Series) -> pd.Series:
    """Compounded per UTC day."""
    return (1.0 + net).groupby(pd.DatetimeIndex(net.index).floor("D")).prod() - 1.0


def uniform(panel: Panel, side: pd.Series) -> pd.DataFrame:
    """``side`` on every name of the population, nothing outside it."""
    columns = panel.close.columns
    values = np.repeat(side.to_numpy(dtype=float)[:, None], len(columns), axis=1)
    return within_reference(pd.DataFrame(values, index=panel.index, columns=columns), panel.reference)


def basket_bar_return(panel: Panel, portfolio: PortfolioParams) -> pd.Series:
    """#199's basket, on the population: last bar's inverse-`asset_vol` proportions times this bar's close/open - 1."""
    sigma = asset_vol(panel.close, portfolio, panel.bars_per_year)
    inverse = within_reference((1.0 / sigma).where(panel.close.notna()), panel.reference)
    whole = inverse.div(inverse.sum(axis=1), axis=0)
    return (whole.shift(1) * (panel.close / panel.open - 1.0)).sum(axis=1, min_count=1)


def trailing_basket_return(panel: Panel, portfolio: PortfolioParams, window: int) -> pd.Series:
    return basket_bar_return(panel, portfolio).rolling(window, min_periods=window).sum()


def market_scores(panel: Panel) -> pd.DataFrame:
    return uniform(panel, pd.Series(1.0, index=panel.index))


def trend_return_scores(portfolio: PortfolioParams, window: int) -> Callable[[Panel], pd.DataFrame]:
    """「涨跌」: the sign of the basket's return over the last ``window`` bars."""

    def scores(panel: Panel) -> pd.DataFrame:
        return uniform(panel, np.sign(trailing_basket_return(panel, portfolio, window)))

    return scores


def trend_level_scores(portfolio: PortfolioParams, window: int) -> Callable[[Panel], pd.DataFrame]:
    """「均线」: the basket's summed return above (+1) or below (-1) its own trailing ``window``-bar mean."""

    def scores(panel: Panel) -> pd.DataFrame:
        level = basket_bar_return(panel, portfolio).fillna(0.0).cumsum()
        return uniform(panel, np.sign(level - level.rolling(window, min_periods=window).mean()))

    return scores


def controls(portfolio: PortfolioParams) -> dict[str, Callable[[Panel], pd.DataFrame]]:
    built: dict[str, Callable[[Panel], pd.DataFrame]] = {"market": market_scores}
    for window in TREND_WINDOWS:
        built[f"trend_return_{window}"] = trend_return_scores(portfolio, window)
        built[f"trend_level_{window}"] = trend_level_scores(portfolio, window)
    return built


def through(base: AlphaModel, scores_for: Callable[[Panel], pd.DataFrame]) -> AlphaModel:
    """``base`` with its one entry's scores replaced and nothing else about it changed."""

    class Replaced(type(base)):  # type: ignore[misc]
        def strategy_scores(self, panel: Panel, reference: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
            scored = panel if reference is None else panel.with_reference(reference)
            return {entry.id: scores_for(scored) for entry in self.entries}

    return Replaced(**{field.name: getattr(base, field.name) for field in dataclasses.fields(base)})


def t_of(series: pd.Series | np.ndarray) -> float | None:
    return newey_west_tstat(series)["t_stat"] if len(series) >= MIN_BARS else None


def reading(series: pd.Series, bars_per_year: float) -> dict[str, Any]:
    return {
        "bars": len(series),
        "sharpe": sharpe(series, bars_per_year) if len(series) >= MIN_BARS else None,
        "t_stat": t_of(series),
        "per_year": float(series.mean() * bars_per_year) if len(series) else None,
    }


def beyond(net: pd.Series, factors: Mapping[str, pd.Series], bars_per_year: float) -> dict[str, Any]:
    """#199's `beyond_direction`: the t of ``net - sum(beta_k * factor_k)``, whose mean is the intercept."""
    frame = pd.concat({"net": net, **factors}, axis=1).dropna()
    if len(frame) < MIN_BARS:
        return {"bars": len(frame), "t_stat": None, "per_year": None, "betas": None}
    design = np.column_stack([np.ones(len(frame)), *(frame[name].to_numpy() for name in factors)])
    coef, *_ = np.linalg.lstsq(design, frame["net"].to_numpy(), rcond=None)
    left = frame["net"].to_numpy() - design[:, 1:] @ coef[1:]
    return {
        "bars": len(frame),
        "t_stat": t_of(left),
        "per_year": float(coef[0]) * bars_per_year,
        "betas": {name: float(coef[i + 1]) for i, name in enumerate(factors)},
    }


def share(part: pd.Series, whole: pd.Series) -> float | None:
    total = float(whole.sum())
    return float(part.sum()) / total if total > 0 else None


def finite(value: float) -> float | None:
    """A correlation of a constant series is NaN; the file says ``null`` rather than a non-JSON ``NaN``."""
    return float(value) if math.isfinite(value) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--profile", default=str(ROOT / "config" / "live.demo.yaml"))
    parser.add_argument("--registry", default=str(ROOT / "config" / "alpha_registry.yaml"))
    parser.add_argument("--tsmom-costs", default=str(ROOT / "config" / "costs.yaml"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "research"))
    args = parser.parse_args()

    for name in ("BEIDOU_TRIALS_LEDGER", "BEIDOU_FEATURE_STORE"):
        if os.environ.get(name):
            raise SystemExit(f"{name} is set; unset it, validate ran without it")

    raw = Path(args.report).read_bytes()
    report = json.loads(raw)
    if report.get("strategy") != STRATEGY:
        raise SystemExit(f"{args.report} is a {report.get('strategy')!r} report, not {STRATEGY!r}")
    interval = str(report["interval"])
    end = pd.Timestamp(report["range"]["end"]) + pd.Timedelta(interval)
    panel = _load(args.root, list(report["symbols"]), interval, None, str(end), True, metrics=True)
    membership = _membership(args.root, report["universe_mode"], panel, int(report["min_tenure"]))
    profile = load_yaml(args.profile)
    cost = CostModel(**report["costs"])
    guards = None if report["book_guards"] is None else BookGuardParams(**report["book_guards"])
    exits = None if report["exits"] is None else ExitParams.from_mapping(report["exits"])
    impact = ImpactModel(**report["impact_model"])
    execution = str(report["execution"])
    bpy = panel.bars_per_year

    def price(weights: pd.DataFrame) -> BacktestResult:
        priced, _overlaid = score_book(panel, weights, cost, execution=execution, guards=guards, exits=exits, impact=impact)
        return priced

    # --- reproduce, then prove the controls' path is the book's path ------------------------------------
    cell = StrategyEntry(id=STRATEGY, params=dict(report["best_params"]))
    params = LsrTimingParams.from_mapping(cell.params)
    weights = _book_weights(cell, profile, interval, None, panel, membership)
    book = price(weights)
    reproduced = book.summary()["annualized_sharpe"]
    recorded = report["full_sample"]["annualized_sharpe"]
    if not math.isclose(reproduced, recorded, rel_tol=REPRODUCTION_TOLERANCE, abs_tol=REPRODUCTION_TOLERANCE):
        raise SystemExit(f"not the run the report describes: Sharpe {reproduced!r} here, {recorded!r} in the report")
    base = _model(cell, profile, interval)
    own, _c, _p = through(base, lambda scored: get_signal(STRATEGY).compute(scored, cell.params)).evaluate(
        panel, membership
    )
    if not own.equals(weights):
        raise SystemExit("the path the controls take does not give the book's own weights on the book's scores")
    priced = {
        name: price(through(base, scores).evaluate(panel, membership)[0])
        for name, scores in controls(base.portfolio).items()
    }

    # --- the segments -----------------------------------------------------------------------------------
    population = panel.with_reference(base.reference_for(panel, membership))
    deviation, names = market_deviation(population, params.window)
    raw_score = np.tanh(-deviation / params.scale)
    side = held_side(raw_score, params.entry_threshold)
    decided = weights.notna().any(axis=1).cummax()
    held = decided.shift(1, fill_value=False).astype(bool)  # bar t earns what t - 1 decided
    positioned = weights.fillna(0.0).abs().sum(axis=1).gt(0.0).shift(1, fill_value=False).astype(bool)
    if not positioned.any():
        raise SystemExit("the book never held a position: no reading was ever strong enough to pick a side")
    crossed = names[names >= 2]
    if crossed.empty:
        raise SystemExit("the long/short ratio never covers two names in the population; there is no B segment")
    split = crossed.index[0]
    segments = {"all": held, "A": held & (panel.index <= split), "B": held & (panel.index > split)}
    net = book.portfolio_net
    streams = {"book": net, **{name: result.portfolio_net for name, result in priced.items()}}
    factors = {name: result.portfolio_gross for name, result in priced.items()}
    trend_side = np.sign(trailing_basket_return(population, base.portfolio, params.window))

    def on(series: pd.Series, mask: pd.Series) -> pd.Series:
        return series[mask.reindex(series.index).fillna(False).astype(bool)]

    def decision(series: pd.Series, mask: pd.Series) -> pd.Series:
        """What was known at the decision behind each bar of the segment."""
        return on(series.shift(1), mask)

    def actionability(mask: pd.Series) -> dict[str, Any] | None:
        """How the side behaved behind the segment's bars: how often it could move, and how often it did."""
        if not mask.any():
            return None
        held_side_then = decision(side, mask)
        known = held_side_then.notna() & held_side_then.shift(1).notna()
        flips = int((np.sign(held_side_then) != np.sign(held_side_then.shift(1)))[known].sum())
        averaged = decision(names, mask)
        strong = decision(raw_score, mask).abs() >= params.entry_threshold
        marks = pd.DatetimeIndex([strong.index[0], *strong.index[strong], strong.index[-1]])
        return {
            "names_averaged": {"min": int(averaged.min()), "median": float(averaged.median()), "max": int(averaged.max())},
            "strong_reading_share": float(strong.mean()),
            "longest_quiet_days": float(marks.to_series().diff().max() / pd.Timedelta(days=1)),
            "flips_per_year": flips / (int(mask.sum()) / bpy),
            "long_share": float((held_side_then > 0).mean()),
            "short_share": float((held_side_then < 0).mean()),
            "flat_share": float(held_side_then.isna().mean()),
            "side_vs_trend_side_corr": finite(np.sign(held_side_then).corr(decision(trend_side, mask))),
            "abs_deviation_quantiles": {str(q): float(decision(deviation, mask).abs().quantile(q)) for q in (0.5, 0.9, 0.99)},
        }

    per_segment: dict[str, Any] = {}
    for label, mask in segments.items():
        per_segment[label] = {
            "first_bar": str(on(net, mask).index[0]) if mask.any() else None,
            "streams": {name: reading(on(series, mask), bpy) for name, series in streams.items()},
            "book_share_of_net": share(on(net, mask), on(net, segments["all"])),
            "beyond_controls": beyond(on(net, mask), {k: on(v, mask) for k, v in factors.items()}, bpy),
            "side": actionability(mask),
        }

    # --- tsmom: the correlation line, and a third factor that is reported only --------------------------
    tsmom = _entry("tsmom", args.registry, "", "")
    tsmom_weights, _c, _p = _model(tsmom, profile, interval).evaluate(panel, membership)
    tsmom_cost = cost_model(load_yaml(args.tsmom_costs), use_funding=True)
    bare = run_backtest(panel, weights, cost, execution=execution).portfolio_net  # type: ignore[arg-type]
    tsmom_bare = run_backtest(panel, tsmom_weights, tsmom_cost, execution=execution)  # type: ignore[arg-type]
    first_day = pd.DatetimeIndex(on(net, positioned).index).floor("D")[0]
    days = pd.DataFrame({"lsr_timing": daily(bare), "tsmom": daily(tsmom_bare.portfolio_net)}).dropna()
    days = days[days.index >= first_day]
    correlation = finite(days["lsr_timing"].corr(days["tsmom"]))
    with_tsmom = beyond(
        on(net, segments["B"]),
        {**{k: on(v, segments["B"]) for k, v in factors.items()}, "tsmom": on(tsmom_bare.portfolio_gross, segments["B"])},
        bpy,
    )

    alpha_t = per_segment["B"]["beyond_controls"]["t_stat"]
    oos = report["oos_selection"]
    oos_gate = max(float(oos["threshold_annual"]), 1.0)
    criteria = {
        "verdict": report.get("verdict"),
        "verdict_passes": report.get("verdict") in PASSING_VERDICTS,
        "oos_sharpe": oos["oos_sharpe_annual"],
        "oos_gate": oos_gate,
        "oos_passes": float(oos["oos_sharpe_annual"]) - oos_gate > LINE_TOLERANCE,
        "alpha_beyond_controls_b_t": alpha_t,
        "alpha_passes": alpha_t is not None and alpha_t - ALPHA_T_LINE > LINE_TOLERANCE,
        "correlation_with_tsmom": correlation,
        "correlation_passes": correlation is not None and correlation < CORRELATION_LINE,
    }
    criteria["all_pass"] = all(criteria[key] for key in ("verdict_passes", "oos_passes", "alpha_passes", "correlation_passes"))
    yearly = on(net, held).groupby(pd.DatetimeIndex(on(net, held).index).year).sum()
    result = {
        "kind": "lsr_timing_criteria",
        "report": {"path": args.report, "sha256": hashlib.sha256(raw).hexdigest()},
        "best_params": report["best_params"],
        "reproduced_sharpe": reproduced,
        "split_decision_bar": str(split),
        "criteria": criteria,
        "correlation_days": len(days),
        "segments": per_segment,
        "reported_only": {
            "b_beyond_market_trend_and_tsmom": with_tsmom,
            "book_net_by_year": {str(year): float(value) for year, value in yearly.items()},
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    out = Path(args.out) / f"lsr_timing-criteria-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    print(f"written: {out} sha256={hashlib.sha256(out.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
