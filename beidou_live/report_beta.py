"""How much of the live return was the market's (D-045): the daily block and `report beta`.

Checklist item #6.9 (performance attribution) of
`docs/analysis/2026-09-23-external-prompt-checklist-vs-beidou.md`.  The computation is
`benchmark.beta_reading`; this module feeds it the archive's closes and renders what it returns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.report import render_markdown
from beidou_live.benchmark import beta_reading
from beidou_live.report_common import _fmt_num, _fmt_pct, _store_closes, json_dumps
from beidou_live.state import StateStore


def _beta_regression_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """One regression's readout.  The t printed is Newey-West, never OLS (see `benchmark` for why).

    The BANDWIDTH is on the page beside the t, which it was not until 2026-09-22.  `NW_LAGS` is 48
    and `MIN_BARS` is also 48, so a window that just cleared the minimum ran a Bartlett kernel as
    wide as its own sample and reported the result as if it were the same ruler the long window uses.
    A 55-bar window read alpha t = 5.49 that way against 2.23 on plain OLS.  Printing the constant's
    name while a different width was in force is the failure this repository keeps finding, one
    layer down from `giveback_since_hwm_in_usdt_pct`.
    """
    lags, wanted = block.get("nw_lags"), block.get("nw_lags_requested")
    covers = block.get("nw_covers_intended_horizon")
    clamped = covers is False
    return {
        "beta": _fmt_num(block.get("beta")),
        "beta_t (NW)": _fmt_num(block.get("beta_t")),
        "alpha": f"{_fmt_num(block.get('alpha_bps_per_hour'))} bps/h",
        "alpha_t (NW)": _fmt_num(block.get("alpha_t")),
        "NW 带宽": (
            f"{lags} bar"
            + (
                f"（被夹住：请求 {wanted}，样本不足其 4 倍，修正够不到它要修的两天自相关）"
                if clamped
                else "（= NW_LAGS，够得到两天自相关）"
            )
            if lags is not None
            else "n/a"
        ),
        "alpha annualised": (
            _fmt_pct(block.get("alpha_annualised"))
            if block.get("alpha_annualised") is not None
            else (
                "不年化（NW 带宽被夹住，标准误答的是更窄的问题）"
                if clamped
                else "不年化（|t| < 2，年化会把噪声放大三个数量级）"
            )
        ),
        "R^2": _fmt_num(block.get("r2")),
        "市场部分": _fmt_pct(block.get("market_part")),
        "残差部分": _fmt_pct(block.get("residual_part")),
    }


def market_beta(
    store: StateStore,
    *,
    closes: Callable[[str], pd.Series] | None = None,
    root: str | Path = ".beidou/data",
    interval: str = "1h",
) -> dict[str, Any]:
    """D-045 in the daily report: `report beta`'s reading, taken by the same `beta_reading`.

    Reported, never judged.  Nothing here pages: no threshold for the beta or the residual was
    registered before the book went live, and one picked after reading the number is not a threshold.

    The catch is broad for the reason `exit_counterfactuals` gives.  This runs inside the hourly check,
    and a reading that gates nothing must not take the rest of the report down with it.  The failure
    stays visible: the block carries its reason, and `report beta` on the same state raises it whole.
    """
    try:
        return beta_reading(
            store.read_jsonl(store.cycles_path),
            store.read_jsonl(store.attribution_path),
            closes or _store_closes(root, interval),
        )
    except Exception as exc:
        return {"measured": False, "reason": f"{type(exc).__name__}: {exc}"}


def _market_beta_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """The daily report's view of `market_beta`: one line per regression, and no verdict.

    Short on purpose.  The full page is `beidou report beta`, and its numbers are these numbers.  The
    all-long share comes before the regressions for the reason `beta_markdown` gives: over those bars
    no split of the return can credit the signal with anything.
    """
    decomposition = block.get("decomposition") or {}
    if not decomposition.get("measured"):
        return {"measured": "no", "reason": decomposition.get("reason") or block.get("reason") or "not computed"}
    window, basket, signal = block.get("window") or {}, block.get("benchmark") or {}, block.get("signal") or {}

    def regression(row: Mapping[str, Any]) -> str:
        return (
            f"beta {_fmt_num(row.get('beta'))}（t {_fmt_num(row.get('beta_t'))}）；"
            f"alpha {_fmt_num(row.get('alpha_bps_per_hour'))} bps/h（t {_fmt_num(row.get('alpha_t'))}）；"
            f"残差部分 {_fmt_pct(row.get('residual_part'))}"
            + (f"；年化 {_fmt_pct(row['alpha_annualised'])}" if row.get("alpha_annualised") is not None else "")
            + (f"；NW 带宽被夹到 {row.get('nw_lags')} bar" if row.get("nw_covers_intended_horizon") is False else "")
        )

    return {
        "窗口": f"{window.get('from')} → {window.get('to')}（{_fmt_num(window.get('days'))} 天）",
        "回归样本": f"{decomposition.get('bars')} 根 bar（剔除的 bar {decomposition.get('excluded_bars')}，D-032）",
        "收益 策略/PIT 基准": (
            f"{_fmt_pct(decomposition.get('strategy_return'))} / {_fmt_pct(decomposition.get('benchmark_return'))}"
        ),
        "净敞口 均值/峰值": (
            f"{_fmt_num(decomposition.get('exposure_mean'))}x / {_fmt_num(decomposition.get('exposure_max'))}x"
        ),
        "基准篮子": (
            f"每根 bar {_fmt_num(basket.get('symbols_per_bar'))} 个币；"
            f"因缺价跳过 {basket.get('prices_missing')} 个 symbol-bar"
        ),
        "全多头的 bar": (
            f"{signal.get('all_long_bars')}/{signal.get('bars')}（{_fmt_pct(signal.get('all_long_share'))}）"
            if signal.get("measured")
            else f"n/a（{signal.get('reason')}）"
        ),
        "constant beta": regression(decomposition.get("constant") or {}),
        "conditional（敞口 × 市场）": regression(decomposition.get("conditional") or {}),
        "读法": "只报告不告警：beta 与残差都没有预登记的阈值。t 按 Newey-West；完整一页见 `beidou report beta`",
    }


def beta_markdown(payload: dict[str, Any]) -> str:
    """`beidou report beta` (D-045): the market's share of the live book's return.

    The signal section comes BEFORE the regressions on purpose.  When every contribution is +1 the
    book is the constant-long comparator and no split of the return can attribute anything to the
    signal; a reader who meets the beta number first will have already formed a view by the time they
    reach that fact.
    """
    decomposition = payload.get("decomposition") or {}
    signal = payload.get("signal") or {}
    if not decomposition.get("measured"):
        sections: list[tuple[str, Any]] = [
            ("Window", payload.get("window") or {}),
            ("无法分解", {"reason": decomposition.get("reason", "unknown")}),
        ]
        return render_markdown("Live beta decomposition", sections)
    all_long = signal.get("all_long_share")
    return render_markdown(
        "Live beta decomposition (D-045)",
        [
            ("Window", payload.get("window") or {}),
            (
                # First, because it bounds what the rest can mean.
                "Signal state",
                {
                    "bars": signal.get("bars"),
                    "全多头的 bar": f"{signal.get('all_long_bars')} ({_fmt_pct(all_long)})",
                    # Renamed 2026-09-22: it counts (bar, symbol) pairs over the window, and read as
                    # a standing position count it produced the question "空头为什么冻在 394" - the
                    # answer being that a cumulative count stops growing, which is not the same event
                    # as a position being closed.  Both numbers now, each saying which it is.
                    "空头信号 bar·标的数（窗口累计）": signal.get("short_positions"),
                    "当前空头标的数（最后一根 bar）": signal.get("shorts_last_bar"),
                    "信号取值": json_dumps(signal.get("values") or {}),
                    "读法": (
                        "全多头的 bar 上这本书按定义等于 constant_long，那些 bar 里的收益不可能来自信号"
                        if (all_long or 0) > 0
                        else "信号在窗口内一直有多空区分"
                    ),
                }
                if signal.get("measured")
                else {"measured": "no", "reason": signal.get("reason")},
            ),
            (
                "Returns",
                {
                    "策略（USDT 权益，A-GB01）": _fmt_pct(decomposition.get("strategy_return")),
                    "PIT 等权基准": _fmt_pct(decomposition.get("benchmark_return")),
                    "净敞口 均值/峰值": f"{_fmt_num(decomposition.get('exposure_mean'))}x / "
                    f"{_fmt_num(decomposition.get('exposure_max'))}x",
                    "bars": decomposition.get("bars"),
                    "剔除的 bar (D-032)": decomposition.get("excluded_bars"),
                },
            ),
            (
                "Basket (point-in-time)",
                {
                    "每根 bar 的币数": _fmt_num((payload.get("benchmark") or {}).get("symbols_per_bar")),
                    "因缺价跳过的 symbol-bar": (payload.get("benchmark") or {}).get("prices_missing"),
                },
            ),
            ("Constant beta", _beta_regression_lines(decomposition.get("constant") or {})),
            (
                # The one that survives the operator moving `vol_target`; the gap to the block above
                # is the size of the error a constant-beta reading makes on this book.
                "Conditional (exposure x market)",
                _beta_regression_lines(decomposition.get("conditional") or {}),
            ),
        ],
    )
