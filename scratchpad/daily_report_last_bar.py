"""归档日报缺每天 23:00 那根 bar：逐日对账（2026-09-28）。

来由：GAP-PR10 那节的「顺带发现」。那一节推断：巡检（`deploy/run_check.sh`，整点 :10）每小时跑一次
`report daily --check`，只渲染当天（UTC）；D 日的文件最后写于 D 日 23:10Z，而 D 日 23:00 那根 bar 的
周期行写于 D+1 日 00:00 之后。日报按 `_day_of`（`as_of_ms`，数据自带的 bar）归日，所以那根 bar 只属于
D，而 D 的文件再也不会重写。

**怎么数。** 全部只读：状态是 `.beidou/live` 在两个周期之间的副本，归档日报是 `reports/daily/*.json`
（`config/live.demo.yaml` 的 `paths.reports_dir` 下），写入时刻取文件 mtime。不逐行读 `cycles.jsonl`
判 bar 结局：取行走日报自己的入口 `StateStore.read_jsonl`，归日走 `_day_of`，汇总照 `daily_payload`
的同几行（周期数、首末权益、`one_row_per_order`、归因行按 `by_strategy` 求和、`FUNDING_FEE` 求和），
M-Q03 走 `restart_cost`。每个整天在四个时刻各算一次：

- `archived`：只留 `at` <= 归档文件 mtime 的行，即归档那一刻日报看得见的；
- `00:10`：`at` <= D+1 日 00:10:59Z，即「00:10Z 补渲染前一天」能看见的；
- `01:10`：`at` <= D+1 日 01:10:59Z；
- `all`：快照里的全部行。

`archived` 一列要与归档文件本身的读数逐项相同，不同就印 MISMATCH——它是这一整套截断能代表归档的证据。
另印一行「按写入时刻 `at` 归日」时 23:10Z 那次渲染会读到的数，给另一种修法（改口径）定价用。

**2026-09-28 的读数**（快照 668 行，末行 09-28T12:00:29Z；归档 09-03 至 09-28 共 26 份）：

- 24 个整天（09-04 至 09-27）的文件全部最后写于 D 日 23:10:05–23:11:02Z。`archived` 与归档文件的
  周期数、`restarts.cycles`、首末权益、权益变化、归因 P&L、资金费 24/24 天相同，MISMATCH 0。
- 每个整天恰好缺 1 行周期行，24/24 都是 23:00 那根，写于 D+1 日 00:00:09–00:00:40Z，全部成功周期。
  缺的内容：1 笔成交（09-16，ENAUSDT），12 条 COOLDOWN 退出事件（0 条 TAKE_PROFIT），0 次 pool 变动，
  0 个 bar_sanity flag，396 条 plan gap。
- 归因行按「持仓那本书的 bar」归日，写入却晚两小时（bar X 的行写于 X+2h）。所以归档还缺 7 天各 1 行
  归因：5 行写于 D+1 日 00:00:2x，2 行写于 01:00:2x（09-16 的 23:00 书；09-27 的 00:00 资金费结算，
  `late_funding`）。
- 已经读到整天的天数（周期数、成交数、末权益、归因 P&L、资金费五项全等）：`archived` 0/24，
  `00:10` 22/24，`01:10` 24/24。
- 整天减归档：权益变化 24/24 天不同，中位 0.136 个百分点，最大 1.280（09-16）；归因 P&L 7/24 天不同，
  最大 29.147 U（09-11）。
- 三个读数反号：09-11 归因 P&L 归档 +15.24、整天 −13.91（22:00 那本书那一行 −29.15，其中已实现
  盈亏 −29.04，09-12T00:00:08 写入）；09-11 权益变化 −0.124% 对 +0.182%；09-26 权益变化 −0.104% 对 +0.182%。
- 按 `at` 归日：09-11 读 +15.24，那一行 −29.15 落进 09-12（−0.03 → −29.17）。

**渲染核对**（用 `scratchpad/reports_split_byte_identity.py render`，不跑 `report daily`，不发 webhook）：
对 09-11、09-16、09-27 各切三份状态（归档 mtime、00:10:59、01:10:59），各渲染一次，比同一天的 JSON。
归档那份的周期数、成交数、成交额、归因 P&L、已实现盈亏、手续费、资金费、首末权益、`failed_bars`、
`fills_measured` 与归档文件逐项相同（09-11 的归档早于 `failed_bars` 字段，那一项不比）。代码是
`1b8da65c`。逐叶比较，变化的顶层键数（归档→00:10 / 00:10→01:10）：09-11 是 32 / 10，09-16 是 29 / 21，
09-27 是 25 / 19。

复现（在两个周期之间复制状态，整点后 5 分钟到下一个整点前 10 分钟；`<wt>` 是 worktree，`<s>` 是 scratch）：

    cp -Rp .beidou/live <s>/state
    PYTHONPATH=<wt> .venv/bin/python scratchpad/daily_report_last_bar.py count <s>/state reports/daily
    # 渲染核对：切一份状态，再用 render 出同一天的日报
    PYTHONPATH=<wt> .venv/bin/python scratchpad/daily_report_last_bar.py cut <s>/state 2026-09-27T23:10:11+00:00 <s>/cut-a
    PYTHONPATH=<wt> .venv/bin/python scratchpad/reports_split_byte_identity.py render <s>/cut-a .beidou/data <s>/out-a
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from beidou_live.report_common import _day_of
from beidou_live.report_execution import restart_cost
from beidou_live.risk_budget import RiskBudgetParams, one_row_per_order
from beidou_live.state import StateStore

CUTS = ("archived", "00:10", "01:10", "all")


def _at(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["at"]))


def _hhmm(row: dict[str, Any]) -> str:
    ms = row.get("as_of_ms") or row.get("bar_open_ms")
    return datetime.fromtimestamp(float(ms) / 1000, tz=UTC).strftime("%H:%M") if ms else "?"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3%}"


def _readings(
    cycles: Sequence[dict[str, Any]], trades: Sequence[dict[str, Any]], attributions: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The day-scoped readings `daily_payload` computes from the day's rows, by the same lines."""
    equities = [float(row["equity"]) for row in cycles if row.get("equity") is not None]
    pnl = sum(float(v) for row in attributions for v in (row.get("by_strategy") or {}).values())
    buckets = [bucket for row in attributions for bucket in (row.get("by_symbol") or {}).values()]
    funding = sum(float(bucket.get("FUNDING_FEE", 0.0)) for bucket in buckets)
    return {
        "cycles": len(cycles),
        "restarts_cycles": restart_cost(cycles, trades, RiskBudgetParams())["cycles"],
        "equity_start": equities[0] if equities else None,
        "equity_end": equities[-1] if equities else None,
        "equity_change_pct": (equities[-1] / equities[0] - 1.0) if len(equities) >= 2 and equities[0] else None,
        "orders": len(one_row_per_order(trades)),
        "pnl": pnl,
        "funding": funding,
        "attribution_rows": len(attributions),
    }


def _archived(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycles": payload.get("cycles"),
        "restarts_cycles": (payload.get("restarts") or {}).get("cycles"),
        "equity_start": payload.get("equity_start"),
        "equity_end": payload.get("equity_end"),
        "equity_change_pct": payload.get("equity_change_pct"),
        "pnl": sum(float(v) for v in (payload.get("pnl_by_strategy") or {}).values()),
        "funding": payload.get("funding"),
    }


def count(state_dir: str, reports_dir: str) -> None:
    store = StateStore(Path(state_dir))
    rows = {
        "cycles": store.read_jsonl(store.cycles_path),
        "trades": store.read_jsonl(store.trades_path),
        "attributions": store.read_jsonl(store.attribution_path),
    }
    print(f"snapshot: {len(rows['cycles'])} cycle rows, last at {max(_at(r) for r in rows['cycles']).isoformat()}")
    totals = {"days": 0, "late_cycle_rows": 0, "late_failed": 0, "late_orders": 0, "late_attribution": 0}
    flips: list[str] = []
    # Per cut, the days on which it already reads what the whole day reads, and how far the archive sits off.
    complete = dict.fromkeys(CUTS, 0)
    gaps: dict[str, list[float]] = {"equity_change_pp": [], "pnl_u": []}
    for path in sorted(Path(reports_dir).glob("*.json")):
        day = path.stem
        written = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if written.strftime("%Y-%m-%d") != day or written.hour != 23:
            print(f"{day}: written {written:%m-%dT%H:%M:%SZ}, not closed by the 23:10Z check - skipped")
            continue
        totals["days"] += 1
        nxt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
        limits = {
            "archived": written,
            "00:10": nxt + timedelta(minutes=10, seconds=59),
            "01:10": nxt + timedelta(hours=1, minutes=10, seconds=59),
            "all": datetime.max.replace(tzinfo=UTC),
        }
        daily = {name: [r for r in found if _day_of(r) == day] for name, found in rows.items()}
        cut = {
            name: _readings(*([r for r in daily[k] if _at(r) <= limit] for k in ("cycles", "trades", "attributions")))
            for name, limit in limits.items()
        }
        # What the same 23:10Z render would read if rows were bucketed by when they were WRITTEN (`at`)
        # instead of by `_day_of` - the other fix on the table, which changes what a day means.
        by_write = _readings(
            *([r for r in found if str(r["at"])[:10] == day and _at(r) <= written] for found in rows.values())
        )
        archived = _archived(json.loads(path.read_text(encoding="utf-8")))
        mismatch = [
            key
            for key, value in archived.items()
            if value is not None and not (abs(float(value) - float(cut["archived"][key] or 0.0)) <= 1e-9)
        ]
        late_cycles = [r for r in daily["cycles"] if _at(r) > written]
        late_attr = [r for r in daily["attributions"] if _at(r) > written]
        totals["late_cycle_rows"] += len(late_cycles)
        totals["late_failed"] += sum(1 for r in late_cycles if r.get("phase") == "ERROR")
        totals["late_orders"] += cut["all"]["orders"] - cut["archived"]["orders"]
        totals["late_attribution"] += len(late_attr)
        late = [(_hhmm(r), r.get("phase") or "OK", f"{_at(r):%m-%dT%H:%M:%S}") for r in late_cycles]
        print(
            f"{day} written {written:%H:%M:%SZ} | cycles {' / '.join(str(cut[c]['cycles']) for c in CUTS)}"
            f" | late cycle rows {late}" + (f" | MISMATCH {mismatch}" if mismatch else " | archived == archive file")
        )
        pnl = " / ".join(f"{cut[c]['pnl']:+.2f}" for c in CUTS)
        chg = " / ".join(_pct(cut[c]["equity_change_pct"]) for c in CUTS)
        fund = " / ".join(f"{cut[c]['funding']:+.4f}" for c in CUTS)
        print(f"      pnl {pnl} | equity change {chg} | funding {fund}")
        print(
            f"      by write time at 23:10Z: cycles {by_write['cycles']} pnl {by_write['pnl']:+.2f}"
            f" equity change {_pct(by_write['equity_change_pct'])} funding {by_write['funding']:+.4f}"
        )
        if late_attr:
            late = [(_hhmm(r), f"{_at(r):%m-%dT%H:%M:%S}", bool(r.get("late_funding"))) for r in late_attr]
            print(f"      late attribution rows {late}")
        for key in ("pnl", "equity_change_pct"):
            before, after = cut["archived"][key], cut["all"][key]
            if before is not None and after is not None and before * after < 0:
                flips.append(f"{day} {key}: archived {before:+.6g} vs whole day {after:+.6g}")
        for name in CUTS:
            if all(cut[name][k] == cut["all"][k] for k in ("cycles", "orders", "equity_end", "pnl", "funding")):
                complete[name] += 1
        before, after = cut["archived"]["equity_change_pct"], cut["all"]["equity_change_pct"]
        if before is not None and after is not None:
            gaps["equity_change_pp"].append(100.0 * abs(after - before))
        gaps["pnl_u"].append(abs(cut["all"]["pnl"] - cut["archived"]["pnl"]))
    print(json.dumps(totals))
    print("days on which the cut already reads the whole day (cycles, orders, equity_end, pnl, funding):", complete)
    for key, values in gaps.items():
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        moved = sum(1 for v in values if v > 1e-9)
        print(
            f"|whole day - archived| {key}: median {median:.3f}, max {ordered[-1]:.3f}, days > 0: {moved}/{len(values)}"
        )
    print("sign flips:", flips or "none")


def cut(state_dir: str, cutoff: str, target: str) -> None:
    """Copy a state directory, keeping only JSONL lines whose `at` <= cutoff; bytes kept as written.

    state.json and heartbeat.json are copied as they are - they cannot be rewound - so two cuts of one day
    share them and a diff between the two renders is a diff of rows only.
    """
    limit = datetime.fromisoformat(cutoff)
    src, dst = Path(state_dir), Path(target)
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.iterdir()):
        if path.suffix != ".jsonl":
            shutil.copy2(path, dst / path.name)
            continue
        kept = dropped = 0
        with path.open("rb") as source, (dst / path.name).open("wb") as sink:
            for raw in source:
                try:
                    stamp = datetime.fromisoformat(str(json.loads(raw)["at"]))
                except (ValueError, KeyError, TypeError):
                    stamp = None
                if stamp is None or stamp <= limit:
                    sink.write(raw)
                    kept += 1
                else:
                    dropped += 1
        print(f"{path.name}: kept {kept}, dropped {dropped}")


if __name__ == "__main__":
    {"count": count, "cut": cut}[sys.argv[1]](*sys.argv[2:])
