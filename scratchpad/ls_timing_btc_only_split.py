"""零 ledger：择时那条线索里，有多少来自 2021 年只有 BTC 有多空比的那 10 个月。

起因。2026-09-27 拆 LS 叶那 4 个正形状（#199，`scratchpad/ls_direction_look.py`）：四个都「只在赌方向：择时」，
排第一的 `8a838550a686852d` 择时部分 Sharpe 1.55、Newey-West t 3.64。同一份输出的第 3 行说
`count_long_short_ratio` 只有 BTCUSDT 从 2021-01-01 起有，其余 17 个币最早 2021-12-01；候选 2021-01-31 起下单，
所以头 10 个月它只能交易 BTC 一个币，那段是单币择时。t 3.64 里有多少来自那段，那份输出分不出来。
`governance/reopen.yaml` 的 `ls-leaf` 把「写预登记之前先把这一段拆开」写进了条件。操作者 2026-09-28T04:03:28Z
在「挖掘更多策略和因子」话题的卡片上选了「立项择时」，卡片写的后果是「先零 ledger 拆开只有 BTC 的那 10 个月，
再写择时预登记（对照大盘），花 ledger 前再问；过了还要补实盘数据通道」。这是第一步。

**零 ledger。** 不写 `trials.jsonl`、不写 `reports/`；不挑形状、不换窗口、不试新写法。四个形状 09-09 已按 DL-K2
计入 ledger，#199 把它们的仓位拆成了几条逐 bar 收益，这里只把那几条按时段切开。

**复现先于解读。** 拆的办法逐字是 #199 钉住的那一份：这个脚本按 sha256 载入 `ls_direction_look.py`（必须是
`e4dd5a59…`，#199 那一版），用它的设置、它的函数。逐 bar 的几条流照它 `look_at()` 的步骤再算一遍，全样本的读数
要与同一次运行里 `look_at()` 自己算出来的逐位相同（差 ≤ 1e-9），也要与 2026-09-27 Mac 上那一次的输出相符（候选
Sharpe 与择时部分的 t，差 ≤ 0.05）；`look_at()` 自己对 mine 报告的复现照旧。任何一条不符，标「未复现」。

**怎么切：按数据，不按眼睛。** 每个形状各切各的：切点是候选第一次能同时交易两个币的那根 bar（`look_at()` 里的
`tradable`，按行数到 ≥ 2；按持仓的那根 bar 算，因为 t 的收益来自 t − 1 定下的仓位）。A 段是候选开始下单到切点
之前，B 段是切点到最后一根。打印切点、A 段每根 bar 能交易几个币（应当只有 BTC 一个，个别 bar 可能是 0）、B 段
最少几个。A 段里 S 恒为 0：候选只有一个币可拿，净方向就是它的全部仓位。

**读数，看之前定死。** 每段各一份，全样本一份作复现：

1. 候选、恒定多、恒定空：Sharpe、NW t。恒定持仓取候选那一段 bar，缺的补 0，与 #199 的 falsifier B 同一个写法；
   只报，不判。
2. 净方向 D、恒定部分 C、择时部分 T、选币 S，都是 #199 那几条带自己资金费与分摊手续费的净收益：Sharpe、NW t、
   按年净收益（占权益），以及这一段占全样本净收益的份额（全样本合计 ≤ 0 时不报）。C 与 T 的分界 n̄ 仍是全样本的
   均值，与 #199 相同；各段自己的平均净敞口在第 4 条里报。
3. 一把尺：如果择时部分的 Sharpe 在各段一样，这一段的 t 该是全样本 t × √(这一段 bar 数 / 全样本 bar 数)。只报。
4. 各段的平均净敞口，与净敞口对篮子过去 168 根 bar 收益的相关（09-27 全样本 +0.21）。只报，给择时预登记挑对照用。
5. B 段 S 的 NW t，与 S 在 B 段上对两个方向因子（b 篮子、D 的毛收益）回归以后的 t，β 只在 B 段上拟合。只核对
   09-27 那句推算（「只看有横截面的那段，S 的 t 也不过 0.6 上下」），不改 `ls-leaf` 的判定。

**判定，每个形状一次；叶的结论看 `8a838550a686852d`，另外三个照报：**

- 候选从头到尾都只有一个币可拿（没有切点）：「没有横截面那段」，只报。
- B 段择时部分 T 的 NW t ≥ 2.0：「有横截面的那段也在」。
- 否则：「有横截面的那段分不出噪声」。
- 未复现的，结论后面标「（未复现）」。

2.0 与 #199 的判定线同一个数。它不是上线的门：B 段也是挖出来以后看的，只回答「值不值得为它写一份预登记」。

**结论之后跟着什么，也先写下：**

- 「有横截面的那段也在」：写择时预登记。对照是大盘（恒定持有同一个篮子）与简单趋势跟随（净敞口跟着篮子过去一段
  的涨跌走）——第 4 条的相关就是为挑这个对照读的；数据用多空比的全部历史，A 段单列、单独报。预登记入库以后、
  花 ledger 之前问操作者。
- 「有横截面的那段分不出噪声」：先不写预登记，把读数报给操作者，建议不为它花 ledger——剩下的证据是 2021 年 BTC
  一个币十个月的择时。要不要继续由操作者定。
- 「未复现」：只报读数，先查为什么对不上，不写预登记。
- 哪种结论都不改 `reopen.yaml`。第 5 条若 B 段 S 的两个 t 都 ≥ 2.0，照实报告，由操作者决定要不要重审 `ls-leaf`。

**合成数据试跑**（定判定线之前，不碰真实数据）：`scratchpad/ls_timing_btc_only_split_synthetic.py` 造三个世界，
多空比只有 BTC 从头开始有、其余 17 个币晚五个月才有，大盘的漂移跟着全市场多空比走，只是两段的强弱不同：
btc_only_edge（只有 BTC 那段强，有横截面那段没有）、both（两段都有）、late_edge（只有横截面那段有）。正确答案是
造的时候定的：「分不出噪声」「也在」「也在」。三个世界的四个形状都要判对，这条判定线才入库；2026-09-28 这一版
12 个全对。btc_only_edge 是要防的陷阱：四个里三个全样本的择时 t 过了 2（排第一的 2.65），B 段一个都没过（1.43 /
0.79 / −0.22 / 1.47），判定线把四个都判成了「分不出噪声」。第一版合成世界两段一样强，A 段太短、只有一个币，全样本
t 起不来，陷阱没被试到，加强了 A 段；判定线本身一个字没改。读数与经过写在那份脚本的 docstring 里。合成数据的
「未复现」是对的：它的日期范围本来就不是报告的。

**预期**（按惯例写下来，事后不能改口）：「有横截面的那段也在」。择时部分 09-27 的全样本 t 是 3.64；若各段一样，
B 段约占全样本 bar 数的 85%，t 该在 3.4 上下。2021 年 BTC 大涨大跌，单币择时那段可能挣得不成比例，所以 B 段
大概不到 3.4，但不至于掉到 2 以下。

用法（在 `~/beidou` 之外的 worktree 里，读主 checkout 的数据；环境里不能有 `BEIDOU_TRIALS_LEDGER` 与
`BEIDOU_FEATURE_STORE`）：

    PYTHONPATH=$PWD /Users/maguannan/beidou/.venv/bin/python scratchpad/ls_timing_btc_only_split.py \\
        --root /Users/maguannan/beidou/.beidou/data --json /tmp/ls_timing_btc_only_split.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_alpha.backtest import CostModel, run_backtest
from beidou_alpha.mining.search import Candidate, to_signal
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, asset_vol, build_weights
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals import register as register_signal
from beidou_alpha.validation.metrics import newey_west_tstat, sharpe

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "scratchpad" / "ls_direction_look.py"
PINNED_SHA256 = "e4dd5a59613ce1af83f1cd58c9e06ab534de8a8f2752ea9d17db1fae11dbdebb"  # #199, 353988b
EXACT = 1e-9  # the streams are rebuilt with the pinned steps, so the full-sample readings must match bit for bit
# What the 2026-09-27 run printed on the Mac (RESEARCH_LOG 「LS 叶那 4 个正形状只在赌方向（择时）」): the candidate's
# Sharpe from its falsifier-B line and the timing part's Newey-West t, as printed.
ON_09_27: dict[str, tuple[float, float]] = {
    "8a838550a686852d": (1.372, 3.64),
    "49742e605be41b26": (1.029, 3.95),
    "2ca33804038a5a52": (0.642, 3.39),
    "8a87c4aea11c7a7a": (0.721, 2.45),
}
SIGNIFICANT_T = 2.0  # the same line #199 drew
STREAMS = (
    "candidate",
    "constant_long",
    "constant_short",
    "direction",
    "direction_constant",
    "direction_timing",
    "selection",
)
PERIODS = ("all", "A", "B")
# Whose split of the full-sample net is printed: the streams this look is about.  A share of a small or mixed-sign
# total (the constant part, the controls) reads as -1600% and says nothing.
SHARED = ("candidate", "direction", "direction_timing")

NO_CROSS_SECTION = "没有横截面那段"
CARRIES_ON = "有横截面的那段也在"
NOT_IN_CROSS_SECTION = "有横截面的那段分不出噪声"


def load_pinned() -> ModuleType:
    """#199's script, byte for byte, or nothing: the split has to be of the streams that were ruled on."""
    body = PINNED.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != PINNED_SHA256:
        raise SystemExit(f"{PINNED.name} is {digest[:12]}, not #199's {PINNED_SHA256[:12]}; the split would not be of that look")
    spec = importlib.util.spec_from_file_location("ls_direction_look", PINNED)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {PINNED}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the streams, rebuilt with the pinned steps -----------------------------------------------------------


def streams(
    pinned: ModuleType,
    candidate: Candidate,
    panel: Panel,
    portfolio: PortfolioParams,
    history: int,
    cost: CostModel,
    sigma: pd.DataFrame,
) -> dict[str, Any]:
    """`look_at()`'s steps 2 and 3, returning the per-bar series instead of their summaries."""
    bpy = panel.bars_per_year
    spec = register_signal(to_signal(candidate))
    model = AlphaModel(
        entries=(StrategyEntry(id=spec.id, params=dict(spec.default_params)),),
        portfolio=portfolio,
        interval=panel.interval,
        min_history_bars=history,
    )
    weights, _combined, per_strategy = model.evaluate(panel, None)
    own = weights.reindex(index=panel.index, columns=panel.symbols)
    tradable = per_strategy[spec.id].notna().reindex(index=panel.index, columns=panel.symbols)
    tradable = tradable.fillna(False).astype(bool)
    started = pinned.started_rows(own)

    constant = pd.DataFrame(np.where(tradable.to_numpy(), 1.0, np.nan), index=panel.index, columns=panel.symbols)
    held = {
        "constant_long": run_backtest(panel, build_weights(constant, panel.close, bpy, portfolio), cost),
        "constant_short": run_backtest(panel, build_weights(-constant, panel.close, bpy, portfolio), cost),
    }

    filled = own.fillna(0.0)
    net = filled.sum(axis=1)
    proportions = pinned.basket(tradable, sigma.reindex(index=panel.index, columns=panel.symbols))
    net_mean = float(net[started].mean())
    direction = proportions.mul(net, axis=0)
    parts_weights = {
        "candidate": own,
        "direction": pinned.on_started(direction, started),
        "selection": pinned.on_started(filled - direction, started),
        "direction_constant": pinned.on_started(proportions * net_mean, started),
        "direction_timing": pinned.on_started(proportions.mul(net - net_mean, axis=0), started),
        "basket": pinned.on_started(proportions, started),
    }
    parts = {name: run_backtest(panel, frame, cost) for name, frame in parts_weights.items()}
    funding_only_cost = CostModel(turnover_bps=0.0, carry_bps_per_bar=0.0, use_funding=cost.use_funding)
    funding_only = {
        name: run_backtest(panel, parts_weights[name], funding_only_cost) for name in ("candidate", *pinned.PARTS)
    }
    nets, _asked = pinned.shared_cost_nets(parts, funding_only, cost)

    own_net = parts["candidate"].portfolio_net
    series = {
        "candidate": own_net,
        # falsifier B's own alignment: the candidate's bars, a bar the constant holding did not trade counting flat
        **{name: result.portfolio_net.reindex(own_net.index).fillna(0.0) for name, result in held.items()},
        **{name: nets[name].reindex(own_net.index) for name in pinned.PARTS},
    }
    return {
        "series": series,
        "factors": {
            "basket": parts["basket"].portfolio_gross.reindex(own_net.index),
            "direction": parts["direction"].portfolio_gross.reindex(own_net.index),
        },
        # counted on the bar the position is held: the return at t is earned by the weights decided at t - 1
        "names": tradable.sum(axis=1).shift(1).reindex(own_net.index),
        # on the decision bar, as #199's exposure profile has it, so it lines up with the basket's past week
        "net_exposure": net.reindex(own_net.index),
    }


# --- the split -------------------------------------------------------------------------------------------


def split_bar(names: pd.Series) -> pd.Timestamp | None:
    """The first bar at which the candidate could hold two names."""
    two = names[names >= 2]
    return None if two.empty else pd.Timestamp(two.index[0])


def masks(index: pd.Index, split: pd.Timestamp | None) -> dict[str, np.ndarray]:
    everything = np.ones(len(index), dtype=bool)
    if split is None:
        return {"all": everything, "A": everything, "B": ~everything}
    after = np.asarray(index >= split)
    return {"all": everything, "A": ~after, "B": after}


def period_stats(net: pd.Series, total: float, bars_per_year: float) -> dict[str, Any]:
    values = net.dropna()
    if values.empty:
        return {"bars": 0, "sharpe": None, "t_stat": None, "per_year": None, "share_of_total": None, "all_zero": None}
    summed = float(values.sum())
    return {
        "bars": len(values),
        "sharpe": sharpe(values, bars_per_year),
        "t_stat": newey_west_tstat(values)["t_stat"],
        "per_year": float(values.mean() * bars_per_year),
        "share_of_total": summed / total if total > 0 else None,
        "all_zero": bool((values.abs() < 1e-15).all()),
    }


def exposure_stats(net_exposure: pd.Series, basket_return: pd.Series) -> dict[str, Any]:
    joined = pd.concat([net_exposure, basket_return.reindex(net_exposure.index)], axis=1).dropna()
    return {
        "mean_net": float(net_exposure.mean()) if len(net_exposure) else None,
        "corr_net_with_trailing_basket_return": (
            float(joined.iloc[:, 0].corr(joined.iloc[:, 1])) if len(joined) > 30 else None
        ),
    }


def split_look(
    pinned: ModuleType, built: Mapping[str, Any], basket_return: pd.Series, bars_per_year: float
) -> dict[str, Any]:
    series: Mapping[str, pd.Series] = built["series"]
    index = series["candidate"].index
    names: pd.Series = built["names"]
    split = split_bar(names)
    cut = masks(index, split)
    periods: dict[str, dict[str, Any]] = {}
    for period in PERIODS:
        mask = cut[period]
        periods[period] = {
            name: period_stats(series[name][mask], float(series[name].sum()), bars_per_year) for name in STREAMS
        }
        periods[period]["exposure"] = exposure_stats(built["net_exposure"][mask], basket_return)
    timing_all = periods["all"]["direction_timing"]
    for period in ("A", "B"):
        full_t, share = timing_all["t_stat"], periods[period]["direction_timing"]["bars"] / max(timing_all["bars"], 1)
        periods[period]["timing_t_if_even"] = None if full_t is None else full_t * math.sqrt(share)

    b = cut["B"]
    selection_b = None
    if b.any():
        factors = {name: frame[b] for name, frame in built["factors"].items()}
        selection_b = {
            "t_stat": newey_west_tstat(series["selection"][b].dropna())["t_stat"],
            "beyond_direction": pinned.beyond_direction(series["selection"][b], factors, bars_per_year),
        }
    a = cut["A"] & (split is not None)
    names_in_a = names[a].value_counts().sort_index() if a.any() else pd.Series(dtype=float)
    return {
        "split": None if split is None else str(split),
        "first_bar": str(index[0]),
        "last_bar": str(index[-1]),
        "names_in_A": {str(int(k)): int(v) for k, v in names_in_a.items()},
        "fewest_names_in_B": None if not b.any() else int(names[b].min()),
        "periods": periods,
        "selection_in_B": selection_b,
    }


def decide(row: Mapping[str, Any]) -> tuple[str, str]:
    if row["split"] is None:
        return NO_CROSS_SECTION, f"{NO_CROSS_SECTION}（候选从头到尾都只有一个币可拿）"
    t_a = row["periods"]["A"]["direction_timing"]["t_stat"]
    t_b = row["periods"]["B"]["direction_timing"]["t_stat"]
    why = f"有横截面那段择时部分 t {_fmt(t_b, '.2f')}，要 ≥ {SIGNIFICANT_T}；只有 BTC 那段 t {_fmt(t_a, '.2f')}"
    if t_b is not None and t_b >= SIGNIFICANT_T:
        return CARRIES_ON, f"{CARRIES_ON}（{why}）"
    return NOT_IN_CROSS_SECTION, f"{NOT_IN_CROSS_SECTION}（{why}）"


# --- reproduction ----------------------------------------------------------------------------------------


def _gap(a: float | None, b: float | None) -> float:
    if a is None and b is None:
        return 0.0
    if a is None or b is None:
        return math.inf
    return abs(float(a) - float(b))


def against_look_at(pinned_row: Mapping[str, Any], periods_all: Mapping[str, Any]) -> float:
    """Largest gap between the rebuilt full-sample readings and what `look_at()` itself returned this run."""
    gaps = []
    for name in ("candidate", *("direction", "selection", "direction_constant", "direction_timing")):
        gaps.append(_gap(periods_all[name]["sharpe"], pinned_row[name]["sharpe"]))
        gaps.append(_gap(periods_all[name]["t_stat"], pinned_row[name]["t_stat"]))
    for name in ("constant_long", "constant_short"):
        gaps.append(_gap(periods_all[name]["sharpe"], pinned_row["falsifier_b"][name]))
    gaps.append(_gap(periods_all["direction_timing"]["t_stat"], pinned_row["t"]["timing"]))
    gaps.append(_gap(periods_all["selection"]["t_stat"], pinned_row["t"]["selection"]))
    return max(gaps)


def against_09_27(hash_: str, periods_all: Mapping[str, Any]) -> dict[str, Any]:
    then_sharpe, then_t = ON_09_27[hash_]
    deltas = {
        "candidate_sharpe": _delta(periods_all["candidate"]["sharpe"], then_sharpe),
        "timing_t": _delta(periods_all["direction_timing"]["t_stat"], then_t),
    }
    return {"deltas": deltas, "ok": all(d is not None and abs(d) <= 0.05 for d in deltas.values())}


def _delta(now: float | None, then: float) -> float | None:
    return None if now is None else float(now) - then


# --- output ----------------------------------------------------------------------------------------------


def _fmt(value: Any, spec: str = "+.3f") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return format(value, spec)


def print_row(row: Mapping[str, Any]) -> None:
    print(f"\n== {row['hash']}  {row['expression']}")
    repro = row["reproduction"]
    print(
        f"reproduction: rebuilt vs look_at() this run, largest gap {repro['vs_look_at']:.1e} (must be <= {EXACT:.0e}); "
        f"vs 09-27: candidate Sharpe Δ {_fmt(repro['vs_09_27']['deltas']['candidate_sharpe'])}, timing t Δ "
        f"{_fmt(repro['vs_09_27']['deltas']['timing_t'], '+.2f')}; look_at() vs the mine report "
        f"{'ok' if repro['vs_report'] else '未复现'} -> {'ok' if repro['ok'] else '未复现'}"
    )
    p = row["periods"]
    print(
        f"split: {row['split']} (first bar the candidate could hold two names); A {row['first_bar']} -> before the split, "
        f"{p['A']['candidate']['bars']} bars, names per bar {row['names_in_A']}; B -> {row['last_bar']}, "
        f"{p['B']['candidate']['bars']} bars, fewest names {row['fewest_names_in_B']}"
    )
    print("stream             | all: Sharpe  NW t | A: Sharpe  NW t | B: Sharpe  NW t | share of net: A     B")
    for name in STREAMS:
        cells = []
        for period in PERIODS:
            s = p[period][name]
            if s["all_zero"]:
                cells.append(f"{'always 0':>15}")
            else:
                cells.append(f"{_fmt(s['sharpe'], '+.2f'):>9} {_fmt(s['t_stat'], '+.2f'):>5}")
        shares = ""
        if name in SHARED:
            shares = f"{_fmt(p['A'][name]['share_of_total'], '+.0%'):>6} {_fmt(p['B'][name]['share_of_total'], '+.0%'):>5}"
        print(f"{name:<18} | {cells[0]:>15} | {cells[1]:>15} | {cells[2]:>15} | {shares:>18}")
    print(
        "(the parts are #199's streams: each with its own funding and the share of the candidate's trading cost its trades "
        "asked for; the constant holdings are on the candidate's bars, flat where they did not trade)"
    )
    print(
        "per year, fraction of equity, net: timing A {ta}, B {tb}; candidate A {ca}, B {cb}; timing t if spread evenly "
        "A {ea}, B {eb}".format(
            ta=_fmt(p["A"]["direction_timing"]["per_year"], "+.2%"),
            tb=_fmt(p["B"]["direction_timing"]["per_year"], "+.2%"),
            ca=_fmt(p["A"]["candidate"]["per_year"], "+.2%"),
            cb=_fmt(p["B"]["candidate"]["per_year"], "+.2%"),
            ea=_fmt(p["A"].get("timing_t_if_even"), "+.2f"),
            eb=_fmt(p["B"].get("timing_t_if_even"), "+.2f"),
        )
    )
    print(
        "exposure: mean net A {ma}, B {mb}; corr(net, basket's past 168 bars) A {ra}, B {rb}".format(
            ma=_fmt(p["A"]["exposure"]["mean_net"]),
            mb=_fmt(p["B"]["exposure"]["mean_net"]),
            ra=_fmt(p["A"]["exposure"]["corr_net_with_trailing_basket_return"]),
            rb=_fmt(p["B"]["exposure"]["corr_net_with_trailing_basket_return"]),
        )
    )
    s = row["selection_in_B"]
    if s is not None:
        left = s["beyond_direction"]
        print(
            f"selection in B only: NW t {_fmt(s['t_stat'], '+.2f')}; beyond direction (betas fitted on B) "
            f"{_fmt(left['per_year'], '+.2%')} a year, t {_fmt(left['t_stat'], '+.2f')}"
        )
    print(f"verdict: {row['verdict']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", required=True, help="the data root, e.g. <main checkout>/.beidou/data")
    parser.add_argument("--registry", default=str(ROOT / "config" / "alpha_registry.yaml"))
    parser.add_argument("--json", default=None, help="optional path for the readings; keep it outside reports/")
    args = parser.parse_args()

    for name in ("BEIDOU_TRIALS_LEDGER", "BEIDOU_FEATURE_STORE"):
        if os.environ.get(name):
            raise SystemExit(f"{name} is set; unset it so nothing is read from or written to another place")
    if args.json and "reports" in Path(args.json).resolve().parts:
        raise SystemExit("--json must not point under reports/: this look spends no ledger and leaves no report")

    from beidou_cli.research_panel import _entry, _load

    pinned = load_pinned()
    source = pinned.archived()
    report, rows = source["report"], source["rows"]
    run = report["run"]
    symbols = [str(s) for s in report["symbols"]]
    history = int(run["min_history_bars"])
    portfolio = PortfolioParams.from_mapping(run["portfolio"])
    cost = pinned.cost_of(report)
    panel = _load(
        args.root, symbols, str(run["interval"]), str(run["start"]), pinned.END, bool(run["funding"]), metrics=True
    )
    bpy = panel.bars_per_year

    checks = {
        "symbols": sorted(panel.symbols) == sorted(symbols),
        "first_bar": str(panel.index[0]) == str(report["range"][0]),
        "last_bar": str(panel.index[-1]) == str(report["range"][1]),
        "universe_mode": str(report["universe_mode"]) == "static",
    }
    print(f"pinned: {PINNED.name} sha256 {PINNED_SHA256[:12]}")
    print(f"panel: {len(panel.symbols)} symbols x {len(panel.index)} bars, {panel.index[0]} -> {panel.index[-1]}")
    print(f"against the report: {json.dumps(checks)}")
    ratio = panel.metric("count_long_short_ratio")
    if ratio is None:
        raise SystemExit("the panel carries no count_long_short_ratio; is the metrics store under --root?")
    first_ratio = ratio.apply(lambda column: column.first_valid_index())
    print("first long/short bucket per symbol: " + ", ".join(f"{s} {str(t)[:10]}" for s, t in first_ratio.items()))

    baseline = _entry(str(report["baseline"]["strategy"]), args.registry, json.dumps(report["baseline"]["params"]))
    base_model = AlphaModel(entries=(baseline,), portfolio=portfolio, interval=panel.interval, min_history_bars=history)
    base_weights, _c, _p = base_model.evaluate(panel, None)
    base = run_backtest(panel, base_weights, cost).portfolio_net
    base_sharpe = sharpe(base, bpy)
    base_delta = _delta(base_sharpe, float(report["baseline"]["sharpe"])) if base_sharpe is not None else None
    base_ok = base_delta is not None and abs(base_delta) <= pinned.REPRODUCTION_TOLERANCE
    print(f"baseline {baseline.id}: Sharpe {_fmt(base_sharpe, '+.4f')} (report {report['baseline']['sharpe']:+.4f}, Δ {_fmt(base_delta)})")

    sigma = asset_vol(panel.close, portfolio, bpy)
    inverse = (1.0 / sigma).where(panel.close.notna())
    whole = inverse.div(inverse.sum(axis=1), axis=0)
    basket_bar = (whole.shift(1) * (panel.close / panel.open - 1.0)).sum(axis=1, min_count=1)
    basket_return = basket_bar.rolling(pinned.TRAILING_BASKET_BARS, min_periods=pinned.TRAILING_BASKET_BARS).sum()

    results = []
    for hash_, window, scale in pinned.SHAPES:
        candidate = pinned.shape(hash_, window, scale, rows)
        ruled = pinned.look_at(candidate, panel, portfolio, history, cost, base, sigma, basket_return)
        built = streams(pinned, candidate, panel, portfolio, history, cost, sigma)
        row: dict[str, Any] = {"hash": candidate.hash, "expression": str(candidate.expr)}
        row.update(split_look(pinned, built, basket_return, bpy))
        vs_look_at = against_look_at(ruled, row["periods"]["all"])
        vs_09_27 = against_09_27(hash_, row["periods"]["all"])
        vs_report = bool(pinned.reproduced(ruled, rows[hash_])["ok"]) and base_ok and all(checks.values())
        row["reproduction"] = {
            "vs_look_at": vs_look_at,
            "vs_09_27": vs_09_27,
            "vs_report": vs_report,
            "ok": vs_look_at <= EXACT and vs_09_27["ok"] and vs_report,
        }
        row["verdict_category"], row["verdict"] = decide(row)
        print_row(row)
        results.append(row)

    governing = next(row for row in results if row["hash"] == pinned.GOVERNING)
    flagged = [row["hash"] for row in results if not row["reproduction"]["ok"]]
    agree = len({row["verdict_category"] for row in results}) == 1
    print()
    for row in results:
        print(f"{row['hash']}  {row['verdict']}{'（未复现）' if not row['reproduction']['ok'] else ''}")
    leaf = governing["verdict"] + ("（未复现）" if flagged else "")
    print(f"timing lead: {leaf}  (governed by {pinned.GOVERNING}; the four {'agree' if agree else 'do not agree'})")

    if args.json:
        payload = {
            "pinned": {"file": PINNED.name, "sha256": PINNED_SHA256},
            "report": pinned.REPORT.name,
            "end": pinned.END,
            "checks": checks,
            "baseline": {"sharpe": base_sharpe, "delta": base_delta},
            "shapes": results,
            "leaf": leaf,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
