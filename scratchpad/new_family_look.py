"""零 ledger 先看：大户持仓多空比、现货主动买卖这两个信息族，值不值得花 4 格 ledger 去预登记。

起因：2026-09-27 carry_hedged 判负（RESEARCH_LOG「carry_hedged 裁决：REFUTED」）之后，库里现成、不用新入库
也不用现货腿的候选里，先验最高的是这两族（`/mnt/project-files/analysis/alpha-discovery-2026-09-27/report.md`
§B）。它们的列都在库里、从没被任何信号读过：`sum_toptrader_long_short_ratio`（metrics），现货 K 线的
`taker_buy_quote`（`SPOT_PANEL_COLUMNS` 只对齐价格与 `quote_volume`，这一列要在这里自己对齐）。

**零 ledger。** 与 `research diagnose` 同一口径（等名义、`scores_to_targets` 阈值 0.2、`hold=True`、
open_to_close、`config/costs.yaml`），不写 `trials.jsonl`、不写 `reports/`。只有 `--json` 给了路径才写一个
文件，而那个路径应该在 `reports/` 之外。

**看之前就定死的东西。** 下面八个写法、预期方向和判定规则是在任何读数出来之前写下的；读数只能关掉一个族，
不能在族内挑格。一个族判 GO，预登记时 4 格就是它自己的 2 个写法 × 2 个窗口，一格不换。

族 A（大户持仓，metrics）：
  - A1(w)：`sum_toptrader_long_short_ratio` / 它自己过去 w 根的均值 − 1。与 `lsr` 叶同形：水平量是交易所
    整体的偏置，能带信息的是偏离。
  - A2(w)：s = log(大户持仓多空比) − log(全体账户多空比 `count_long_short_ratio`)，取 s − 它过去 w 根的均值。
    「大户比散户更偏多」相对它自己的常态。
  - w ∈ {72, 168}。预期方向：正（跟大户）。
族 B（现货主动买卖，现货 K 线）：
  - B1(w)：过去 w 根现货 taker 买入额 / 现货成交额 − 0.5。
  - B2(w)：B1(w) 减去同窗口的永续 taker 买入占比 − 0.5。flow 的 docstring 自己写着它看不见现货需求，这一条
    量的就是那部分。
  - w ∈ {24, 72}。预期方向：正（现货买盘领先永续价格）。

原始值先限在可交易范围内（`min_history_bars` 与 pit 成员），再做横截面排名，映到 [−1, 1]。

**判定（每族一次）。** GO 要同一个写法同时满足三条：
  1. 在 24 / 72 / 168 根里某一个前瞻期上，横截面 IC 为正且 Newey-West t ≥ 2.6。每族 4 个写法 × 3 个前瞻期
     = 12 次看，单侧 5% 的 Bonferroni 约为 t 2.64，取 2.6；
  2. 等名义零成本 Sharpe > 0；
  3. 与 tsmom 等名义书的日收益相关 < 0.5，只从新书有数据的那天算起（之前那段新书空仓、tsmom 在交易，会把
     相关往 0 拉，恰好是让一个 tsmom 的翻版过线的方向）。
  否则 NO-GO：这个族在零 ledger 关掉，读数记进 RESEARCH_LOG。数据不够（有值的币中位数 < 50，或有值的日子
  不到 730 天）判 UNREADABLE，不是 NO-GO。

这是一次看，不是免费的：GO 的族进预登记时按 N=4 计，NO-GO 的族记一行，以后的人才知道它被看过。

用法（在 `~/beidou` 之外的 worktree 里，读主 checkout 的数据；环境里不能有 `BEIDOU_TRIALS_LEDGER` 与
`BEIDOU_FEATURE_STORE`）：

    PYTHONPATH=$PWD /Users/maguannan/beidou/.venv/bin/python scratchpad/new_family_look.py \\
        --root /Users/maguannan/beidou/.beidou/data --to 2026-09-27
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_alpha.backtest import CostModel, run_backtest
from beidou_alpha.features import cross_sectional_rank
from beidou_alpha.panel import Panel
from beidou_alpha.signals.base import scores_to_targets
from beidou_alpha.validation.labels import forward_returns
from beidou_alpha.validation.metrics import (
    information_coefficient,
    newey_west_tstat,
    sign_bucketed_ic,
    time_series_ic,
)

ROOT = Path(__file__).resolve().parents[1]
HORIZONS = (1, 4, 24, 72, 168, 336, 720)
GO_HORIZONS = (24, 72, 168)
GO_T = 2.6
TSMOM_CORRELATION_LINE = 0.5
ENTRY_THRESHOLD = 0.2
MIN_MEDIAN_SYMBOLS = 50
MIN_DAYS = 730
TOP = "sum_toptrader_long_short_ratio"
CROWD = "count_long_short_ratio"
FAMILIES: dict[str, tuple[str, ...]] = {
    "A": ("A1_w72", "A1_w168", "A2_w72", "A2_w168"),
    "B": ("B1_w24", "B1_w72", "B2_w24", "B2_w72"),
}


# --- the eight formulations, fixed before any reading ----------------------------------------------


def _positive(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.where(frame > 0)


def _log(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.log(frame.to_numpy(dtype=float)), index=frame.index, columns=frame.columns)


def _taker_share(buy: pd.DataFrame, total: pd.DataFrame, window: int) -> pd.DataFrame:
    buys = buy.rolling(window, min_periods=window).sum()
    volume = total.rolling(window, min_periods=window).sum()
    return buys / volume.where(volume > 0) - 0.5


def formulations(panel: Panel, spot_buy: pd.DataFrame, spot_quote: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Raw values, before eligibility and ranking.  NaN wherever the input is absent - never zero."""
    top = panel.metric(TOP)
    crowd = panel.metric(CROWD)
    if top is None or crowd is None:
        raise SystemExit(f"the panel carries no {TOP} / {CROWD}; was it loaded with metrics=True?")
    if panel.taker_buy_quote is None or panel.quote_volume is None:
        raise SystemExit("the perpetual panel carries no taker_buy_quote / quote_volume")
    top = _positive(top)
    spread = _log(top) - _log(_positive(crowd))
    out: dict[str, pd.DataFrame] = {}
    for window in (72, 168):
        mean = top.rolling(window, min_periods=window).mean()
        out[f"A1_w{window}"] = top / mean - 1.0
        out[f"A2_w{window}"] = spread - spread.rolling(window, min_periods=window).mean()
    for window in (24, 72):
        spot = _taker_share(spot_buy, spot_quote, window)
        perp = _taker_share(panel.taker_buy_quote, panel.quote_volume, window)
        out[f"B1_w{window}"] = spot
        out[f"B2_w{window}"] = spot - perp
    return {name: frame.reindex(index=panel.index, columns=panel.symbols) for name, frame in out.items()}


def spot_taker_columns(root: str, panel: Panel) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Spot `taker_buy_quote` and `quote_volume` on the perp bar grid, keyed by the PERPETUAL symbol.

    Same contract as `beidou_data.spot.align_spot_to_perp_bars`: the spot bar with the same open time, else
    NaN, no fill.  No multiplier: both columns are quote-currency amounts, and only their ratio is used.
    """
    from beidou_data.spot import read_spot_map
    from beidou_data.store import SPOT_KLINE_KIND, KlineStore

    store = KlineStore(root, kind=SPOT_KLINE_KIND)
    mappings = read_spot_map(store.root)
    buy: dict[str, pd.Series] = {}
    quote: dict[str, pd.Series] = {}
    missing_column = 0
    for symbol in panel.symbols:
        mapping = mappings.get(symbol)
        if mapping is None or mapping.spot is None:
            continue
        try:
            frame = store.load(mapping.spot, panel.interval)
        except FileNotFoundError:
            continue
        if frame.empty:
            continue
        if not {"open_time", "taker_buy_quote", "quote_volume"} <= set(frame.columns):
            missing_column += 1
            continue
        stamps = pd.DatetimeIndex(pd.to_datetime(frame["open_time"].astype("int64").to_numpy(), unit="ms", utc=True))
        indexed = frame[["taker_buy_quote", "quote_volume"]].set_axis(stamps, axis=0).astype(float)
        indexed = indexed[~indexed.index.duplicated(keep="last")].sort_index().reindex(panel.index)
        buy[symbol] = indexed["taker_buy_quote"]
        quote[symbol] = indexed["quote_volume"]
    if missing_column:
        raise SystemExit(f"{missing_column} spot files carry no taker_buy_quote; the store is not what this assumes")
    if not buy:
        raise SystemExit("no spot klines found for any panel symbol; is the spot store under --root?")
    wide = {
        name: pd.DataFrame(series, index=panel.index).reindex(columns=panel.symbols)
        for name, series in (("buy", buy), ("quote", quote))
    }
    return wide["buy"], wide["quote"]


# --- readings -------------------------------------------------------------------------------------


def to_scores(raw: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    return cross_sectional_rank(raw.where(eligible))


def data_width(scores: pd.DataFrame) -> dict[str, Any]:
    per_bar = scores.notna().sum(axis=1)
    live = per_bar[per_bar > 0]
    days = int(pd.DatetimeIndex(live.index).normalize().nunique()) if len(live) else 0
    return {
        "symbols_with_scores": int(scores.notna().any().sum()),
        "median_symbols_per_bar": float(live.median()) if len(live) else 0.0,
        "days_with_scores": days,
        "first_bar": None if live.empty else str(live.index[0]),
        "coverage": float(scores.notna().mean().mean()),
    }


def ic_rows(scores: pd.DataFrame, close: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for horizon in HORIZONS:
        fwd = forward_returns(close, horizon)
        ts = [time_series_ic(scores[s], fwd[s]) for s in scores.columns]
        ts_values = [v for v in ts if v is not None and math.isfinite(v)]
        xs = information_coefficient(scores, fwd)
        nw = newey_west_tstat(xs, max_lags=horizon) if len(xs) > 10 else {"t_stat": None, "p_value": None}
        bucket = sign_bucketed_ic(scores, fwd, horizon, threshold=ENTRY_THRESHOLD)
        rows.append(
            {
                "horizon": horizon,
                "ts_ic": float(np.mean(ts_values)) if ts_values else None,
                "xs_ic": float(xs.mean()) if len(xs) else None,
                "nw_t": nw["t_stat"],
                "nw_p": nw["p_value"],
                "nonoverlap_ic": bucket["overall_ic"],
                "long_n": bucket["long"]["n"],
                "long_mean_fwd": bucket["long"]["mean_forward_return"],
                "short_n": bucket["short"]["n"],
                "short_mean_fwd": bucket["short"]["mean_forward_return"],
            }
        )
    return rows


def equal_notional(
    panel: Panel, scores: pd.DataFrame, cost: CostModel, threshold: float = ENTRY_THRESHOLD
) -> tuple[dict[str, Any], pd.Series]:
    """`research diagnose`'s signal-only book: targets / number of symbols."""
    targets = scores_to_targets(scores, threshold, hold=True)
    equal = targets / max(1, len(panel.symbols))
    result = run_backtest(panel, equal, cost, execution="open_to_close")
    return result.summary(), result.portfolio_net


def daily(net: pd.Series) -> pd.Series:
    return (1.0 + net.fillna(0.0)).groupby(pd.DatetimeIndex(net.index).normalize()).prod() - 1.0


def daily_correlation(a: pd.Series, b: pd.Series, start: pd.Timestamp | None) -> float | None:
    """Daily compounded returns, only from the first day `a` could hold anything.

    Before its data starts the new book is flat while tsmom trades, and those days would pull the
    correlation towards zero - the direction that lets a copy of tsmom pass the 0.5 line.
    """
    joined = pd.concat([daily(a), daily(b)], axis=1, join="inner").dropna()
    if start is not None:
        joined = joined[joined.index >= start.normalize()]
    joined = joined[(joined.iloc[:, 0] != 0.0) | (joined.iloc[:, 1] != 0.0)]
    if len(joined) < 30:
        return None
    return float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))


def _first_scored_bar(scores: pd.DataFrame) -> pd.Timestamp | None:
    any_score = scores.notna().any(axis=1)
    return pd.Timestamp(any_score.idxmax()) if bool(any_score.any()) else None


def xs_overlap(a: pd.DataFrame, b: pd.DataFrame) -> float | None:
    """Mean per-bar cross-sectional Spearman between two score frames (how much of `a` is `b` again)."""
    values = information_coefficient(a, b)
    return float(values.mean()) if len(values) else None


def _sharpe(summary: Mapping[str, Any]) -> float | None:
    value = summary.get("annualized_sharpe", summary.get("sharpe"))
    return None if value is None else float(value)


def decide(family: str, readings: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    widths = [readings[name]["width"] for name in FAMILIES[family]]
    if max(w["median_symbols_per_bar"] for w in widths) < MIN_MEDIAN_SYMBOLS or max(
        w["days_with_scores"] for w in widths
    ) < MIN_DAYS:
        return {"family": family, "verdict": "UNREADABLE", "why": "data too narrow or too short", "passing": []}
    passing = []
    for name in FAMILIES[family]:
        reading = readings[name]
        hits = [
            row["horizon"]
            for row in reading["ic"]
            if row["horizon"] in GO_HORIZONS
            and row["xs_ic"] is not None
            and row["xs_ic"] > 0
            and row["nw_t"] is not None
            and row["nw_t"] >= GO_T
        ]
        sharpe = _sharpe(reading["zero_cost"])
        correlation = reading["tsmom_daily_correlation"]
        if hits and sharpe is not None and sharpe > 0 and correlation is not None and correlation < TSMOM_CORRELATION_LINE:
            passing.append({"formulation": name, "horizons": hits, "zero_cost_sharpe": sharpe, "tsmom_corr": correlation})
    return {"family": family, "verdict": "GO" if passing else "NO-GO", "passing": passing}


# --- main ------------------------------------------------------------------------------------------


def _fmt(value: Any, spec: str = "+.4f") -> str:
    return "n/a" if value is None or (isinstance(value, float) and not math.isfinite(value)) else format(value, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", required=True, help="the data root, e.g. <main checkout>/.beidou/data")
    parser.add_argument("--to", required=True, help="YYYY-MM-DD exclusive; pinned so the reading reproduces")
    parser.add_argument("--profile", default=str(ROOT / "config" / "live.demo.yaml"))
    parser.add_argument("--registry", default=str(ROOT / "config" / "alpha_registry.yaml"))
    parser.add_argument("--costs", default=str(ROOT / "config" / "costs.yaml"))
    parser.add_argument("--json", default=None, help="optional path for the readings; keep it outside reports/")
    args = parser.parse_args()

    for name in ("BEIDOU_TRIALS_LEDGER", "BEIDOU_FEATURE_STORE"):
        if os.environ.get(name):
            raise SystemExit(f"{name} is set; unset it so nothing is read from or written to another place")

    from beidou_cli.research_feature_store import feature_scores
    from beidou_cli.research_panel import _entry, _load, _membership, _resolve_symbols
    from beidou_live.composition import cost_model
    from beidou_shared.config import load_yaml

    symbols = _resolve_symbols(args.root, "", "1h", "pit")
    panel = _load(args.root, symbols, "1h", None, args.to, True, metrics=True, spot=True)
    min_history = int((load_yaml(args.profile).get("portfolio", {}) or {}).get("min_history_bars", 720))
    eligible = panel.close.notna().cumsum() >= min_history
    membership = _membership(args.root, "pit", panel, 0)
    if membership is not None:
        eligible &= membership
    print(f"panel: {len(panel.symbols)} symbols x {len(panel.index)} bars, {panel.index[0]} -> {panel.index[-1]}")

    full_cost = cost_model(load_yaml(args.costs), use_funding=True)
    zero_cost = CostModel(0.0, 0.0, False)
    tsmom_entry = _entry("tsmom", args.registry, "")
    tsmom = feature_scores("tsmom", tsmom_entry.params, panel).where(eligible)
    flow = feature_scores("flow", _entry("flow", args.registry, "").params, panel).where(eligible)
    _, tsmom_net = equal_notional(panel, tsmom, zero_cost, tsmom_entry.entry_threshold)
    crowd_raw = panel.metric(CROWD)
    crowd = None if crowd_raw is None else to_scores(crowd_raw / crowd_raw.rolling(168, min_periods=168).mean() - 1.0, eligible)

    spot_buy, spot_quote = spot_taker_columns(args.root, panel)
    raws = formulations(panel, spot_buy, spot_quote)
    readings: dict[str, dict[str, Any]] = {}
    for name, raw in raws.items():
        scores = to_scores(raw, eligible)
        zero_summary, zero_net = equal_notional(panel, scores, zero_cost)
        full_summary, _ = equal_notional(panel, scores, full_cost)
        overlap_with = flow if name.startswith("B") else crowd
        readings[name] = {
            "width": data_width(scores),
            "ic": ic_rows(scores, panel.close),
            "zero_cost": zero_summary,
            "full_cost": full_summary,
            "tsmom_daily_correlation": daily_correlation(zero_net, tsmom_net, _first_scored_bar(scores)),
            "overlap": {
                "with": "flow" if name.startswith("B") else "lsr(168) on count_long_short_ratio",
                "mean_xs_spearman": None if overlap_with is None else xs_overlap(scores, overlap_with),
            },
        }
        width = readings[name]["width"]
        print(
            f"\n== {name}: {width['symbols_with_scores']} symbols, median {width['median_symbols_per_bar']:.0f}/bar, "
            f"{width['days_with_scores']} days from {width['first_bar']}, coverage {width['coverage']:.3f}"
        )
        print("horizon | ts-IC | xs-IC | NW t | NW p | non-overlap IC | long n / mean fwd | short n / mean fwd")
        for row in readings[name]["ic"]:
            print(
                f"{row['horizon']:>7} | {_fmt(row['ts_ic'])} | {_fmt(row['xs_ic'])} | {_fmt(row['nw_t'], '+.2f')} | "
                f"{_fmt(row['nw_p'], '.4f')} | {_fmt(row['nonoverlap_ic'])} | {row['long_n']} / "
                f"{_fmt(row['long_mean_fwd'], '+.4%')} | {row['short_n']} / {_fmt(row['short_mean_fwd'], '+.4%')}"
            )
        print(
            f"equal-notional Sharpe: zero cost {_fmt(_sharpe(zero_summary), '+.2f')}, "
            f"full cost {_fmt(_sharpe(full_summary), '+.2f')}; "
            f"daily corr with tsmom {_fmt(readings[name]['tsmom_daily_correlation'], '+.3f')}; "
            f"mean xs Spearman with {readings[name]['overlap']['with']} "
            f"{_fmt(readings[name]['overlap']['mean_xs_spearman'], '+.3f')}"
        )

    verdicts = [decide(family, readings) for family in FAMILIES]
    print()
    for verdict in verdicts:
        print(f"family {verdict['family']}: {verdict['verdict']} {json.dumps(verdict.get('passing'), default=str)}")
    if args.json:
        path = Path(args.json)
        if "reports" in path.resolve().parts:
            raise SystemExit("--json must not point under reports/: this look spends no ledger and leaves no report")
        payload = {"to": args.to, "readings": readings, "verdicts": verdicts}
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
