"""lsr_timing 判定脚本的合成数据试跑：三个答案已知的世界，按预登记的协议跑 validate，再跑判定脚本。

不碰真实数据，不写真 ledger：validate 的 ledger 指向 ``--work`` 里的一个文件（`BEIDOU_TRIALS_LEDGER`），
报告也写在那里；跑完核一遍仓库里 `reports/research/trials.jsonl` 的 sha256 没变。

每个世界 13 个币、2021-01-01 起五年 1h K 线、资金费每 8 小时一次、按天刷新的时点成员表。多空比只有 BTC
从头就有，其余从 2021-12-01 起才有（与真实数据同一天），所以 A 段是那之前的十一个月。成员表里 S11 在
2022-06-01 离开，S12 的 K 线 2021-09-01 才开始、2022-03-01 才进，让 pit 的进出都走一遍。大盘因子加各币自己的
噪声；账户多空比是 2 × exp(人群的公共部分 + 各币一点自己的)。validate 按预登记的协议跑，`--prior-trials 658`。

- **timing**：人群的公共部分是一条外生的慢 AR(1)；大盘每根 bar 的漂移是上一根人群偏离（对自己过去 168 根
  均值）的反方向。四条判据都应当过。
- **trend_only**：大盘有动量，漂移跟着过去 168 根的涨跌走；人群的公共部分反着价格水平走，所以 −M 就是一个
  趋势信号。书只是趋势跟随的另一个写法：「B 段超出对照」应当不过（validate 过不过都行）。
- **btc_era**：timing 的机制只在多空比只有 BTC 有的那十一个月里有，之后大盘没有漂移：「B 段超出对照」应当
  不过。这是 A 段单列要防的那种书：全样本也许好看，钱在单币那段。

第一次试跑（两年、A 段五个月、趋势对照只有最优格窗口的一条）读对两个：trend_only 的「B 段超出对照」t 3.19，
过了。那个世界里书选中的 72 根窗口被更长窗口的趋势张成，所以判定脚本改成十条趋势对照（五个窗口、两种写法）；
同一次试跑里还看到 validate 的区间被第一个强读数来得最晚的那一格推后到 2022-04，所以信号改成第一个强读数
之前明确空仓。两处改动都在读真实数据之前，经过写在预登记里。世界本身没改，只拉长到五年、A 段挪到与真实数据
同一天，让 validate 的门按接近真实的长度量。

用法：

    PYTHONPATH=$PWD .venv/bin/python scratchpad/lsr_timing_criteria_synthetic.py --work <空目录>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from click.testing import CliRunner

from beidou_cli import main as cli
from beidou_data.pool import MEMBERSHIP_FILE
from beidou_data.store import FundingStore, KlineStore, MetricsStore

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "reports" / "research" / "trials.jsonl"
GRID = {"window": [72, 168], "scale": [0.5, 1.0]}
PRIOR_TRIALS = 658
HOUR_MS = 3_600_000
START = pd.Timestamp("2021-01-01", tz="UTC")
N_BARS = 24 * 1826  # 2021-01-01 to 2025-12-31
SYMBOLS = ["BTCUSDT", *[f"S{i:02d}USDT" for i in range(1, 13)]]
CROSS_SECTION_FROM = pd.Timestamp("2021-12-01", tz="UTC")
LATE, LATE_KLINES_FROM, LATE_JOINS = "S12USDT", pd.Timestamp("2021-09-01", tz="UTC"), pd.Timestamp("2022-03-01", tz="UTC")
LEAVER, LEAVES = "S11USDT", pd.Timestamp("2022-06-01", tz="UTC")
FACTOR_SIGMA, OWN_SIGMA = 0.006, 0.004
WORLDS = {"timing": True, "trend_only": False, "btc_era": False}  # what 「B 段超出对照」 and the whole rule should read


def ar1(rng: np.random.Generator, n: int, phi: float, sigma: float) -> np.ndarray:
    shocks = rng.normal(0.0, sigma, n)
    out = np.zeros(n)
    for t in range(1, n):
        out[t] = phi * out[t - 1] + shocks[t]
    return out


def deviation(level: np.ndarray, window: int = 168) -> np.ndarray:
    series = pd.Series(level)
    return (series / series.rolling(window, min_periods=window).mean() - 1.0).to_numpy()


def world(name: str, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """(market factor per bar, the crowd's common log level per bar)."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(START, periods=N_BARS, freq="h")
    noise = rng.normal(0.0, FACTOR_SIGMA, N_BARS)
    if name in ("timing", "btc_era"):
        crowd = ar1(rng, N_BARS, 1.0 - 1.0 / 200.0, 0.012)
        drift = -0.004 * np.nan_to_num(pd.Series(deviation(np.exp(crowd))).shift(1).to_numpy())
        if name == "btc_era":
            drift = np.where(index < CROSS_SECTION_FROM, 3.0 * drift, 0.0)
        return drift + noise, crowd
    factor = np.zeros(N_BARS)
    for t in range(1, N_BARS):
        trailing = factor[max(0, t - 168) : t].sum()
        factor[t] = 0.0003 * np.tanh(trailing / 0.05) + noise[t]
    crowd = -2.0 * np.cumsum(factor) + ar1(rng, N_BARS, 0.99, 0.004)
    return factor, crowd


def write_root(root: Path, name: str, seed: int) -> None:
    rng = np.random.default_rng(seed + 1_000)
    factor, crowd = world(name, seed)
    index = pd.date_range(START, periods=N_BARS, freq="h")
    open_ms = (index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
    klines, funding, metrics = KlineStore(root), FundingStore(root), MetricsStore(root)
    for j, symbol in enumerate(SYMBOLS):
        beta = 0.8 + 0.4 * j / len(SYMBOLS)
        returns = beta * factor + rng.normal(0.0, OWN_SIGMA, N_BARS)
        close = 100.0 * np.exp(np.cumsum(returns))
        opened = np.concatenate(([100.0], close[:-1]))
        frame = pd.DataFrame(
            {
                "open_time": open_ms.to_numpy(),
                "open": opened,
                "high": np.maximum(opened, close) * 1.001,
                "low": np.minimum(opened, close) * 0.999,
                "close": close,
                "volume": 1_000.0,
                "close_time": open_ms.to_numpy() + HOUR_MS - 1,
                "quote_volume": 1_000.0 * close,
                "trades": 100,
                "taker_buy_base": 500.0,
                "taker_buy_quote": 500.0 * close,
            }
        )
        first = LATE_KLINES_FROM if symbol == LATE else START
        frame = frame[index >= first]
        klines.append(symbol, "1h", frame)
        settles = frame[frame["open_time"] % (8 * HOUR_MS) == 0]
        funding.append(
            symbol,
            pd.DataFrame(
                {
                    "funding_time": settles["open_time"].to_numpy() + 3,
                    "funding_rate": rng.normal(0.0001, 0.00005, len(settles)),
                    "mark_price": settles["close"].to_numpy(),
                }
            ),
        )
        ratio = 2.0 * np.exp(crowd + ar1(rng, N_BARS, 0.99, 0.005))
        has_ratio = index >= (START if symbol == "BTCUSDT" else max(first, CROSS_SECTION_FROM))
        metrics.append(
            symbol,
            pd.DataFrame(
                {
                    "open_time": open_ms.to_numpy()[has_ratio] + 55 * 60_000,  # the 5m bucket that closes with the bar
                    "symbol": symbol,
                    "count_long_short_ratio": ratio[has_ratio],
                }
            ),
        )
    days = pd.date_range(START, index[-1].floor("D"), freq="D")
    membership = pd.DataFrame(True, index=days, columns=SYMBOLS)
    membership.loc[days < LATE_JOINS, LATE] = False
    membership.loc[days >= LEAVES, LEAVER] = False
    membership.to_parquet(root / MEMBERSHIP_FILE)


def validate(root: Path, out: Path, ledger: Path) -> Path:
    os.environ["BEIDOU_TRIALS_LEDGER"] = str(ledger)
    try:
        result = CliRunner().invoke(
            cli,
            [
                "research", "validate", "--strategy", "lsr_timing", "--universe", "pit", "--root", str(root),
                "--grid", json.dumps(GRID), "--charge", "4", "--prior-trials", str(PRIOR_TRIALS), "--out", str(out),
            ],
        )  # fmt: skip
    finally:
        del os.environ["BEIDOU_TRIALS_LEDGER"]
    if result.exit_code != 0:
        raise SystemExit(f"validate failed: {result.output}\n{result.exception!r}")
    return next(out.glob("lsr_timing-validation-*.json"))


def criteria(report: Path, root: Path, out: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in ("BEIDOU_TRIALS_LEDGER", "BEIDOU_FEATURE_STORE")}
    env["PYTHONPATH"] = str(ROOT)
    run = subprocess.run(
        [sys.executable, str(ROOT / "scratchpad" / "lsr_timing_criteria.py"), "--report", str(report), "--root", str(root),
         "--out", str(out)],
        capture_output=True, text=True, env=env, cwd=ROOT, check=False,
    )  # fmt: skip
    if run.returncode != 0:
        raise SystemExit(f"criteria failed:\n{run.stdout[-2000:]}\n{run.stderr[-4000:]}")
    return dict(json.loads(next(out.glob("lsr_timing-criteria-*.json")).read_text(encoding="utf-8")))


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--work", required=True, help="an empty directory; everything this writes goes there")
    args = parser.parse_args()
    work = Path(args.work)
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"{work} is not empty")
    for name in ("BEIDOU_TRIALS_LEDGER", "BEIDOU_FEATURE_STORE"):
        if os.environ.get(name):
            raise SystemExit(f"{name} is set; unset it")
    before = hashlib.sha256(LEDGER.read_bytes()).hexdigest() if LEDGER.exists() else None

    rows = []
    for seed, (name, expected) in enumerate(WORLDS.items(), start=11):
        root, out, checked = work / name / "data", work / name / "reports", work / name / "criteria"
        root.mkdir(parents=True)
        write_root(root, name, seed)
        report_path = validate(root, out, work / name / "trials.jsonl")
        read = criteria(report_path, root, checked)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        b, a = read["segments"]["B"], read["segments"]["A"]
        rule = read["criteria"]
        rows.append(
            {
                "world": name,
                "expected": expected,
                "alpha_passes": rule["alpha_passes"],
                "all_pass": rule["all_pass"],
                "criteria": rule,
                "verdict_reasons": report["reasons"],
                "range": report["range"],
                "best_params": report["best_params"],
                "full_sample_sharpe": report["full_sample"]["annualized_sharpe"],
                "split": read["split_decision_bar"],
                "b_alpha_t": b["beyond_controls"]["t_stat"],
                "a_alpha_t": a["beyond_controls"]["t_stat"],
                "b_book_t": b["streams"]["book"]["t_stat"],
                "b_controls_t": {k: v["t_stat"] for k, v in b["streams"].items() if k != "book"},
                "b_betas": b["beyond_controls"]["betas"],
                "a_share": a["book_share_of_net"],
                "a_side": a["side"],
                "b_side": b["side"],
            }
        )
        print(json.dumps(rows[-1], ensure_ascii=False, default=str))

    after = hashlib.sha256(LEDGER.read_bytes()).hexdigest() if LEDGER.exists() else None
    print(f"repository ledger sha256 before {before} after {after}: {'unchanged' if before == after else 'CHANGED'}")
    right = sum(row["alpha_passes"] == row["expected"] and row["all_pass"] == row["expected"] for row in rows)
    print(f"{right}/{len(rows)} worlds read as built")
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
