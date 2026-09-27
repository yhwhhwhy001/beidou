"""零 ledger：LS（多空比）叶那 4 个边际为正的形状，是不是只在赌方向。

起因。2026-09-10 那轮 mine（`reports/research/mine-shortlist-20260909T172541Z.json`）第一次给 LS 叶的 36 个形状
打分：4 个边际为正，全是 `squash(-lsr(w), s)` 水平臂；4 个 `cs_rank` 横截面臂全为负。水平臂不做横截面中性，
可以带净方向敞口。`governance/reopen.yaml` 的 `ls-leaf` 把「先答那 4 个是不是在赌方向」写成重开的前提，形状取
basis 叶预登记的 falsifier B：水平臂对照恒定持仓，不是 baseline 书。操作者 2026-09-27T18:40:46Z 在「挖掘更多
策略和因子」话题的卡片上选了「答多空比」：零 ledger 先答这一问。

**零 ledger。** 不写 `trials.jsonl`、不写 `reports/`，也不做选择：四个形状 09-09 已按 DL-K2 计入 ledger（那轮
658 行），这里只拆它们已经跑出来的仓位，不在形状之间挑，不试新写法，不换窗口。

**复现先于解读。** 设置全部从那份报告读，不从今天的配置读：报告里的 18 个标的；2021-01-01 到 2026-09-08 16:00
（最后一根的开盘时刻，所以 `end` 取 17:00，不含）；1h；static universe；资金费开；成本取报告的 `costs`（7bps，
资金费照收）；组合参数取报告的 `run.portfolio`（vol_target 0.30，不是今天的 0.175）；`min_history_bars` 取
`run.min_history_bars`；基准 tsmom 的参数取报告的 `baseline.params`；执行口径 open_to_close。先复算基准与四个
形状的 Sharpe 与边际，与报告逐个比，差超过 0.05 的标「未复现」。

**边际与 mine 同一个式子**（`beidou_cli/research_mine_cmd.py`）：sharpe(基准与候选各半的收益) − sharpe(基准)，
两条流按并集对齐、缺的补 0。它只用在两处：复现，和「今天还为正吗」这个前提。**方向这一问不经 tsmom**：
`reopen.yaml` 写的形状就是「对照恒定持仓，不是 baseline 书」。各部分对 tsmom 的边际照样打印，不进判定。

**读数，看之前定死：**

1. 候选本身（复现）。
2. 恒定持仓，falsifier B 的对照物。候选能交易的每个币上恒定 +1 或恒定 −1，从候选第一次下单起，过同一套构造
   （按波动率配仓、定到 30% 年化、同样的上限与 band）、同样的成本与资金费。这是 D-024 `constant_long` 的写法
   （`beidou_alpha/validation/decompose.py`），只把范围限在候选自己能交易的币上，因为 LS 的数据比 K 线晚开始。
   两个方向都跑，取 Sharpe 高的那个作对照：对候选更紧的一边。比的是候选那一段 bar 上的 **Sharpe**，正面比。
   **为什么不比边际**：合成数据试跑时抓到的。边际对仓位大小敏感：一个纯恒定持仓的世界里，候选与恒定空头的
   Sharpe 都是 2.66，候选实际波动大一成，边际就高出 0.06，四个形状全被 falsifier B 放了过去。Sharpe 不随仓位
   大小变，而且预登记本来就说对照物是恒定持仓，不是 baseline 书。
3. 候选自己的仓位拆成三块，W = C + T + S。n = ΣW 是净敞口；b 是候选当时能交易的币上、按构造自己的 `asset_vol`
   取倒数再归一的篮子（和为 1）。D = n·b 是候选实际带着的净方向，再拆成恒定部分 C = n̄·b（n̄ 是候选开始交易
   以后 n 的均值）与择时部分 T = (n − n̄)·b；S = W − D 净敞口恒为 0，只剩选币。三块都不再过构造，所以一分没有
   被放大。毛收益与资金费对仓位是线性的，直接相加等于候选；手续费不是（|ΔW| ≤ |ΔD| + |ΔS|），所以候选真实付的
   手续费先按每格 |ΔD| : |ΔS| 分给 D 与 S，D 那份再按 |ΔC| : |ΔT| 分给 C 与 T。每一层都守恒：D + S 的净收益
   逐 bar 等于候选，C + T 的等于 D，脚本核这两条。
   **为什么不让各块自己付自己的手续费**：合成数据试跑时抓到的。D 的篮子比例随波动率每根 bar 都在变，而候选的
   仓位过了 band，单拆出来的 D 没有，于是 D 多付了候选从没付过的手续费，一个纯择时的世界里四个形状有三个被判成
   「不只是方向」。分摊以后同一份合成数据四个都判对。
4. S 扣掉方向以后还剩多少。S 的逐 bar 净收益对两个方向因子回归，取截距：b 篮子本身的逐 bar 收益（净敞口为 1
   的方向），和 D 的逐 bar 毛收益（跟着 n 伸缩、跟着 n 翻面的方向）。S 的净敞口是 0，β 却不一定是 0：候选若
   在净多时偏重高 β 的币、净空时偏重低 β 的币——多空比的偏离在高 β 的币上更大，正会这样——S 就带着一份跟着 n
   翻面的方向，只看美元中性看不出来。第二个因子就是为它放的。β 在全样本上拟合，只会从 S 身上拿走东西，所以这一条
   偏向「只是方向」；证明选币有信息的责任本来就在选币这一边。合成数据里专门造了这样一个世界（见下）：排第一的形状，
   选币部分的 t 是 2.23，只看它就判成了「不只是方向」；扣掉方向以后是 0.78。

**判定，每个形状一次，按顺序，第一条成立就停：**

- 今天复算的边际 ≤ 0：「今天不再为正」。前提没了，不往下判。
- 候选的 Sharpe ≤ 恒定持仓（两个方向里高的那个）在同一段 bar 上的 Sharpe：「只在赌方向：恒定持仓就够」。
  falsifier B 的原话：跑不赢即判为方向性押注。
- 选币部分 S（带自己的资金费与那份手续费）逐 bar 净收益的 Newey-West t < 2.0，或扣掉方向以后的 t < 2.0：
  只在赌方向。证明选币有信息的责任在选币这一边。子标签看择时部分 T（同样带自己的资金费与那份手续费）的 t：
  ≥ 2.0 是「只在赌方向：择时」；否则是「只在赌方向：恒定持仓就够」——候选比它自己的平均净敞口多出来的那点
  分不出噪声。括号里写明两个 t 与择时的 t。带宽用 `newey_west_tstat` 的默认，它的 docstring 说的正是逐 bar
  组合收益这种形状。t 这一条也是合成数据逼出来的：纯择时的世界里排第一的形状，选币只占毛收益 1%，边际比 D
  高 0.007，只比边际就判成了「不只是方向」。
- 否则：「不只是方向」。在它自己的净方向之上，选币部分还挣了钱，扣掉方向以后仍分得出噪声。结论后面带上选币
  部分占毛收益的比例和两个 t，因为「不只」说的是有没有，不是有多少。

叶的结论看排第一的 `8a838550a686852d`：09-10 那一节说的「这个候选」，任何人要拿去 validate 的就是它。另外三个
照报。任何一个标了「未复现」，叶的结论也标。

**合成数据试跑**（定判定线之前，不碰真实数据）：18 个同名标的、2025-01 到 2026-09 的假 K 线、资金费与多空比，
四个世界，每个的正确答案是造的时候定的：「择时」（全市场一条多空比，大盘跟着它走）、「不只是方向」（各币各自的
多空比，各自的漂移跟着它走，大盘不动）、「恒定持仓就够」（多空比一路上行，大盘一路下跌）、「β 倾斜」（全市场
一条多空比，但偏离按各币的 β 放大，大盘跟着它走——答案是「择时」）。四个世界的四个形状都要判对，这条判定线
才入库；2026-09-27 这一版 16 个全对。造数据的脚本是 `scratchpad/ls_direction_look_synthetic.py`，读数在那里的
docstring 里。合成数据的「未复现」是对的：它的日期范围本来就不是报告的。

**结论之后跟着什么，也先写下：**

- 「只在赌方向」（两种都算）：LS 叶作为选币信号收口，`reopen.yaml` 的 `ls-leaf` 改判 REFUTED。用全市场多空比
  做择时是另一条假设，要自己的预登记、自己的对照（大盘，不是 tsmom）、照付 ledger。
- 「不只是方向」：重开条件的前一半答了；后一半（2026-10-03 的窗口）照旧，validate 要花 ledger、要先问操作者。
  水平问题不因此消失：最好的那个全样本 Sharpe 1.376，mined 桶的门约 1.78。
- 「今天不再为正」或「未复现」：只报读数，不改 `reopen.yaml`，先查为什么对不上。

**预期**（按惯例写下来，事后不能改口）：「只在赌方向：择时」。账户多空比对自己均值的偏离在各币之间同涨同跌，
散户是一起抄底、一起止盈的，所以 −lsr 主要在动整本书的净敞口；而 −lsr 在时间上大致围着 0 转，恒定持仓应该
解释不了。

用法（在 `~/beidou` 之外的 worktree 里，读主 checkout 的数据；环境里不能有 `BEIDOU_TRIALS_LEDGER` 与
`BEIDOU_FEATURE_STORE`）：

    PYTHONPATH=$PWD /Users/maguannan/beidou/.venv/bin/python scratchpad/ls_direction_look.py \\
        --root /Users/maguannan/beidou/.beidou/data --json /tmp/ls_direction_look.json
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

from beidou_alpha.backtest import BacktestResult, CostModel, run_backtest
from beidou_alpha.mining.expr import Const, LongShortRatio, Mul, Squash
from beidou_alpha.mining.search import Candidate, to_signal
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import Panel
from beidou_alpha.portfolio import PortfolioParams, asset_vol, build_weights
from beidou_alpha.registry import StrategyEntry
from beidou_alpha.signals import register as register_signal
from beidou_alpha.validation.metrics import max_drawdown, newey_west_tstat, sharpe

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "research" / "mine-shortlist-20260909T172541Z.json"
# The four with a positive marginal, in the report's order.  Built here and checked against the report's hash
# and expression, so a canonicalisation change cannot swap a shape under the same name.
SHAPES: tuple[tuple[str, int, float], ...] = (
    ("8a838550a686852d", 168, 1.0),
    ("49742e605be41b26", 168, 0.5),
    ("2ca33804038a5a52", 72, 0.5),
    ("8a87c4aea11c7a7a", 72, 1.0),
)
GOVERNING = "8a838550a686852d"
END = "2026-09-08 17:00"  # exclusive on open_time: the report's last bar opened at 16:00
REPRODUCTION_TOLERANCE = 0.05
SIGNIFICANT_T = 2.0  # Newey-West t a part must reach before it is said to carry anything but noise
TRAILING_BASKET_BARS = 168  # descriptive only: how the net exposure lines up with the basket's past week

# The candidate's positions and the pieces they are split into.  D = C + T and W = D + S.
PARTS = ("direction", "selection", "direction_constant", "direction_timing")
HELD = ("constant_long", "constant_short")

NOT_POSITIVE = "今天不再为正"
CONSTANT_ENOUGH = "只在赌方向：恒定持仓就够"
TIMING = "只在赌方向：择时"
MORE_THAN_DIRECTION = "不只是方向"


# --- settings, read from the archived report --------------------------------------------------------------


def archived() -> dict[str, Any]:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    rows = {row["hash"]: row for row in report["candidates"]}
    missing = [h for h, _, _ in SHAPES if h not in rows]
    if missing:
        raise SystemExit(f"{REPORT.name} has no row for {missing}; this is not the report the look was written against")
    return {"report": report, "rows": rows}


def shape(hash_: str, window: int, scale: float, rows: Mapping[str, Mapping[str, Any]]) -> Candidate:
    candidate = Candidate.of(Squash(Mul(Const(-1.0), LongShortRatio(window)), scale))
    if candidate.hash != hash_ or str(candidate.expr) != rows[hash_]["expression"]:
        raise SystemExit(
            f"built {candidate.expr} -> {candidate.hash}, the report says {rows[hash_]['expression']} -> {hash_}"
        )
    return candidate


def cost_of(report: Mapping[str, Any]) -> CostModel:
    block = report["costs"]
    if float(block["carry_bps_per_bar"]) != 0.0:
        raise SystemExit("the report charged carry; `shared_cost_nets` shares turnover cost only and would misstate it")
    return CostModel(
        turnover_bps=float(block["turnover_bps"]),
        carry_bps_per_bar=float(block["carry_bps_per_bar"]),
        use_funding=bool(block["use_funding"]),
    )


# --- the readings ----------------------------------------------------------------------------------------


def marginal(base: pd.Series, net: pd.Series, bars_per_year: float) -> dict[str, float | None]:
    """The miner's own column, byte for byte: union alignment, a missing bar is a flat bar."""
    frame = pd.DataFrame({"baseline": base, "candidate": net}).dropna(how="all").fillna(0.0)
    combined = sharpe(frame.mean(axis=1), bars_per_year)
    alone = sharpe(frame["baseline"], bars_per_year)
    correlation = frame["baseline"].corr(frame["candidate"])
    base_vol = float(frame["baseline"].std())
    return {
        "marginal": None if combined is None or alone is None else combined - alone,
        "correlation": None if pd.isna(correlation) else float(correlation),
        "vol_ratio": float(frame["candidate"].std()) / base_vol if base_vol > 0 else None,
    }


def stats(net: pd.Series, base: pd.Series, bars_per_year: float, turnover: float) -> dict[str, Any]:
    return {
        "sharpe": sharpe(net, bars_per_year),
        "t_stat": newey_west_tstat(net)["t_stat"],
        "max_drawdown": max_drawdown(net),
        "turnover": turnover,
        "first_bar": str(net.index[0]),
        "bars": len(net),
        **marginal(base, net, bars_per_year),
    }


def reading(result: BacktestResult, base: pd.Series, bars_per_year: float) -> dict[str, Any]:
    return stats(result.portfolio_net, base, bars_per_year, float(result.turnover.sum()))


def started_rows(weights: pd.DataFrame) -> pd.Series:
    return weights.notna().any(axis=1).cummax()


def on_started(frame: pd.DataFrame, started: pd.Series) -> pd.DataFrame:
    """NaN before the candidate's first decision, so every part starts on the candidate's own first bar."""
    mask = np.repeat(started.to_numpy()[:, None], frame.shape[1], axis=1)
    return frame.where(pd.DataFrame(mask, index=frame.index, columns=frame.columns))


def basket(tradable: pd.DataFrame, sigma: pd.DataFrame) -> pd.DataFrame:
    """Inverse-vol proportions over the names the candidate could hold at each bar, rows summing to 1 (or 0)."""
    inverse = (1.0 / sigma).where(tradable & sigma.notna() & (sigma > 0))
    total = inverse.sum(axis=1)
    return inverse.div(total.where(total > 0), axis=0).fillna(0.0)


def moves(executed: pd.DataFrame) -> pd.DataFrame:
    """|Δw| per cell exactly as `run_backtest` charges it, the first executed row counting in full."""
    delta = executed.diff()
    delta.iloc[0] = executed.iloc[0]
    return delta.abs()


def split_cost(paid: pd.DataFrame, first: pd.DataFrame, second: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``paid`` per cell, shared in proportion to the trade each part asked for; the second takes the rest, so it is exact.

    Nothing is lost where neither part asked for a trade: |Δ(a + b)| <= |Δa| + |Δb|, so the whole paid nothing there.
    """
    asked = first + second
    mine = paid * (first / asked.where(asked > 0)).fillna(0.0)
    return mine, paid - mine


def shared_cost_nets(
    parts: Mapping[str, BacktestResult], funding_only: Mapping[str, BacktestResult], cost: CostModel
) -> tuple[dict[str, pd.Series], dict[str, float]]:
    """D, S, C and T, each net of its own funding and of its share of the trading cost the CANDIDATE paid.

    Gross and funding are linear in the weights and split exactly.  Trading cost is not, since |ΔW| can be
    less than |ΔD| + |ΔS|, so the candidate's real cost in each cell goes to D and S in proportion to the
    trade each asked for, and D's share on to C and T the same way.  D + S then adds up to the candidate and
    C + T to D on every bar, which is checked.
    """
    move = {name: moves(parts[name].weights) for name in ("candidate", *PARTS)}
    paid = move["candidate"] * (cost.turnover_bps / 10_000.0)
    trading: dict[str, pd.DataFrame] = {}
    trading["direction"], trading["selection"] = split_cost(paid, move["direction"], move["selection"])
    trading["direction_constant"], trading["direction_timing"] = split_cost(
        trading["direction"], move["direction_constant"], move["direction_timing"]
    )
    nets = {name: (parts[name].gross - funding_only[name].costs - trading[name]).sum(axis=1) for name in PARTS}
    for whole, pieces, total in (
        ("candidate", ("direction", "selection"), parts["candidate"].portfolio_net),
        ("direction", ("direction_constant", "direction_timing"), nets["direction"]),
    ):
        gap = float((nets[pieces[0]] + nets[pieces[1]] - total).abs().max())
        if gap > 1e-9:
            raise SystemExit(f"the shared-cost parts do not add up to {whole} (gap {gap:.3g})")
    asked = {name: float(move[name].to_numpy().sum()) for name in PARTS}
    return nets, asked


def beyond_direction(net: pd.Series, factors: Mapping[str, pd.Series], bars_per_year: float) -> dict[str, Any]:
    """What is left of a zero-net stream once it is regressed on the direction factors, and that remainder's t.

    The series tested is ``net - sum(beta_k * factor_k)``, whose mean is the regression's intercept.  The betas
    are fitted in-sample, which can only take from the stream, never give to it.
    """
    frame = pd.concat({"net": net, **factors}, axis=1).dropna()
    names = list(factors)
    design = np.column_stack([np.ones(len(frame)), *(frame[name].to_numpy() for name in names)])
    coef, *_ = np.linalg.lstsq(design, frame["net"].to_numpy(), rcond=None)
    left = frame["net"].to_numpy() - design[:, 1:] @ coef[1:]
    return {
        "t_stat": newey_west_tstat(left)["t_stat"],
        "per_year": float(coef[0]) * bars_per_year,
        "betas": {name: float(coef[i + 1]) for i, name in enumerate(names)},
    }


def attribution(
    parts: Mapping[str, BacktestResult], funding_only: Mapping[str, BacktestResult], bars_per_year: float
) -> dict[str, float | None]:
    """Annualised mean contribution of each piece, as a fraction of equity per year."""

    def per_year(series: pd.Series) -> float:
        return float(series.mean() * bars_per_year)

    gross_total = per_year(parts["candidate"].portfolio_gross)
    gross_selection = per_year(parts["selection"].portfolio_gross)
    funding_candidate = funding_only["candidate"].costs.sum(axis=1)
    return {
        "gross_total": gross_total,
        "gross_direction_constant": per_year(parts["direction_constant"].portfolio_gross),
        "gross_direction_timing": per_year(parts["direction_timing"].portfolio_gross),
        "gross_selection": gross_selection,
        "selection_share_of_gross": gross_selection / gross_total if gross_total > 0 else None,
        "funding_direction": -per_year(funding_only["direction"].costs.sum(axis=1)),
        "funding_selection": -per_year(funding_only["selection"].costs.sum(axis=1)),
        "funding_total": -per_year(funding_candidate),
        "trading_cost_total": -per_year(parts["candidate"].costs.sum(axis=1) - funding_candidate),
        "net_total": per_year(parts["candidate"].portfolio_net),
    }


def exposure_profile(weights: pd.DataFrame, started: pd.Series, basket_return: pd.Series) -> dict[str, Any]:
    held = weights.fillna(0.0)[started]
    net = held.sum(axis=1)
    gross = held.abs().sum(axis=1)
    active = gross > 0
    one_sided = ((held[active] >= 0).all(axis=1) | (held[active] <= 0).all(axis=1)).mean() if active.any() else None
    joined = pd.concat([net, basket_return.reindex(held.index)], axis=1).dropna()
    return {
        "mean_net": float(net.mean()),
        "mean_abs_net": float(net.abs().mean()),
        "mean_gross": float(gross.mean()),
        "net_over_gross": float((net.abs()[active] / gross[active]).mean()) if active.any() else None,
        "share_bars_all_one_side": None if one_sided is None else float(one_sided),
        "share_bars_net_long": float((net > 0).mean()),
        "share_bars_net_short": float((net < 0).mean()),
        "corr_net_with_trailing_basket_return": (
            float(joined.iloc[:, 0].corr(joined.iloc[:, 1])) if len(joined) > 30 else None
        ),
    }


def _significant(value: float | None) -> bool:
    return value is not None and value >= SIGNIFICANT_T


def decide(row: Mapping[str, Any]) -> tuple[str, str]:
    """(category, the line that is printed).  The category is what the four are compared on."""
    cand = row["candidate"]["marginal"]
    if cand is None or cand <= 0:
        return NOT_POSITIVE, NOT_POSITIVE
    own = row["candidate"]["sharpe"]
    held = [row["falsifier_b"][name] for name in HELD if row["falsifier_b"][name] is not None]
    if own is not None and held and own <= max(held):
        return CONSTANT_ENOUGH, f"{CONSTANT_ENOUGH}（跑不赢恒定持仓：Sharpe {own:+.2f} ≤ {max(held):+.2f}）"
    t = row["t"]
    if not (_significant(t["selection"]) and _significant(t["selection_beyond_direction"])):
        why = (
            f"选币部分 t {_fmt(t['selection'], '.2f')}，扣掉方向后 {_fmt(t['selection_beyond_direction'], '.2f')}，"
            f"要都 ≥ {SIGNIFICANT_T}；择时部分 t {_fmt(t['timing'], '.2f')}"
        )
        return (TIMING, f"{TIMING}（{why}）") if _significant(t["timing"]) else (CONSTANT_ENOUGH, f"{CONSTANT_ENOUGH}（{why}）")
    share = row["attribution_per_year"]["selection_share_of_gross"]
    return MORE_THAN_DIRECTION, (
        f"{MORE_THAN_DIRECTION}（选币占毛收益 {_fmt(share, '.0%')}，t {t['selection']:.2f}，"
        f"扣掉方向后 {t['selection_beyond_direction']:.2f}）"
    )


def reproduced(row: Mapping[str, Any], archived_row: Mapping[str, Any]) -> dict[str, Any]:
    deltas = {
        "sharpe": _delta(row["candidate"]["sharpe"], archived_row.get("sharpe")),
        "marginal": _delta(row["candidate"]["marginal"], archived_row.get("baseline_marginal_sharpe")),
    }
    ok = all(value is not None and abs(value) <= REPRODUCTION_TOLERANCE for value in deltas.values())
    return {"deltas": deltas, "ok": ok}


def _delta(now: float | None, then: float | None) -> float | None:
    return None if now is None or then is None else float(now) - float(then)


# --- one shape -------------------------------------------------------------------------------------------


def look_at(
    candidate: Candidate,
    panel: Panel,
    portfolio: PortfolioParams,
    history: int,
    cost: CostModel,
    base: pd.Series,
    sigma: pd.DataFrame,
    basket_return: pd.Series,
) -> dict[str, Any]:
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
    started = started_rows(own)

    # 2. the constant holdings: D-024's `constant_long`, on the names the candidate could hold, both signs
    constant = pd.DataFrame(np.where(tradable.to_numpy(), 1.0, np.nan), index=panel.index, columns=panel.symbols)
    held = {
        "constant_long": run_backtest(panel, build_weights(constant, panel.close, bpy, portfolio), cost),
        "constant_short": run_backtest(panel, build_weights(-constant, panel.close, bpy, portfolio), cost),
    }

    # 3. the candidate's own positions, split; no construction, so nothing is rescaled
    filled = own.fillna(0.0)
    net = filled.sum(axis=1)
    proportions = basket(tradable, sigma.reindex(index=panel.index, columns=panel.symbols))
    net_mean = float(net[started].mean())
    direction = proportions.mul(net, axis=0)
    parts_weights = {
        "candidate": own,
        "direction": on_started(direction, started),
        "selection": on_started(filled - direction, started),
        "direction_constant": on_started(proportions * net_mean, started),
        "direction_timing": on_started(proportions.mul(net - net_mean, axis=0), started),
        "basket": on_started(proportions, started),  # the direction at a net of exactly 1, a factor for step 4
    }
    parts = {name: run_backtest(panel, frame, cost) for name, frame in parts_weights.items()}
    for whole, pieces in (("candidate", ("direction", "selection")), ("direction", ("direction_constant", "direction_timing"))):
        gap = float((sum(parts[p].gross for p in pieces) - parts[whole].gross).abs().to_numpy().max())
        if gap > 1e-9:
            raise SystemExit(f"{candidate.hash}: {' + '.join(pieces)} does not add up to {whole} (gross gap {gap:.3g})")
    funding_only_cost = CostModel(turnover_bps=0.0, carry_bps_per_bar=0.0, use_funding=cost.use_funding)
    funding_only = {name: run_backtest(panel, parts_weights[name], funding_only_cost) for name in ("candidate", *PARTS)}
    nets, asked = shared_cost_nets(parts, funding_only, cost)

    # 4. the selection part once the direction factors have taken what they can explain
    factors = {"basket": parts["basket"].portfolio_gross, "direction": parts["direction"].portfolio_gross}
    left = beyond_direction(nets["selection"], factors, bpy)

    own_net = parts["candidate"].portfolio_net
    row: dict[str, Any] = {
        "hash": candidate.hash,
        "expression": str(candidate.expr),
        "candidate": reading(parts["candidate"], base, bpy),
        **{name: reading(result, base, bpy) for name, result in held.items()},
        # falsifier B: head to head on the candidate's own bars, not through the baseline
        "falsifier_b": {
            name: sharpe(result.portfolio_net.reindex(own_net.index).fillna(0.0), bpy) for name, result in held.items()
        },
        **{name: stats(nets[name], base, bpy, asked[name]) for name in PARTS},
        "selection_beyond_direction": left,
        "t": {
            "selection": newey_west_tstat(nets["selection"])["t_stat"],
            "selection_beyond_direction": left["t_stat"],
            "timing": newey_west_tstat(nets["direction_timing"])["t_stat"],
        },
        "attribution_per_year": attribution(parts, funding_only, bpy),
        "exposure": exposure_profile(own, started, basket_return),
        "net_mean": net_mean,
    }
    row["verdict_category"], row["verdict"] = decide(row)
    return row


# --- output ----------------------------------------------------------------------------------------------


def _fmt(value: Any, spec: str = "+.3f") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return format(value, spec)


def print_row(row: Mapping[str, Any]) -> None:
    print(f"\n== {row['hash']}  {row['expression']}")
    repro = row["reproduction"]
    print(
        f"reproduction: Sharpe Δ {_fmt(repro['deltas']['sharpe'])}, marginal Δ {_fmt(repro['deltas']['marginal'])} "
        f"-> {'ok' if repro['ok'] else '未复现'} (tolerance {REPRODUCTION_TOLERANCE})"
    )
    print("stream             | Sharpe | NW t   | marginal | corr w/ base | vol / base | MDD     | turnover | first bar")
    for name in ("candidate", *HELD, *PARTS):
        r = row[name]
        print(
            f"{name:<18} | {_fmt(r['sharpe'], '+.2f'):>6} | {_fmt(r['t_stat'], '+.2f'):>6} | {_fmt(r['marginal']):>8} | "
            f"{_fmt(r['correlation']):>12} | {_fmt(r['vol_ratio'], '.2f'):>10} | {_fmt(r['max_drawdown'], '+.3f'):>7} | "
            f"{r['turnover']:>8.0f} | {r['first_bar']}"
        )
    print(
        "(the four parts carry their own funding and the share of the candidate's trading cost their trades asked for; "
        "their turnover is what they asked for, not what was paid; marginals against the baseline are shown, not judged)"
    )
    b = row["falsifier_b"]
    print(
        f"falsifier B, on the candidate's bars: candidate Sharpe {_fmt(row['candidate']['sharpe'], '+.3f')} vs constant "
        f"long {_fmt(b['constant_long'], '+.3f')}, constant short {_fmt(b['constant_short'], '+.3f')}"
    )
    left = row["selection_beyond_direction"]
    print(
        f"selection beyond direction: {_fmt(left['per_year'], '+.2%')} a year, Newey-West t {_fmt(left['t_stat'], '+.2f')} "
        f"(betas: basket {_fmt(left['betas']['basket'], '+.4f')}, direction {_fmt(left['betas']['direction'], '+.4f')})"
    )
    a = row["attribution_per_year"]
    print(
        "per year, fraction of equity: gross {gt} = direction constant {dc} + direction timing {dt} + selection {gs} "
        "(selection share {ss}); funding {ft} (direction {fd}, selection {fs}); trading cost {tc}; net {nt}".format(
            gt=_fmt(a["gross_total"], "+.2%"),
            dc=_fmt(a["gross_direction_constant"], "+.2%"),
            dt=_fmt(a["gross_direction_timing"], "+.2%"),
            gs=_fmt(a["gross_selection"], "+.2%"),
            ss=_fmt(a["selection_share_of_gross"], ".0%"),
            ft=_fmt(a["funding_total"], "+.2%"),
            fd=_fmt(a["funding_direction"], "+.2%"),
            fs=_fmt(a["funding_selection"], "+.2%"),
            tc=_fmt(a["trading_cost_total"], "+.2%"),
            nt=_fmt(a["net_total"], "+.2%"),
        )
    )
    e = row["exposure"]
    print(
        f"exposure: mean net {_fmt(e['mean_net'])}, mean |net| {_fmt(e['mean_abs_net'], '.3f')}, mean gross "
        f"{_fmt(e['mean_gross'], '.3f')}, |net|/gross {_fmt(e['net_over_gross'], '.3f')}, all one side "
        f"{_fmt(e['share_bars_all_one_side'], '.1%')}, net long {_fmt(e['share_bars_net_long'], '.1%')}, net short "
        f"{_fmt(e['share_bars_net_short'], '.1%')}, corr(net, basket's past {TRAILING_BASKET_BARS} bars) "
        f"{_fmt(e['corr_net_with_trailing_basket_return'])}"
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

    source = archived()
    report, rows = source["report"], source["rows"]
    run = report["run"]
    symbols = [str(s) for s in report["symbols"]]
    history = int(run["min_history_bars"])
    portfolio = PortfolioParams.from_mapping(run["portfolio"])
    cost = cost_of(report)
    panel = _load(args.root, symbols, str(run["interval"]), str(run["start"]), END, bool(run["funding"]), metrics=True)
    bpy = panel.bars_per_year

    checks = {
        "symbols": sorted(panel.symbols) == sorted(symbols),
        "first_bar": str(panel.index[0]) == str(report["range"][0]),
        "last_bar": str(panel.index[-1]) == str(report["range"][1]),
        "universe_mode": str(report["universe_mode"]) == "static",
    }
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
    base_delta = _delta(base_sharpe, report["baseline"].get("sharpe"))
    base_ok = base_delta is not None and abs(base_delta) <= REPRODUCTION_TOLERANCE
    print(f"baseline {baseline.id}: Sharpe {_fmt(base_sharpe, '+.4f')} (report {report['baseline']['sharpe']:+.4f}, Δ {_fmt(base_delta)})")

    sigma = asset_vol(panel.close, portfolio, bpy)
    inverse = (1.0 / sigma).where(panel.close.notna())
    whole = inverse.div(inverse.sum(axis=1), axis=0)
    basket_bar = (whole.shift(1) * (panel.close / panel.open - 1.0)).sum(axis=1, min_count=1)
    basket_return = basket_bar.rolling(TRAILING_BASKET_BARS, min_periods=TRAILING_BASKET_BARS).sum()

    results = []
    for hash_, window, scale in SHAPES:
        row = look_at(shape(hash_, window, scale, rows), panel, portfolio, history, cost, base, sigma, basket_return)
        row["reproduction"] = reproduced(row, rows[hash_])
        row["reproduction"]["ok"] = row["reproduction"]["ok"] and base_ok and all(checks.values())
        print_row(row)
        results.append(row)

    governing = next(row for row in results if row["hash"] == GOVERNING)
    flagged = [row["hash"] for row in results if not row["reproduction"]["ok"]]
    agree = len({row["verdict_category"] for row in results}) == 1
    leaf = governing["verdict"] + ("（未复现）" if flagged else "")
    print()
    for row in results:
        print(f"{row['hash']}  {row['verdict']}{'（未复现）' if not row['reproduction']['ok'] else ''}")
    print(f"LS leaf: {leaf}  (governed by {GOVERNING}; the four {'agree' if agree else 'do not agree'})")

    if args.json:
        payload = {
            "report": REPORT.name,
            "end": END,
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
