"""合成数据：给 `scratchpad/ls_direction_look.py` 的判定线试跑用，不碰真实数据。

报告里那 18 个标的，2025-01-01 到 2026-09-08 16:00 的假 1h K 线、资金费（恒定 0.01%）与账户多空比
（每根 bar 12 个 5 分钟桶），四个世界，每个世界的正确答案在造的时候就定了：

- timing：全市场一条多空比，大盘的漂移跟着它的 −lsr 走。→ 「只在赌方向：择时」
- selection：各币各自的多空比，各币自己的漂移跟着自己的 −lsr 走，大盘不动。→ 「不只是方向」
- constant：多空比一路上行，大盘一路下跌。→ 「只在赌方向：恒定持仓就够」
- beta：像 timing，但各币的偏离按自己的 β 放大，各币对大盘的敞口也是这个 β。候选净多时偏重高 β 的币、净空时偏重
  低 β 的币，于是净敞口为 0 的选币部分也跟着大盘挣钱。→ 「只在赌方向：择时」

用法（`<out>` 放在仓库之外）：

    PYTHONPATH=$PWD .venv/bin/python scratchpad/ls_direction_look_synthetic.py <out>
    for v in timing selection constant beta; do
        PYTHONPATH=$PWD .venv/bin/python scratchpad/ls_direction_look.py --root <out>/$v
    done

2026-09-27 的读数，判定线入库前那一版，16 个全对（「未复现」是对的：日期范围本来就不是报告的）。排第一的
`8a838550a686852d` 的数：

    world      判定                  选币 t   扣掉方向后 t   择时 t
    timing     只在赌方向：择时       0.51     0.55           4.98
    selection  不只是方向             19.43    19.45          -
    constant   只在赌方向：恒定持仓就够  0.24     0.31          -0.53
    beta       只在赌方向：择时       2.23     0.78           8.08

constant 世界里另外三个形状是 falsifier B 判的（Sharpe 2.63 / 2.56 / 2.63 ≤ 恒定空头 2.66），排第一的那个以
Sharpe 2.658 对 2.656 过了 falsifier B，落在「择时部分分不出噪声」那一条上——两条路到的是同一个答案。beta 世界
那一行是「扣掉方向」那一步存在的理由：没有它，选币 t 2.23 就判成了「不只是方向」。

这份脚本定判定线之前写好、判定线的三处修改（手续费分摊、选币的 t、恒定持仓比 Sharpe 而不是边际）都是在它上面
抓到的，经过见 `ls_direction_look.py` 的 docstring。
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
WORLDS = (("timing", 1), ("selection", 2), ("constant", 3), ("beta", 4))


def lsr(ratio: np.ndarray, window: int = 168) -> np.ndarray:
    frame = pd.DataFrame(ratio)
    return (frame / frame.rolling(window, min_periods=window).mean() - 1.0).to_numpy()


def mean_reverting_walks(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    walks = np.cumsum(rng.normal(0, 0.01, shape), axis=0)
    return walks - pd.DataFrame(walks).rolling(500, min_periods=1).mean().to_numpy().reshape(shape)


def build(world: str, out: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2025-01-01", "2026-09-08 16:00", freq="1h", tz="UTC")
    T, N = len(index), len(SYMBOLS)
    if world == "timing":
        common = mean_reverting_walks(rng, (T,))
        ratio = 2.0 * np.exp(4.0 * common[:, None] + rng.normal(0, 0.002, (T, N)))
        signal = -np.nan_to_num(lsr(ratio).mean(axis=1))
        market = 0.0015 * np.roll(np.tanh(signal), 1) + rng.normal(0, 0.006, T)
        rets = market[:, None] * rng.uniform(0.8, 1.4, N)[None, :] + rng.normal(0, 0.008, (T, N))
    elif world == "beta":
        betas = rng.uniform(0.5, 2.0, N)
        common = mean_reverting_walks(rng, (T,))
        ratio = 2.0 * np.exp(4.0 * common[:, None] * betas[None, :] + rng.normal(0, 0.002, (T, N)))
        signal = -np.nan_to_num(lsr(ratio).mean(axis=1))
        market = 0.0015 * np.roll(np.tanh(signal), 1) + rng.normal(0, 0.006, T)
        rets = market[:, None] * betas[None, :] + rng.normal(0, 0.008, (T, N))
    elif world == "constant":
        walks = mean_reverting_walks(rng, (T, N))
        ratio = 2.0 * np.exp(0.0025 * np.arange(T)[:, None] + 1.0 * walks)
        market = -0.0003 + rng.normal(0, 0.006, T)
        rets = market[:, None] * rng.uniform(0.8, 1.4, N)[None, :] + rng.normal(0, 0.008, (T, N))
    elif world == "selection":
        walks = mean_reverting_walks(rng, (T, N))
        ratio = 2.0 * np.exp(4.0 * walks)
        signal = -np.nan_to_num(lsr(ratio))
        market = rng.normal(0, 0.006, T)
        rets = market[:, None] + 0.0015 * np.roll(np.tanh(signal), 1, axis=0) + rng.normal(0, 0.008, (T, N))
    else:
        raise SystemExit(f"no world called {world!r}")
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
        buckets = (ms[:, None] + np.arange(12)[None, :] * 300_000).ravel()
        metrics.append(
            symbol,
            pd.DataFrame(
                {
                    "open_time": buckets,
                    "symbol": symbol,
                    "count_long_short_ratio": np.repeat(ratio[:, j], 12),
                    "sum_open_interest": 1e6,
                }
            ),
        )
    print(f"{world}: {out}, {T} bars x {N} symbols")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: ls_direction_look_synthetic.py <out>")
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
