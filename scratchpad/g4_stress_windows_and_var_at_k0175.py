"""G4 在 k = 0.175 上重放（2026-09-29）：崩盘窗口、一日 VaR / ES，以及每个窗口的进场净敞口。

起因。backtest-guard 2026-09-29 体检（`docs/analysis/2026-09-29-backtest-guard-audit.md`）第一遍的 🟡：
09-23 的 `scratchpad/g4_stress_windows_and_var_at_k060.py` 量的是 k = 0.60。k 在 09-27 换成 0.175
（#163，重启 #59 载入）之后，两处读者落在了后面：

* `beidou_live.report_risk.tail_readings` 的常量只属于一个 vol_target，于是它在 0.175 上拒绝换算，日报
  「Tail beside the sigma ruler (G4)」一节从那天起印 n/a。
* `config/alpha_registry.yaml` 写着「k = 0.175 上没有重放过」。

09-25 的体检另要两样东西，本脚本一起补上：2025-10-10 那次连环强平作为一个窗口；每个窗口的进场净敞口，
好把「从多头进崩盘」与「从空头进崩盘」分开读。

跑的是什么。与 09-23 那份同一本书：`build_model(registry, profile)`，只换 profile 的 `vol_target`；时点面板；
exit overlay；`run_backtest` 带两个组合层护栏与实际资金费；再用上线的 `ladder_step` 按盯市尺子走 R8，
`base` 取所跑的 k。两列：

* 0.175：profile 里的 k，从 config 读，不在这里写死。档位用 `Policy().drawdown_ladder`，即循环在跑的
  policy 0.3.6。
* 0.60：09-23 那张表描述的书，作对照。档位用它在跑时生效的那一组 ((-0.49, 0.45), (-0.70, 0.30))，即
  `rescaled(0.60, budget=0.70)`（policy 0.3.3 至 0.3.5）。拿它对 09-23 的表，能分开「数据变了」与「k 变了」。

与 09-23 那份的差别。旧脚本一字不改。

1. 再平衡带。09-23 时 `combine_books` 只带 D2，那份脚本自己套 D2 + D3，并核对 `entry_multiple` 取 1.0 时
   手套的带等于 `evaluate` 的。#115 之后 `combine_books` 带的就是 D2 + D3，旧的核对会让旧脚本停下。
   这里把核对倒过来：按 profile 的 `band_entry_multiple` 手套的带，必须与 `evaluate` 自己的权重逐位相等，
   不等就停。只有 D2 的敏感度随之去掉：它当初存在，是因为 `combine_books` 少 D3。
2. 多一个窗口：2025-10 tariff liquidation cascade（2025-10-08 至 10-20）。它是样本里唯一一次从上升趋势里
   按小时砸下来的崩盘，其余几次都砸在已经走弱的市场里。
3. 每个窗口的进场净敞口：执行权重之和（exit 与两个护栏之后），乘阶梯的标量。给三个读数：窗口开始前
   168 根 bar 的均值与最后一根；市场最差那一小时的净敞口与书在那一小时的收益，市场取当期成员的等权篮子；
   书自己最差那一小时的净敞口。第二个回答「崩盘那一刻书站在哪边」，第三个回答「书在哪一小时伤得最重」。
4. `--to` 默认 2026-09-28（不含）：完整 UTC 日到 2026-09-27。

VaR / ES 的定义与 09-23 相同，跑之前定下：只数完整 UTC 日（24 根 bar），日收益是逐 bar 净收益的复利。
n 天升序排列，m = ceil(n × (1 − level))，VaR 是第 m 差那天的相反数，ES 是最差 m 天均值的相反数。

照旧带着的缺口，与 09-23 那份列的相同：阶梯的标量乘在净收益上，不重推权重路径；flow sleeve 的探针止损
不重放；2020-03 早于归档起点。

零 ledger：不调任何 `research` 命令。`reports/research/trials.jsonl` 跑前跑后各算一次 sha256，不等就报错；
没设 `BEIDOU_TRIALS_LEDGER` 时把它指向一个哨兵文件，结尾断言那个文件不存在。

输出写到 `--out`（默认 `reports/research/g4-k0175-20260929/g4-tail-readings.json`）。它是列表，第 0 项是
meta：`governance replay` 只读 `reports/research` 顶层的 dict 形 JSON，列表放在子目录里，两层都读不到。

    PYTHONPATH=. .venv/bin/python scratchpad/g4_stress_windows_and_var_at_k0175.py \
        --data-root .beidou/data [--to 2026-09-28] [--out PATH]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import g4_stress_windows_and_var_at_k060 as g4

from beidou_alpha.backtest import benchmark_returns, run_backtest
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_alpha.portfolio import apply_no_trade_band
from beidou_alpha.validation.metrics import compound, max_drawdown, sharpe
from beidou_cli.research_cmd import _load, _membership, _resolve_symbols
from beidou_data.pool import MEMBERSHIP_FILE
from beidou_governance.policy import Policy
from beidou_live.composition import build_model, cost_model, load_registry
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config" / "live.demo.yaml"
K_CONTROL = 0.60
#: The rungs in force while k was 0.60 (policy 0.3.3-0.3.5, 2026-09-14 -> 09-27).  `Policy()` today carries
#: 0.175's rungs; at 0.60 their targets would be 0.22 and 0.15 of the base, a ladder that book never ran.
RUNGS_AT_060 = ((-0.49, 0.45), (-0.70, 0.30))
LOOKBACK_BARS = 168
WINDOWS = (*g4.WINDOWS, ("2025-10 tariff liquidation cascade", "2025-10-08", "2025-10-20"))
LEDGER = ROOT / "reports" / "research" / "trials.jsonl"
DEFAULT_OUT = ROOT / "reports" / "research" / "g4-k0175-20260929" / "g4-tail-readings.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tag(k: float) -> str:
    return f"k{k:.3f}"


def live_weights(k: float, panel: Any, membership: pd.DataFrame | None) -> pd.DataFrame:
    """The registry book at `vol_target = k`, banded the way the loop bands it (D2 + D3).

    Stops unless the hand-applied band at the profile's `band_entry_multiple` IS `evaluate`'s own band.
    """
    tuned = copy.deepcopy(load_yaml(PROFILE))
    tuned.setdefault("portfolio", {})["vol_target"] = k
    model = build_model(load_registry(ROOT / "config" / "alpha_registry.yaml"), tuned)
    per = model.strategy_targets(panel, membership)
    unbanded = model.weights_from(per, panel.close, panel.bars_per_year, band=False)
    own = model.weights_from(per, panel.close, panel.bars_per_year)
    p = model.portfolio
    hand = apply_no_trade_band(unbanded, p.no_trade_band, p.no_trade_rel_band, p.flat_inside_band, p.band_entry_multiple)
    if not (own.shape == hand.shape and np.array_equal(own.to_numpy(), hand.to_numpy(), equal_nan=True)):
        raise SystemExit(f"k={k}: the hand-applied band at entry_multiple {p.band_entry_multiple} is not evaluate's band")
    return own


def replay(
    k: float, panel: Any, weights: pd.DataFrame, rungs: tuple[tuple[float, float], ...]
) -> tuple[pd.Series, pd.Series, dict[str, Any], pd.DataFrame]:
    """Hourly net return and net exposure at `vol_target = k`: exits, both guards, then R8 at these rungs."""
    profile = load_yaml(PROFILE)
    registry = load_registry(ROOT / "config" / "alpha_registry.yaml")
    exits = ExitParams.from_mapping({**(profile.get("exits") or {}), "bars_per_day": 24})
    pf = profile.get("portfolio", {}) or {}
    guards = BookGuardParams(
        max_weight=float(pf["max_weight"]),
        max_gross=float(pf["max_gross"]),
        daily_loss_pause=float(profile["guards"]["daily_loss_pause"]),
    )
    weights = apply_exits(weights, panel.close, exits).weights if exits.enabled else weights
    result = run_backtest(
        panel, weights, cost_model(load_yaml(ROOT / "config" / "costs.yaml"), use_funding=True), guards=guards
    )
    raw = result.portfolio_net
    scalars = g4.ladder_scalars(raw.to_numpy(dtype=float), k, replace(Policy(), drawdown_ladder=rungs))
    net = pd.Series(raw.to_numpy(dtype=float) * scalars, index=raw.index)
    exposure = pd.Series(result.weights.reindex(raw.index).sum(axis=1).to_numpy(dtype=float) * scalars, index=raw.index)
    events = result.guard_events if result.guard_events is not None else pd.DataFrame()
    info = {
        "k": k,
        "portfolio": {**pf, "vol_target": k},
        "exits": profile.get("exits"),
        "daily_loss_pause": guards.daily_loss_pause,
        "ladder_rungs": [list(rung) for rung in rungs],
        "sleeves": {name: spec.fraction for name, spec in registry.books.items()},
        "strategies": [entry.id for entry in registry.enabled],
        "bars": len(net),
        "first_bar": str(net.index[0]),
        "last_bar": str(net.index[-1]),
        "ladder_bars_below_1": int((scalars < 1.0 - 1e-12).sum()),
        "unladdered_min_drawdown": float(max_drawdown(raw)),
        "daily_loss_pause_bars": int(events["daily_loss_pause"].sum()) if "daily_loss_pause" in events else None,
        "gross_capped_bars": int(events["gross_capped"].sum()) if "gross_capped" in events else None,
        "net_exposure": {"mean": float(exposure.mean()), "share_net_long": float((exposure > 0).mean())},
        "full_sample": {
            "compound": compound(net),
            "cagr": float(np.prod(1.0 + net.to_numpy()) ** (panel.bars_per_year / len(net)) - 1.0),
            "sharpe": sharpe(net, panel.bars_per_year),
            "max_drawdown": max_drawdown(net),
        },
    }
    return net, exposure, info, events.reindex(net.index)


def window_row(
    label: str, start: str, end: str, ks: tuple[float, ...], nets: dict, exposures: dict, events: dict, bench: pd.Series
) -> dict[str, Any]:
    first = ks[0]
    index = nets[first].index
    opened = pd.Timestamp(start, tz="UTC")
    inside = (index >= opened) & (index < pd.Timestamp(end, tz="UTC"))
    before = (index >= opened - pd.Timedelta(hours=LOOKBACK_BARS)) & (index < opened)
    row: dict[str, Any] = {"kind": "window", "window": label, "start": start, "end": end, "bars": int(inside.sum())}
    if row["bars"] < 24:
        row["in_sample"] = False
        return row
    row["in_sample"] = True
    market = bench.loc[nets[first].loc[inside].index]
    for k in ks:
        slab = nets[k].loc[inside]
        going_in = exposures[k].loc[before]
        worst = slab.idxmin()
        crash = market.idxmin()
        bound = events[k].loc[slab.index]
        row[tag(k)] = {
            "return": compound(slab),
            "sharpe": sharpe(slab, 8760.0),
            "max_drawdown": max_drawdown(slab),
            "pause_bars": int(bound["daily_loss_pause"].sum()),
            "gross_capped_bars": int(bound["gross_capped"].sum()),
            "net_in_mean_168": float(going_in.mean()) if len(going_in) else None,
            "net_in_last": float(going_in.iloc[-1]) if len(going_in) else None,
            "worst_hour": str(worst),
            "worst_hour_return": float(slab.min()),
            "net_at_worst_hour": float(exposures[k].loc[worst]),
            "net_at_market_worst_hour": float(exposures[k].loc[crash]),
            "return_at_market_worst_hour": float(nets[k].loc[crash]),
        }
    row["benchmark_members"] = compound(market)
    row["market_worst_hour"] = str(market.idxmin())
    row["market_worst_hour_return"] = float(market.min())
    return row


def tail_row(k: float, net: pd.Series) -> dict[str, Any]:
    daily = g4.complete_days(net)
    levels = {f"{level:.2f}": g4.var_es(daily, level) for level in g4.LEVELS}
    sd = float(daily.std(ddof=1))
    return {
        "kind": "tail",
        "k": k,
        "days": len(daily),
        "first_day": str(daily.index[0].date()),
        "last_day": str(daily.index[-1].date()),
        "levels": levels,
        "daily_sd": sd,
        "design_daily_sigma": k / math.sqrt(365.0),
        "in_sd": {f"{name}_{level[2:]}": v[name] / sd for level, v in levels.items() for name in ("var", "es")},
        "worst_days": {str(stamp.date()): float(value) for stamp, value in daily.nsmallest(6).items()},
        "days_below_daily_loss_pause": int((daily < -0.05).sum()),
    }


def main(data_root: str, to: str, out: Path) -> None:
    sentinel = None
    if not os.environ.get("BEIDOU_TRIALS_LEDGER"):
        sentinel = out.parent / "ledger-must-not-exist.jsonl"
        os.environ["BEIDOU_TRIALS_LEDGER"] = str(sentinel)
    ledger_before = _sha(LEDGER)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    k_now = float(load_yaml(PROFILE)["portfolio"]["vol_target"])
    columns = ((k_now, tuple(tuple(rung) for rung in Policy().drawdown_ladder)), (K_CONTROL, RUNGS_AT_060))
    ks = tuple(k for k, _ in columns)
    print(f"HEAD {head}; k now {k_now} (profile), control {K_CONTROL}; trials.jsonl sha256 {ledger_before}")

    table_path = Path(data_root) / MEMBERSHIP_FILE
    digest = hashlib.sha256(table_path.read_bytes()).hexdigest()
    symbols = _resolve_symbols(data_root, "", "1h", "pit")
    panel = _load(data_root, symbols, "1h", None, to, True)
    membership = _membership(data_root, "pit", panel, 0)
    bench = benchmark_returns(panel, "open_to_close", membership=membership)
    print(f"pit panel: {len(panel.symbols)} symbols x {len(panel.index):,} bars, {panel.index[0]} -> {panel.index[-1]}")
    print(f"membership.parquet sha256 {digest}")

    nets: dict[float, pd.Series] = {}
    exposures: dict[float, pd.Series] = {}
    infos: dict[float, dict[str, Any]] = {}
    events: dict[float, pd.DataFrame] = {}
    for k, rungs in columns:
        nets[k], exposures[k], infos[k], events[k] = replay(k, panel, live_weights(k, panel, membership), rungs)
        info, fs = infos[k], infos[k]["full_sample"]
        print(
            f"k={k}: {info['bars']:,} bars {info['first_bar']} -> {info['last_bar']}  CAGR {fs['cagr']:+.2%}  "
            f"Sharpe {fs['sharpe']:.3f}  MDD {fs['max_drawdown']:.2%}  ladder bars {info['ladder_bars_below_1']} "
            f"(unladdered MDD {info['unladdered_min_drawdown']:.2%})  pause bars {info['daily_loss_pause_bars']}  "
            f"gross-capped bars {info['gross_capped_bars']}  net exposure mean {info['net_exposure']['mean']:+.3f} "
            f"(net long on {info['net_exposure']['share_net_long']:.1%} of bars)"
        )
    print("the hand-applied band at band_entry_multiple equals evaluate's own band bit for bit at both k")

    rows = [window_row(label, start, end, ks, nets, exposures, events, bench) for label, start, end in WINDOWS]
    print(f"\n{'window':<36}{'bars':>5}" + "".join(
        f"{f'ret@{k}':>10}{f'mdd@{k}':>10}{f'in@{k}':>9}{f'crash@{k}':>10}{f'hit@{k}':>9}" for k in ks
    ) + f"{'bench':>9}{'mkt-worst':>11}")
    for row in rows:
        if not row["in_sample"]:
            print(f"{row['window']:<36}{row['bars']:>5}   NOT IN SAMPLE")
            continue
        line = f"{row['window']:<36}{row['bars']:>5}"
        for k in ks:
            cell = row[tag(k)]
            line += (
                f"{cell['return']:>+10.4f}{cell['max_drawdown']:>10.4f}{cell['net_in_mean_168']:>+9.3f}"
                f"{cell['net_at_market_worst_hour']:>+10.3f}{cell['return_at_market_worst_hour']:>+9.4f}"
            )
        print(line + f"{row['benchmark_members']:>+9.4f}{row['market_worst_hour_return']:>+11.4f}")
    print(
        "in@k = mean net exposure over the 168 bars before the window; crash@k / hit@k = net exposure and book "
        "return on the market's worst hour (members' equal-weight basket, mkt-worst)"
    )
    print("market's worst hour per window: " + "; ".join(
        f"{row['window'][:7]} {row['market_worst_hour'][:16]}" for row in rows if row["in_sample"]
    ))
    print("worst hour per window: " + "; ".join(
        f"{row['window'][:7]} {row[tag(ks[0])]['worst_hour'][:16]} {row[tag(ks[0])]['worst_hour_return']:+.2%}"
        for row in rows if row["in_sample"]
    ) + f"  (k={ks[0]})")
    print("guards inside each window (bars): " + "; ".join(
        f"{row['window'][:7]} pause " + "/".join(str(row[tag(k)]['pause_bars']) for k in ks)
        + " capped " + "/".join(str(row[tag(k)]['gross_capped_bars']) for k in ks)
        for row in rows if row["in_sample"]
    ) + f"  (k={'/'.join(str(k) for k in ks)})")

    tails = [tail_row(k, nets[k]) for k in ks]
    print("\none-day tail, historical, fractions of equity (positive = loss):")
    for tail in tails:
        text = "  ".join(
            f"VaR{level[2:]} {v['var']:.4%} ES{level[2:]} {v['es']:.4%} (m={v['m']})" for level, v in tail["levels"].items()
        )
        print(f"k={tail['k']}: {tail['days']:,} complete UTC days {tail['first_day']} -> {tail['last_day']}  {text}")
        print("        worst days: " + ", ".join(f"{day} {value:+.2%}" for day, value in tail["worst_days"].items()))
        in_sd = tail["in_sd"]
        print(
            f"        daily sd {tail['daily_sd']:.4%} (design {tail['design_daily_sigma']:.4%}); in sd: VaR95 "
            f"{in_sd['var_95']:.3f} ES95 {in_sd['es_95']:.3f} VaR99 {in_sd['var_99']:.3f} ES99 {in_sd['es_99']:.3f}; "
            f"days below the -5% pause line {tail['days_below_daily_loss_pause']}"
        )

    ledger_after = _sha(LEDGER)
    if ledger_after != ledger_before:
        raise RuntimeError(f"trials.jsonl changed during the run: {ledger_before} -> {ledger_after}")
    if sentinel is not None and sentinel.exists():
        raise RuntimeError(f"something wrote a ledger row to {sentinel}")
    meta = {
        "kind": "meta",
        "script": "scratchpad/g4_stress_windows_and_var_at_k0175.py",
        "head": head,
        "data_root": data_root,
        "to_exclusive": to,
        "membership_sha256": digest,
        "panel": {"symbols": len(panel.symbols), "bars": len(panel.index),
                  "first": str(panel.index[0]), "last": str(panel.index[-1])},
        "replays": {tag(k): infos[k] for k in ks},
        "trials_sha256_before": ledger_before,
        "trials_sha256_after": ledger_after,
        "ledger_sentinel_checked": sentinel is not None,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([meta, *rows, *tails], indent=1, default=str) + "\n", encoding="utf-8")
    print(f"\ntrials.jsonl unchanged; wrote {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", default=".beidou/data")
    parser.add_argument("--to", default="2026-09-28")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    main(args.data_root, args.to, Path(args.out).resolve())
