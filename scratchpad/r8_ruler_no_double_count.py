"""R8 尺子不再重复计算已实现盈亏：用修复后的 `attributed_drawdown_state` 重放实盘记录，与 W5 的「不重复口径」对账。

缺陷记在 docs/RESEARCH_LOG.md「2026-09-25 · 8.R：两条在跑的状态规则的确认延迟」的「尺子还有一处偏差」。
第 k 行的 `unrealized` 是第 k 个周期下单之前的快照；挂在第 k 行的收入，是第 k 个周期下单之后才实现、
下一个周期才收进来的（`_ingest_income` 按 `state.last_bar_ms` 记账）。两者相加，一笔平仓算了两次。
修法：每条收入挂到收进它的那个周期的行上。操作者 2026-09-25 裁定「修」，与 10-13 那次重启同批合入。

复现（在仓库根目录，worktree 也一样）：

    cp -p .beidou/live/cycles.jsonl .beidou/live/attribution.jsonl <副本目录>/   # 避开整点前后 2 分钟
    PYTHONPATH=$PWD .venv/bin/python scratchpad/r8_ruler_no_double_count.py --live <副本目录>

只读副本，不写任何文件，不走 `research` 命令，零 ledger。修复前的函数从 `BEFORE` 提交原样取出
（`git show`），不手抄。W5 的口径照 `scratchpad/regime_confirmation_lag.py` 的 `ruler_section` 原样重写：
收入挂到写入时刻不早于它的第一个定价行，那一行若是 rebaseline 就丢掉，行按序号重新编号。

读的是什么：

1. 记录的读数能否由修复前的函数复现（W5 的第一条，应为 364/364）；修复后的函数复现几个。记录只存
   `ruler` 标签，修复前后同名，所以 10-13 重启之后核「循环跑的是哪把尺子」就看这一行：重启之后的周期
   应当全部由修复后的函数逐位复现。
2. 修复后的函数与 W5 口径逐位对账：读数与高水位，比 `==`，另报 max|差|。
3. 修复前后的差异分布，与 W5 发表的数对照：331/364 不同、平均深 0.27pp、最多深 1.58pp、最多浅
   0.51pp、高水位最多高 226.89 USDT、09-24T18:00Z 读数 −4.17% → −2.89%。
4. 每条收入落在哪一行：修复后的函数（逐条二分前缀，看它何时不再 pending）对 W5 的落点。
5. 按引擎真实记账方式重做 2026-09-13 的「平仓不动读数」检验：持有与平掉两个世界，修复前后各读一遍。
6. 副本到最后一行（钉住之后的周期也算）的同样统计，以及日报口径（`_cycles`：定价、非 dry-run）的读数。

2026-09-25 的读数。副本 05:36Z 复制：cycles.jsonl 585 行（sha256 1faf0e45…），attribution.jsonl 102 行
（sha256 90318f98…），最后一行是 09-25T04:00Z 那根 bar。

    钉到 09-24T18:00Z：带读数的周期 364 个
      一、修复前的函数复现记录的读数 364/364，max|差| 0；修复后的复现 33/364，即修复前后相同的那 33 个
      二、修复后 vs W5 口径：读数逐位相同 364/364，高水位逐位相同 364/364，max|差| 都是 0
      三、读数不同 331/364；修复前 − 修复后：最深 −1.58pp，最浅 +0.51pp，均值 −0.27pp
          高水位 364 个周期全部偏高，最多 226.89 USDT、最少 1.88 USDT
          09-24T18:00Z：−4.17% → −2.89%；整段最深读数 −7.18% → −6.06%
    副本全长：374/374 逐位相同；读数不同 341/374，均值 −0.29pp；09-25T04:00Z：−4.49% → −3.33%
    四、102 条收入里 9 条的落点与 W5 不同，全在 09-03/04、第 54 行（唯一一次 rebaseline）之前：
        收入与 W5 选的那一行写在同一秒，修复后的规则不排同秒，顺延到下一根 bar。读数因此不受影响。
    五、平仓不动读数：修复前，读到那笔平仓的周期「持有 −4%、平掉 −8%」，亏损算了两次；修复后两边都是 −4%
    六、引擎口径与日报口径读数相同：−3.41%；收入 101 条在路径上、1 条在 base 里（第 54 行的 −21.80）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from beidou_live.risk_budget import RiskBudgetParams, attributed_drawdown_state  # noqa: E402

#: 修复之前的 main（PR #145 合入后）。旧函数从这个提交原样取出。
BEFORE = "54da72a8"
#: W5 钉住的那一行（含），对账只到这里。
LIVE_PIN = "2026-09-24T18:00:00+00:00"
PARAMS = RiskBudgetParams()
Ruler = Callable[[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]], RiskBudgetParams], dict[str, Any]]


def ruler_before() -> Ruler:
    """修复前的 `attributed_drawdown_state`，从 `BEFORE` 原样执行出来。"""
    source = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BEFORE}:beidou_live/risk_budget.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = types.ModuleType("risk_budget_before")
    sys.modules[module.__name__] = module  # dataclass 要在 sys.modules 里找到自己的模块
    exec(compile(source, f"{BEFORE}:beidou_live/risk_budget.py", "exec"), module.__dict__)
    ruler: Ruler = module.attributed_drawdown_state
    return ruler


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def priced(row: Mapping[str, Any]) -> bool:
    return isinstance(row.get("equity"), int | float) and row["equity"] > 0


def w5_landing(rows: Sequence[Mapping[str, Any]]) -> Callable[[Mapping[str, Any]], int | None]:
    """W5 的落点，照 `regime_confirmation_lag.ruler_section.landing` 原样：写入时刻不早于收入的第一个定价行。"""
    written = [datetime.fromisoformat(row["at"]) for row in rows]
    rebased = [bool((row.get("external_flows") or {}).get("rebaselined")) for row in rows]

    def landing(income: Mapping[str, Any]) -> int | None:
        ingested = datetime.fromisoformat(income["at"])
        first = next((i for i, at in enumerate(written) if at >= ingested), None)
        k = None if first is None else next((i for i in range(first, len(rows)) if priced(rows[i])), None)
        return None if k is None or rebased[k] else k

    return landing


def w5_raw_landing(rows: Sequence[Mapping[str, Any]], income: Mapping[str, Any]) -> int | None:
    """同上，但不把 rebaseline 行上的收入丢掉：只用来和修复后的落点比「落在哪一行」。"""
    ingested = datetime.fromisoformat(income["at"])
    first = next((i for i, row in enumerate(rows) if datetime.fromisoformat(row["at"]) >= ingested), None)
    return None if first is None else next((i for i in range(first, len(rows)) if priced(rows[i])), None)


def fixed_landing(rows: Sequence[Mapping[str, Any]], income: Mapping[str, Any]) -> int | None:
    """修复后的函数把这条收入挂在哪一行：只喂它一条，二分前缀长度，找它不再 pending 的第一行。

    用函数本身量，不另写一遍规则。单独喂与一起喂落点相同：排在前面的收入能落的行，排在后面的也
    只会更晚。
    """

    def pending(m: int) -> bool:
        block = attributed_drawdown_state(rows[:m], [income], PARAMS)
        return int(block.get("pending_rows", 1)) > 0

    if pending(len(rows)):
        return None
    low, high = 1, len(rows)
    while low < high:
        middle = (low + high) // 2
        if pending(middle):
            low = middle + 1
        else:
            high = middle
    return low - 1


def summarise(label: str, pairs: Sequence[tuple[str, float, float, float, float]]) -> None:
    """pairs: (bar, 修复前读数, 修复后读数, 修复前高水位, 修复后高水位)。"""
    diffs = np.array([before - after for _, before, after, _, _ in pairs])
    peaks = np.array([before - after for _, _, _, before, after in pairs])
    differ = int((np.abs(diffs) > 1e-12).sum())
    print(f"  {label}：{len(pairs)} 个周期，读数不同 {differ} 个")
    print(
        f"    修复前 − 修复后：最深 {diffs.min():+.2%}，最浅 {diffs.max():+.2%}，均值 {diffs.mean():+.2%}；"
        f"不同的周期里均值 {diffs[np.abs(diffs) > 1e-12].mean():+.2%}"
    )
    print(
        f"    高水位：修复前最多高 {peaks.max():,.2f} USDT，最少高 {peaks.min():,.2f} USDT，"
        f"偏高的周期 {int((peaks > 1e-9).sum())}，偏低的 {int((peaks < -1e-9).sum())}"
    )
    print(
        f"    最深读数：修复前 {min(p[1] for p in pairs):+.2%}，修复后 {min(p[2] for p in pairs):+.2%}；"
        f"最后一个周期 {pairs[-1][0]}：{pairs[-1][1]:+.2%} → {pairs[-1][2]:+.2%}"
    )


def replay(rows: list[dict[str, Any]], attribution: list[dict[str, Any]], before: Ruler, label: str) -> None:
    bars = [int(row["bar_open_ms"]) for row in rows]
    landing = w5_landing(rows)
    lands = [landing(income) for income in attribution]
    renumbered = [{**row, "bar_open_ms": i} for i, row in enumerate(rows)]
    recorded_hits = fixed_hits = compared = value_bits = peak_bits = 0
    worst_recorded = worst_value = worst_peak = 0.0
    pairs: list[tuple[str, float, float, float, float]] = []
    for n in range(1, len(rows)):
        recorded = (rows[n].get("risk_ladder") or {}).get("drawdown")
        if recorded is None:
            continue
        # 第 n 个周期的梯子在写第 n 行之前读：前 n 行，加上到它自己收进来的那条为止的收入
        seen = [income for income in attribution if int(income["bar_open_ms"]) <= bars[n - 1]]
        old = before(rows[:n], seen, PARAMS)
        new = attributed_drawdown_state(rows[:n], seen, PARAMS)
        moved = [{**a, "bar_open_ms": k} for a, k in zip(attribution, lands, strict=True) if k is not None and k < n]
        w5 = before(renumbered[:n], moved, PARAMS)
        if old.get("value") is None or new.get("value") is None or w5.get("value") is None:
            print(f"  跳过 {rows[n]['bar']}：修复前 {old.get('why')} / 修复后 {new.get('why')} / W5 {w5.get('why')}")
            continue
        compared += 1
        worst_recorded = max(worst_recorded, abs(float(old["value"]) - float(recorded)))
        recorded_hits += int(abs(float(old["value"]) - float(recorded)) <= 1e-12)
        fixed_hits += int(abs(float(new["value"]) - float(recorded)) <= 1e-12)
        value_bits += int(new["value"] == w5["value"])
        peak_bits += int(new["peak"] == w5["peak"])
        worst_value = max(worst_value, abs(float(new["value"]) - float(w5["value"])))
        worst_peak = max(worst_peak, abs(float(new["peak"]) - float(w5["peak"])))
        pairs.append((str(rows[n]["bar"]), float(old["value"]), float(new["value"]), float(old["peak"]), float(new["peak"])))
    print(f"\n== {label}：{rows[0]['bar']} .. {rows[-1]['bar']}，{len(rows)} 行，带读数的周期 {compared} 个")
    print(
        f"  一、修复前的函数复现记录的读数：{recorded_hits}/{compared}，max|差| {worst_recorded:.1e}；"
        f"修复后的函数复现的 {fixed_hits}/{compared}（重启换上修复之后，新周期应当全部由它复现）"
    )
    print(
        f"  二、修复后 vs W5 不重复口径：读数逐位相同 {value_bits}/{compared}（max|差| {worst_value:.1e}），"
        f"高水位逐位相同 {peak_bits}/{compared}（max|差| {worst_peak:.1e}）"
    )
    summarise("三、修复前后", pairs)


def landings(rows: list[dict[str, Any]], attribution: list[dict[str, Any]]) -> None:
    """四、每条收入落在哪一行，修复后的函数对 W5。"""
    rebased = [i for i, row in enumerate(rows) if (row.get("external_flows") or {}).get("rebaselined")]
    last_baseline = max(rebased, default=0)
    differ = []
    for j, income in enumerate(attribution):
        mine, theirs = fixed_landing(rows, income), w5_raw_landing(rows, income)
        if mine != theirs:
            differ.append((j, income, mine, theirs))
    print(f"\n== 四、落点：{len(attribution)} 条收入，修复后与 W5 落在不同行的 {len(differ)} 条（最后一次 rebaseline 在第 {last_baseline} 行）")
    for j, income, mine, theirs in differ:
        side = "之前" if max(mine or 0, theirs or 0) <= last_baseline else "之后"
        tie = next((i for i in range(len(rows)) if rows[i]["at"] == income["at"] and priced(rows[i])), None)
        stamp = datetime.fromtimestamp(income["bar_open_ms"] / 1000, tz=UTC).strftime("%m-%dT%H:%MZ")
        print(
            f"  第 {j} 条 {income['at']}，记在 {stamp}，{float(income['total']):+.2f}：修复后第 {mine} 行，"
            f"W5 第 {theirs} 行；同秒写入的定价行 {tie}；在最后一次 rebaseline {side}"
        )
    multi: dict[int, int] = {}
    for row in rows:
        if priced(row):
            multi[int(row["bar_open_ms"])] = multi.get(int(row["bar_open_ms"]), 0) + 1
    repeated = {bar: count for bar, count in multi.items() if count > 1}
    unpriced = [row.get("phase") for row in rows if not priced(row)]
    last = datetime.fromtimestamp(max(repeated, default=0) / 1000, tz=UTC).isoformat()
    print(
        f"  记录里：不带 equity 的行 {len(unpriced)}（SKIPPED {unpriced.count('SKIPPED')}、ERROR {unpriced.count('ERROR')}）；"
        f"有多个定价行的 bar {len(repeated)} 个（最多 {max(repeated.values(), default=0)} 行，最后一个 {last}）"
    )


def realisation(before: Ruler) -> None:
    """五、2026-09-13 的「平仓不动读数」检验，按引擎的记账方式重做。

    `scratchpad/r8_is_realisation_invariant.py` 把平仓那笔收入记在「浮亏已经消失」的那一根 bar 上。
    引擎不是这样记的：第 1 个周期下单平掉，下一个周期收进来，按 `state.last_bar_ms` 记在第 1 根；第 2 行
    的快照里浮亏已经没了。这里按引擎的方式造两个世界，修复前后各读一遍。
    """
    bar, start, u = 3_600_000, 1_789_000_000_000, -400.0

    def world(realise: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cycles = [
            {"at": f"2026-09-01T0{i + 1}:00:31+00:00", "bar_open_ms": start + i * bar, "equity": 10_000.0 + (u if i else 0.0)}
            | {"unrealized": 0.0 if i == 0 else (0.0 if (i == 2 and realise) else u)}
            for i in range(3)
        ]
        # 第 1 个周期下单平掉 -> 第 2 个周期 02:00:26 收进来，记在第 1 根（`state.last_bar_ms`）
        booked = [{"at": "2026-09-01T03:00:26+00:00", "bar_open_ms": start + bar, "total": u}] if realise else []
        return cycles, [{"at": "2026-09-01T02:00:26+00:00", "bar_open_ms": start, "total": 0.0}, *booked]

    print("\n== 五、平仓不动读数（按引擎记账方式重做 2026-09-13 的检验）")
    for name, ruler in (("修复前", before), ("修复后", attributed_drawdown_state)):
        for when, upto in (("第 2 个周期的梯子（读前 2 行）", 2), ("第 2 行写入之后", 3)):
            held, flat = (
                ruler(world(realise)[0][:upto], world(realise)[1], PARAMS) for realise in (False, True)
            )
            print(
                f"  {name}，{when}：持有 {held['value']:+.4f}（最深 {held['max_drawdown']:+.4f}），"
                f"平掉 {flat['value']:+.4f}（最深 {flat['max_drawdown']:+.4f}）；逐位相同 "
                f"{held['value'] == flat['value'] and held['max_drawdown'] == flat['max_drawdown']}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", type=Path, required=True, help="实盘 cycles.jsonl 与 attribution.jsonl 的只读副本目录")
    args = parser.parse_args()
    cycles_path, attribution_path = args.live / "cycles.jsonl", args.live / "attribution.jsonl"
    for path in (cycles_path, attribution_path):
        print(f"{path.name}: sha256 {hashlib.sha256(path.read_bytes()).hexdigest()}")
    import beidou_live.risk_budget as fixed

    print(f"修复后的函数从 {fixed.__file__} 加载；修复前的从 {BEFORE}:beidou_live/risk_budget.py 执行")
    rows, attribution = read_jsonl(cycles_path), read_jsonl(attribution_path)
    before = ruler_before()
    pin = int(datetime.fromisoformat(LIVE_PIN).timestamp() * 1000)
    replay([row for row in rows if int(row["bar_open_ms"]) <= pin], attribution, before, f"钉到 {LIVE_PIN}（W5 对账）")
    replay(rows, attribution, before, "副本全长")
    landings(rows, attribution)
    realisation(before)
    report_rows = [row for row in rows if row.get("equity") is not None and not row.get("dry_run")]
    for name, view in (("引擎口径（全部行）", rows), ("日报口径（`_cycles`）", report_rows)):
        block = attributed_drawdown_state(view, attribution, PARAMS)
        print(
            f"\n== 六、{name}，副本最后一行之后的读数：{block['value']:+.4%}，高水位 {block['peak']:,.2f}，"
            f"最深 {block['max_drawdown']:+.4%}；收入 在路径上 {block['rows']}、在 base 里 {block['in_base_rows']}、"
            f"pending {block['pending_rows']}（{block['pending_pnl']:+.2f}）"
        )


if __name__ == "__main__":
    main()
