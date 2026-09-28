"""合成数据：给 `scratchpad/ls_timing_btc_only_split.py` 的判定线试跑用，不碰真实数据。

报告里那 18 个标的，2025-01-01 到 2026-09-08 16:00 的假 1h K 线、资金费（恒定 0.01%）与账户多空比（每根 bar
12 个 5 分钟桶）。多空比只有 BTCUSDT 从头开始有，其余 17 个币从 2025-06-01 起才有——真实数据的形状（BTC
2021-01-01 起、其余 2021-12-01 起）缩短了。所有币的多空比跟着同一条全市场的均值回复走，再加一点各自的噪声；
大盘的漂移跟着这条多空比的 −lsr(168) 走，三个世界只是两段的漂移强度不同，正确答案在造的时候就定了：

- btc_only_edge：只有 BTC 那段漂移强（0.005），有横截面那段没有。→ 「有横截面的那段分不出噪声」
- both：只有 BTC 那段一样强，有横截面那段是 0.0015（#199 的 timing 世界那么强）。→ 「有横截面的那段也在」
- late_edge：只有 BTC 那段没有，有横截面那段 0.0015。→ 「有横截面的那段也在」

btc_only_edge 是要防的那个陷阱：只有 BTC 那段强到全样本的择时 t 也过 2，有横截面那段却什么都没有。

用法（`<out>` 放在仓库之外）：

    PYTHONPATH=$PWD .venv/bin/python scratchpad/ls_timing_btc_only_split_synthetic.py <out>
    for v in btc_only_edge both late_edge; do
        PYTHONPATH=$PWD .venv/bin/python scratchpad/ls_timing_btc_only_split.py --root <out>/$v
    done

2026-09-28 的读数，判定线入库前那一版，12 个全对（「未复现」是对的：日期范围本来就不是报告的）。择时部分的 NW t，
四个形状按 `8a838550` / `49742e60` / `2ca33804` / `8a87c4ae` 的顺序：

    world          判定                    全样本                        只有 BTC 那段（A）            有横截面那段（B）
    btc_only_edge  4/4 分不出噪声           2.65 / 2.27 / 1.15 / 2.76     4.78 / 6.16 / 5.59 / 5.08     1.43 / 0.79 / −0.22 / 1.47
    both           4/4 也在                 6.09 / 5.60 / 4.46 / 5.90     3.88 / 4.43 / 2.82 / 4.35     5.34 / 4.69 / 3.94 / 5.04
    late_edge      4/4 也在                 5.85 / 6.53 / 5.13 / 4.49     0.81 / −0.22 / −1.87 / −1.33  5.86 / 6.85 / 5.81 / 5.02

btc_only_edge 那一行就是这次拆分存在的理由：四个里三个全样本 t 过了 2（排第一的 2.65），有横截面那段一个都没过。
那段的 1.43、1.47 是纯噪声给的，离 2 不远，这条线不是白给的。

第一版三个世界两段一样强（都是 0.0015），12 个也全对，但只有 BTC 那段太短、只有一个币，btc_only_edge 的 A 段
t 最高 1.63，全样本 t 最高 1.69——陷阱没被试到。于是把 btc_only_edge 与 both 的 A 段加强到 0.005，判定线
（B 段 t ≥ 2.0）一个字没改。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_data.store import FundingStore, KlineStore, MetricsStore

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "research" / "mine-shortlist-20260909T172541Z.json"
SYMBOLS = json.loads(REPORT.read_text(encoding="utf-8"))["symbols"]
ONLY_FROM_START = "BTCUSDT"
CROSS_SECTION_FROM = pd.Timestamp("2025-06-01", tz="UTC")
WORLDS = (("btc_only_edge", 21), ("both", 22), ("late_edge", 23))
# the drift's strength before and after the cross-section starts
DRIFT = {"btc_only_edge": (0.005, 0.0), "both": (0.005, 0.0015), "late_edge": (0.0, 0.0015)}


def lsr(ratio: np.ndarray, window: int = 168) -> np.ndarray:
    frame = pd.DataFrame(ratio)
    return (frame / frame.rolling(window, min_periods=window).mean() - 1.0).to_numpy()


def mean_reverting_walk(rng: np.random.Generator, length: int) -> np.ndarray:
    walk = np.cumsum(rng.normal(0, 0.01, length))
    return walk - pd.Series(walk).rolling(500, min_periods=1).mean().to_numpy()


def build(world: str, out: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", "2026-09-08 16:00", freq="1h", tz="UTC")
    T, N = len(index), len(SYMBOLS)
    cross = np.asarray(index >= CROSS_SECTION_FROM)
    before, after = DRIFT[world]
    strength = np.where(cross, after, before)

    common = mean_reverting_walk(rng, T)
    ratio = 2.0 * np.exp(4.0 * common[:, None] + rng.normal(0, 0.002, (T, N)))
    signal = -np.nan_to_num(lsr(ratio).mean(axis=1))
    market = strength * np.roll(np.tanh(signal), 1) + rng.normal(0, 0.006, T)
    rets = market[:, None] * rng.uniform(0.8, 1.4, N)[None, :] + rng.normal(0, 0.008, (T, N))

    close = 100.0 * np.exp(np.cumsum(rets, axis=0))
    opens = np.vstack([close[:1], close[:-1]])
    ms = index.as_unit("ms").asi8.astype("int64")
    klines, funding, metrics = KlineStore(out), FundingStore(out), MetricsStore(out)
    for j, symbol in enumerate(SYMBOLS):
        frame = pd.DataFrame(
            {
                "open_time": ms,
                "open": opens[:, j],
                "high": np.maximum(opens[:, j], close[:, j]) * 1.001,
                "low": np.minimum(opens[:, j], close[:, j]) * 0.999,
                "close": close[:, j],
                "volume": 1000.0,
                "close_time": ms + 3_599_999,
                "quote_volume": 1000.0 * close[:, j],
                "trades": 100,
                "taker_buy_base": 500.0,
                "taker_buy_quote": 500.0 * close[:, j],
            }
        )
        klines.append(symbol, "1h", frame)
        funding.append(
            symbol, pd.DataFrame({"funding_time": ms[::8], "funding_rate": 0.0001, "mark_price": close[::8, j]})
        )
        # the long/short ratio exists for BTC from the first bar and for everyone else only from the cross-section on
        has_ratio = np.ones(T, dtype=bool) if symbol == ONLY_FROM_START else cross
        buckets = (ms[has_ratio][:, None] + np.arange(12)[None, :] * 300_000).ravel()
        metrics.append(
            symbol,
            pd.DataFrame(
                {
                    "open_time": buckets,
                    "symbol": symbol,
                    "count_long_short_ratio": np.repeat(ratio[has_ratio, j], 12),
                    "sum_open_interest": 1e6,
                }
            ),
        )
    print(f"{world}: {out}, {T} bars x {N} symbols, cross-section from {CROSS_SECTION_FROM.date()}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: ls_timing_btc_only_split_synthetic.py <out>")
    out = Path(sys.argv[1]).resolve()
    if ROOT in (out, *out.parents):
        raise SystemExit("<out> must be outside the repository: these are fake prices")
    for world, seed in WORLDS:
        if (out / world).exists():
            raise SystemExit(f"{out / world} already exists; the stores append, so start from an empty directory")
        build(world, out / world, seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
