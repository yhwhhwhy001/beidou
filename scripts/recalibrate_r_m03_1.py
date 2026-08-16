#!/usr/bin/env python3
"""R-M03-1 阈值重标定证据生成(M08 修正内核后回测)。

复刻引擎入场/退出信号语义(M03 修正后的指标数学):
- Wilder RSI(14)(beidou_research.factors.rsi 权威实现)
- Wilder ATR(14)百分比(feed._wilder_smooth_last 同语义)
- SMA5/20 crossover + trend_20_pct 门(engine.py:779-786)
- 退出:trailing 2.0×ATR / 止盈 3.0×ATR+rsi>70 / 均线破位
  (engine.py:1093-1101 同语义)

用 M08 修正后的 simulate_paper_window(复利 PnL/年化/复利回撤/
参数化 bars_per_year)评估,扫描:
- RSI 入场门对:(70,30) 旧基线 + (65,35)/(75,25)/(60,40)/(80,20)
- trailing ATR 乘数:1.5/2.0(旧)/2.5/3.0
- cost_bps:1.0/2.0/8.0(成本敏感性,规避配置口径不确定性)

输出:
- docs/optimization/runs/2026-08-16-deep-module-optimization/evidence/M03/
  r-m03-1-recalibration.json(全量扫描证据)
- 终端摘要:旧阈值在扫描网格中的排名与稳健性结论

用法: python scripts/recalibrate_r_m03_1.py [symbol...]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from beidou_research.backtest.replay import simulate_paper_window
from beidou_research.factors.rsi import compute_rsi_wilder

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KLINE_DIR = PROJECT_ROOT / ".beidou" / "data" / "klines"
EVIDENCE_DIR = (
    PROJECT_ROOT / "docs" / "optimization" / "runs" / "2026-08-16-deep-module-optimization" / "evidence" / "M03"
)

RSI_PAIRS = [(70, 30), (65, 35), (75, 25), (60, 40), (80, 20)]
TRAIL_MULTS = [1.5, 2.0, 2.5, 3.0]
COST_BPS = [1.0, 2.0, 8.0]
BARS_PER_YEAR = 24 * 365  # 1h
WINDOW = 200  # 滚动指标窗口(指标仅依赖近窗)
OLD_BASELINE = ((70, 30), 2.0)


def wilder_rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """逐 bar 滚动 Wilder RSI(研究链权威实现;无效期填 50.0 与 feed 同语义)。"""
    out: list[float] = []
    for i in range(len(closes)):
        result = compute_rsi_wilder(closes[max(0, i - WINDOW) : i + 1], period=period)
        out.append(float(result.value) if result.is_valid else 50.0)
    return out


def wilder_atr_pct_series(highs, lows, closes, period: int = 14) -> list[float]:
    """逐 bar 滚动 Wilder ATR 百分比(feed._wilder_smooth_last 同语义)。"""
    out: list[float] = []
    for i in range(len(closes)):
        start = max(1, i - WINDOW)
        trs = [
            max(
                highs[j] - lows[j],
                abs(highs[j] - closes[j - 1]),
                abs(lows[j] - closes[j - 1]),
            )
            for j in range(start, i + 1)
        ]
        if not trs:
            out.append(0.0)
            continue
        if len(trs) < period:
            atr = sum(trs) / len(trs)
        else:
            smoothed = sum(trs[:period]) / period
            for value in trs[period:]:
                smoothed = (smoothed * (period - 1) + value) / period
            atr = smoothed
        out.append(atr / closes[i] * 100 if closes[i] > 0 else 0.0)
    return out


def sma_series(closes: list[float], period: int) -> list[float]:
    out: list[float] = []
    for i in range(len(closes)):
        window = closes[max(0, i - period + 1) : i + 1]
        out.append(sum(window) / len(window) if window else 0.0)
    return out


def simulate(
    closes: list[float],
    sma5: list[float],
    sma20: list[float],
    rsi: list[float],
    atr_pct: list[float],
    *,
    rsi_long: float,
    rsi_short: float,
    trail_mult: float,
    cost_bps: float,
):
    """复刻引擎状态机,产出逐 bar 信号序列(+1/-1/0)。"""
    n = len(closes)
    signals = [0.0] * n
    pos = 0.0
    entry = 0.0
    for i in range(1, n):
        trend_20_pct = (closes[i] / closes[i - 20] - 1) * 100 if i >= 20 and closes[i - 20] > 0 else 0.0
        if pos == 0.0:
            crossover = sma5[i] - sma20[i]
            if crossover > 0 and trend_20_pct > 0.5 and rsi[i] < rsi_long:
                pos = 1.0
                entry = closes[i]
            elif crossover < 0 and trend_20_pct < -0.5 and rsi[i] > rsi_short:
                pos = -1.0
                entry = closes[i]
        else:
            pnl_pct = (closes[i] / entry - 1) * 100 * pos
            trailing = trail_mult * atr_pct[i]
            exit_now = False
            if (
                pnl_pct < -trailing
                or (pnl_pct > 3.0 * atr_pct[i] and rsi[i] > 70)
                or (pos > 0 and closes[i] < sma20[i])
                or (pos < 0 and closes[i] > sma20[i])
            ):
                exit_now = True
            if exit_now:
                pos = 0.0
                entry = 0.0
        signals[i] = pos
    return simulate_paper_window(
        signals,
        closes,
        cost_bps,
        min_window_bars=500,
        bars_per_year=BARS_PER_YEAR,
    )


def load_symbol(symbol: str) -> pd.DataFrame:
    path = KLINE_DIR / symbol / "1h.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"{path} missing — run backfill first")
    df = pd.read_parquet(path)
    df = df[df["is_closed"]].sort_values("open_time").reset_index(drop=True)
    return df


def run_symbol(symbol: str, results: dict) -> None:
    df = load_symbol(symbol)
    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    print(f"[{symbol}] bars={len(closes)} — computing indicators...")
    sma5 = sma_series(closes, 5)
    sma20 = sma_series(closes, 20)
    rsi = wilder_rsi_series(closes)
    atr_pct = wilder_atr_pct_series(highs, lows, closes)
    print(f"[{symbol}] scanning {len(RSI_PAIRS)}x{len(TRAIL_MULTS)}x{len(COST_BPS)} configs...")
    for rsi_long, rsi_short in RSI_PAIRS:
        for trail in TRAIL_MULTS:
            for cost in COST_BPS:
                result = simulate(
                    closes,
                    sma5,
                    sma20,
                    rsi,
                    atr_pct,
                    rsi_long=rsi_long,
                    rsi_short=rsi_short,
                    trail_mult=trail,
                    cost_bps=cost,
                )
                key = f"rsi({rsi_long},{rsi_short})/trail{trail}/cost{cost}"
                results.setdefault(symbol, {})[key] = (
                    None
                    if result is None
                    else {
                        "paper_ir": result.paper_ir,
                        "paper_sharpe": result.paper_sharpe,
                        "paper_drawdown_pct": result.paper_drawdown_pct,
                        "signal_consistency": result.signal_consistency,
                        "n_trades": result.n_trades,
                        "window_bars": result.window_bars,
                    }
                )


def legacy_sma_rsi(prices_window: list[float], period: int = 14) -> float:
    """M03 审查还原的旧实现:returns[-15:] 取 15 个收益率再取 14 个做简单均值。"""
    if len(prices_window) < 2:
        return 50.0
    returns = [
        (prices_window[i] - prices_window[i - 1]) / prices_window[i - 1]
        for i in range(1, len(prices_window))
        if prices_window[i - 1] > 0
    ]
    window = returns[-15:]
    if not window:
        return 50.0
    gains = [max(r, 0.0) for r in window]
    losses = [abs(min(r, 0.0)) for r in window]
    if not gains or not losses:
        return 50.0
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_mapping_analysis(symbol: str, results: dict) -> None:
    """旧 SMA-RSI 与新 Wilder RSI 的阈值等效映射(重标定的数学基础)。"""
    df = load_symbol(symbol)
    closes = df["close"].tolist()
    rows: list[dict] = []
    for i in range(2, len(closes)):
        window = closes[max(0, i - WINDOW) : i + 1]
        old_rsi = legacy_sma_rsi(window)
        new_result = compute_rsi_wilder(window, period=14)
        new_rsi = float(new_result.value) if new_result.is_valid else None
        if new_rsi is not None:
            rows.append({"old": round(old_rsi), "new": new_rsi})
    if not rows:
        return
    frame = pd.DataFrame(rows)
    mapping = {}
    for old_level in (30, 70):
        sub = frame[frame["old"] == old_level]
        if sub.empty:
            continue
        mapping[str(old_level)] = {
            "n": len(sub),
            "new_rsi_median": round(float(sub["new"].median()), 2),
            "new_rsi_p25": round(float(sub["new"].quantile(0.25)), 2),
            "new_rsi_p75": round(float(sub["new"].quantile(0.75)), 2),
            "diff_mean": round(float((sub["new"] - old_level).mean()), 2),
        }
    results.setdefault("_rsi_mapping", {})[symbol] = mapping
    print(
        f"[{symbol}] RSI 映射: "
        + " | ".join(
            f"old{old}→new median {v['new_rsi_median']} [p25 {v['new_rsi_p25']}, p75 {v['new_rsi_p75']}] n={v['n']}"
            for old, v in mapping.items()
        )
    )


def vol_tier_analysis(symbol: str, results: dict) -> None:
    """R-M03-3: 年化修正后 adaptive_leverage 档位失效量化。

    旧口径 ann_vol_old = vol_20×√365(1h 误用日频年化);
    新口径 ann_vol_new = vol_20×√8760 = old×√24。
    统计:旧档位(0.2/0.4/0.6)在新/旧口径下的时间占比与杠杆分布。
    """
    df = load_symbol(symbol)
    closes = df["close"].tolist()
    rows: list[dict] = []
    for i in range(20, len(closes)):
        rets = [(closes[j] - closes[j - 1]) / closes[j - 1] for j in range(i - 19, i + 1) if closes[j - 1] > 0]
        if len(rets) < 20:
            continue
        vol_20 = (sum(r**2 for r in rets) / len(rets)) ** 0.5
        rows.append(
            {
                "old": vol_20 * (365**0.5),
                "new": vol_20 * (8760**0.5),
            }
        )
    if not rows:
        return
    frame = pd.DataFrame(rows)
    tiers = (0.2, 0.4, 0.6)
    levels = (3.0, 2.0, 1.0, 0.5)

    def bucket_pct(ann_vol: pd.Series) -> dict[str, float]:
        out = {}
        for level in levels:
            if level == 3.0:
                mask = ann_vol < tiers[0]
            elif level == 2.0:
                mask = (ann_vol >= tiers[0]) & (ann_vol < tiers[1])
            elif level == 1.0:
                mask = (ann_vol >= tiers[1]) & (ann_vol < tiers[2])
            else:
                mask = ann_vol >= tiers[2]
            out[str(level)] = round(float(mask.mean() * 100), 2)
        return out

    old_bucket = bucket_pct(frame["old"])
    new_old_threshold = bucket_pct(frame["new"])  # 新口径 + 旧阈值
    equivalent_tiers = tuple(t * (24**0.5) for t in tiers)
    eq_bucket = {}
    for idx, level in enumerate(levels):
        if idx == 0:
            mask = frame["new"] < equivalent_tiers[0]
        elif idx == 3:
            mask = frame["new"] >= equivalent_tiers[2]
        else:
            mask = (frame["new"] >= equivalent_tiers[idx - 1]) & (frame["new"] < equivalent_tiers[idx])
        eq_bucket[str(level)] = round(float(mask.mean() * 100), 2)
    results.setdefault("_vol_tiers", {})[symbol] = {
        "old_ann_vol_bucket_pct": old_bucket,
        "new_ann_vol_with_old_thresholds_pct": new_old_threshold,
        "new_ann_vol_with_equivalent_thresholds_pct": eq_bucket,
        "equivalent_thresholds": [round(t, 2) for t in equivalent_tiers],
        "new_ann_vol_quantiles": {
            q: round(float(frame["new"].quantile(q)), 2) for q in (0.25, 0.5, 0.75, 0.9, 0.95)
        },
    }
    print(
        f"[{symbol}] vol 档位: 旧口径={old_bucket} | 新口径+旧阈值={new_old_threshold} "
        f"| 新口径+等效阈值{tuple(round(t,2) for t in equivalent_tiers)}={eq_bucket}"
    )


def main() -> None:
    symbols = sys.argv[1:] or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    results: dict = {}
    for symbol in symbols:
        run_symbol(symbol, results)
        rsi_mapping_analysis(symbol, results)
        vol_tier_analysis(symbol, results)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "R-M03-1 threshold recalibration evidence (M08-corrected backtest kernel)",
        "method": "engine-equivalent signal state machine + M08 simulate_paper_window",
        "old_baseline": {"rsi_pair": [70, 30], "trail_mult": 2.0},
        "results": results,
    }
    out_path = EVIDENCE_DIR / "r-m03-1-recalibration.json"
    out_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"\nEvidence written: {out_path}")

    # 摘要:旧基线在各 symbol 的排名
    print("\n=== 旧阈值(70/30, trail 2.0)稳健性摘要 ===")
    for symbol in symbols:
        rows = {k: v for k, v in results[symbol].items() if v is not None}
        if not rows:
            print(f"{symbol}: no valid window")
            continue
        ranked = sorted(rows.items(), key=lambda kv: kv[1]["paper_ir"], reverse=True)
        old_keys = [k for k in rows if k.startswith("rsi(70,30)/trail2.0/")]
        best = ranked[0]
        for old_key in old_keys:
            rank = next(idx for idx, (k, _) in enumerate(ranked) if k == old_key) + 1
            old = rows[old_key]
            print(
                f"{symbol}: cost={old_key.split('cost')[-1]}bps "
                f"paper_ir={old['paper_ir']:.3f} rank={rank}/{len(ranked)} "
                f"(best: {best[0].split('/cost')[0]} ir={best[1]['paper_ir']:.3f}) "
                f"dd={old['paper_drawdown_pct']:.1f}% trades={old['n_trades']}"
            )


if __name__ == "__main__":
    main()
