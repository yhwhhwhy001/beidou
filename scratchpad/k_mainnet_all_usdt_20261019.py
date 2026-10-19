"""mainnet 全 USDT 保证金下的 k（2026-10-19）：D-035 在因子 1.0 上重量，零 ledger。

起因。操作者 2026-10-19 在项目线程「实盘交易切换方案」的卡片上选了「现在重推」与「不算，现在做」：mainnet 小额校准
（L-B）的 k 按全 USDT 保证金重推，用 mainnet 自己的一份 registry 与阶梯，不动 demo。

规则（D-035，09-04 先写后跑，原样）：取自助 q95 回撤仍在声明预算 −70% 以内的最大 k，pit 与 static 两个 universe 都要满足。
全 USDT 账户上可动用 = 总权益，因子 F = 1.0，档位按同一条规则取 `rescaled(k, budget=0.70)` = ((−0.49, 0.75k), (−0.70, 0.50k))，
base = k。这就是 09-25 `k_remeasure_honest_drift_20260925.py` 的 K-4 臂（`ladder@k`、`unit`），只换三处：

1. **书**：今天的 registry（flow 已退役，只剩 tsmom 主书）与今天的 profile。面板走同一条 `p32h.panel` -> `p32d.panel_series`。
2. **数据末端**：钉在 2026-10-19 01:00（不含），即最后一根 bar 开盘 10-19T00:00Z，与在架证据
   `tsmom-validation-20261019T021124Z.json` 的 range 末端相同。
3. **网格**：0.20 到 0.60，步长 0.05，共 9 格（09-25 那 8 格加 0.55）。不加更细的格子：插值只作读数。

漂移两臂，**取两臂 D-035 答案的较小者**（写在跑之前，不看结果改）：

* `h0919`：1.2306419157745636，`tsmom-validation-20260919T081914Z.json` 的 `oos_selection.oos_sharpe_annual`（k=0.60，16 格各折自选）。
* `h0930`：1.583996462786068，`tsmom-validation-20260930T142244Z.json` 同一字段（k=0.175，16 格各折自选，FAIL 差 0.0144）。
  Sharpe 对 k 不变（`config/live.demo.yaml` P13 那段的恒等式），所以两个读数都描述同一本书，差别在数据窗口与 registry。

另跑原漂移（`orig`）与无梯（`none`）臂作对照，不进规则。

不在规则里、但要一起写下的：

* 成本模型是平费率 7 bps、无冲击。L-B 资金下这是好近似；L-C 之前要按真实成交校准冲击系数再量一次（MAINNET_READINESS §6）。
* 周块 bootstrap 抹掉多月 regime，换算假设亏损全落在 USDT（全 USDT 账户上这一条恰好成立），CAGR 只有差值有意义。
* 本脚本只量，不改任何 config；采纳是 mainnet profile / registry 的 PR，另裁。

零 ledger 的核对与 09-25 那支相同：跑前跑后各算一次 `reports/research/trials.jsonl` 的 sha256，不等就报错退出；
`BEIDOU_TRIALS_LEDGER` 未设时指向 `<out>/ledger-must-not-exist.jsonl`，结尾断言不存在。

复现::

    PYTHONPATH=. .venv/bin/python scratchpad/k_mainnet_all_usdt_20261019.py --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import k_remeasure_honest_drift_20260925 as km
import p32d_ladder_bootstrap_pathwise as p32d
import p32h_k_sweep_at_the_base_in_force as p32h
from p32f_usdt_denominated_budget import DECLARED_BUDGET

ROOT = Path(__file__).resolve().parents[1]
GRID = (0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20)
END = "2026-10-19 01:00"  # exclusive: last bar opens 2026-10-19T00:00Z, the cited evidence's range end
DRIFTS = {"h0919": 1.2306419157745636, "h0930": 1.583996462786068}
LEDGER = ROOT / "reports" / "research" / "trials.jsonl"


def build_panels(job: tuple[str, str]) -> dict[str, Any]:
    """`km.build_panels` for the d3 band with this file's grid and end; same pin, same counting."""
    mode, out = job
    os.chdir(ROOT)
    calls = {"load": 0, "load_cached": 0}
    cache: dict[Any, Any] = {}
    original_load = p32d._load

    def load_to_end(root, symbols, interval, start, end, funding, *args, **kwargs):
        if start is not None or end is not None:
            raise RuntimeError(f"panel_series passed start/end ({start!r}, {end!r}); this pin assumes None/None")
        key = (root, tuple(symbols), interval, funding, args, tuple(sorted(kwargs.items())))
        calls["load"] += 1
        if key in cache:
            calls["load_cached"] += 1
            return cache[key]
        cache[key] = original_load(root, symbols, interval, start, END, funding, *args, **kwargs)
        return cache[key]

    p32d._load = load_to_end
    series: dict[str, np.ndarray] = {}
    shape: dict[str, Any] = {}
    try:
        for k in GRID:
            before = calls["load"]
            mark, realised = p32h.panel(mode, k)
            if calls["load"] != before + 1:
                raise RuntimeError(f"the end pin did not take at {mode}/k={k}: {calls}")
            panel = next(iter(cache.values()))
            shape = {
                "panel_bars": len(panel.index),
                "first": str(panel.index[0]),
                "last": str(panel.index[-1]),
                "symbols": len(panel.symbols),
            }
            series[f"k{k:.2f}_mark"], series[f"k{k:.2f}_realised"] = mark, realised
    finally:
        p32d._load = original_load
    path = Path(out) / f"panels-{mode}-d3.npz"
    np.savez(path, **series)
    return {
        "mode": mode,
        "file": str(path),
        "calls": calls,
        **shape,
        "series_bars": len(series[f"k{GRID[0]:.2f}_mark"]),
    }


def tasks_for(panels: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(mode: str, k: float, arm: str, rungs: str, drift: str) -> None:
        out.append(
            {
                "label": f"{mode}|k{k:.2f}|{arm}|{drift}",
                "set": "mainnet",
                "mode": mode,
                "band": "d3",
                "k": k,
                "arm": arm,
                "rungs": rungs,
                "drift": "orig" if drift == "orig" else "honest",
                "target_sharpe": DRIFTS.get(drift),
                "file": panels[mode],
                "key": f"k{k:.2f}",
                "drift_name": drift,
            }
        )

    for mode in ("pit", "static"):
        for drift in ("h0919", "h0930", "orig"):
            for k in GRID:
                add(mode, k, "ladder@k", "unit", drift)
    for mode in ("pit", "static"):
        for drift in ("h0919", "h0930", "orig"):
            for k in GRID:
                add(mode, k, "none", "none", drift)
    return out


def arm(task: dict[str, Any]) -> dict[str, Any]:
    row = km.run_arm(task)
    row["drift_name"] = task["drift_name"]
    row["label"] = task["label"]
    return row


def crossing(points: list[tuple[float, float, float]], target: float) -> tuple[float, float] | None:
    for (k1, q1, c1), (k2, q2, c2) in pairwise(sorted(points, key=lambda p: -p[0])):
        if q1 < target <= q2:
            t = (target - q1) / (q2 - q1)
            return k1 + t * (k2 - k1), c1 + t * (c2 - c1)
    return None


def d035(table: dict[tuple[str, float], dict[str, Any]]) -> dict[str, Any]:
    holds = [k for k in GRID if table[("pit", k)]["holds"] and table[("static", k)]["holds"]]
    best = max(holds) if holds else None
    out: dict[str, Any] = {"k": best}
    if best is not None:
        out["margin_pp"] = {m: table[(m, best)]["margin"] for m in ("pit", "static")}
        out["cagr_median"] = {m: table[(m, best)]["cagr"] for m in ("pit", "static")}
        above = [k for k in GRID if k > best]
        if above:
            out["next_up"] = {
                "k": min(above),
                "margin_pp": {m: table[(m, min(above))]["margin"] for m in ("pit", "static")},
            }
    out["interpolated"] = {
        m: crossing([(k, table[(m, k)]["q95"], table[(m, k)]["cagr"]) for k in GRID], -DECLARED_BUDGET)
        for m in ("pit", "static")
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", default=str(ROOT / "scratchpad" / "k-mainnet-20261019"))
    args = parser.parse_args()
    os.chdir(ROOT)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sentinel = None
    if not os.environ.get("BEIDOU_TRIALS_LEDGER"):
        sentinel = out / "ledger-must-not-exist.jsonl"
        os.environ["BEIDOU_TRIALS_LEDGER"] = str(sentinel)
    ledger_before = km._sha(LEDGER)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"HEAD {head}; trials.jsonl sha256 before {ledger_before}; end {END} (exclusive); drifts {DRIFTS}")

    t0 = time.time()
    with ProcessPoolExecutor(max_workers=2) as pool:
        built = list(pool.map(build_panels, [(m, str(out)) for m in ("pit", "static")]))
    for item in built:
        print(
            f"  panels {item['mode']}: {item['series_bars']} bars of {item['panel_bars']} "
            f"({item['first']} -> {item['last']}, {item['symbols']} symbols) calls {item['calls']}"
        )
    panels = {i["mode"]: i["file"] for i in built}
    sharpe = {m: {f"{k:.2f}": km.annual_sharpe(np.load(panels[m])[f"k{k:.2f}_mark"]) for k in GRID} for m in panels}
    tasks = tasks_for(panels)
    print(f"panels built in {time.time() - t0:.0f}s; {len(tasks)} arms; series Sharpe {sharpe}")

    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(arm, task) for task in tasks]
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if i % 10 == 0 or i == len(tasks):
                print(f"  [{i}/{len(tasks)} {time.time() - t0:5.0f}s]", flush=True)
    by = {r["label"]: r for r in rows}
    for mode in ("pit", "static"):
        shas = {r["starts_sha"] for r in rows if r["mode"] == mode}
        if len(shas) != 1:
            raise RuntimeError(f"{mode}: arms drew from {len(shas)} different sets of block starts: {shas}")

    def table(arm_name: str, drift: str) -> dict[tuple[str, float], dict[str, Any]]:
        return {
            (m, k): km.reading(by[f"{m}|k{k:.2f}|{arm_name}|{drift}"], "unit") for m in ("pit", "static") for k in GRID
        }

    tables = {f"{a}_{d}": table(a, d) for a in ("ladder@k", "none") for d in ("h0919", "h0930", "orig")}
    rules = {name: d035(t) for name, t in tables.items()}
    for name, t in tables.items():
        print(f"\n== {name}")
        for k in GRID:
            p, s = t[("pit", k)], t[("static", k)]
            print(
                f"{k:>5.2f} | pit {p['q95']:>8.2%} P>70 {p['p']:>6.2%} CAGR {p['cagr']:>7.1%} | static {s['q95']:>8.2%} "
                f"P>70 {s['p']:>6.2%} CAGR {s['cagr']:>7.1%} | {'YES' if p['holds'] and s['holds'] else 'no'}"
            )
        print(f"   D-035 -> {rules[name]}")
    answers = [rules[f"ladder@k_{d}"]["k"] for d in DRIFTS]
    adopted = min(answers) if all(a is not None for a in answers) else None
    print(f"\nPRE-REGISTERED ANSWER (min over h0919/h0930 of D-035 on ladder@k, factor 1.0): k = {adopted}")

    ledger_after = km._sha(LEDGER)
    if ledger_after != ledger_before:
        raise RuntimeError(f"trials.jsonl changed during the run: {ledger_before} -> {ledger_after}")
    if sentinel is not None and sentinel.exists():
        raise RuntimeError(f"something wrote a ledger row to {sentinel}")
    meta = {
        "kind": "meta",
        "script": "scratchpad/k_mainnet_all_usdt_20261019.py",
        "head": head,
        "grid": list(GRID),
        "end_exclusive": END,
        "drifts": DRIFTS,
        "factor": 1.0,
        "draws": km.DRAWS,
        "seed": p32d.SEED,
        "block": p32d.BLOCK,
        "static_universe": list(p32h.STATIC_P32G),
        "series_sharpe": sharpe,
        "panels": [{k: v for k, v in item.items() if k != "file"} for item in built],
        "d035": rules,
        "adopted_rule": "min over drifts h0919/h0930 of D-035 on ladder@k (rescaled(k, budget=0.70)), factor 1.0",
        "answer_k": adopted,
        "trials_sha256_before": ledger_before,
        "trials_sha256_after": ledger_after,
        "ledger_sentinel_checked": sentinel is not None,
    }
    path = out / "k-mainnet-all-usdt-20261019.json"
    path.write_text(json.dumps([meta, *rows], indent=1), encoding="utf-8")
    print(f"\ntrials.jsonl unchanged; wrote {path} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
