"""GAP-PR10：失败周期在再平衡窗口内重试一次，能救回几根 bar。零 ledger、只读、不连交易所。

执行手册 §3.13（`docs/analysis/2026-09-28-production-refactor-execution-plan.md`）预写的阈值：
**≥ 4/7 可救 → 值得写「窗口内重试」的预登记**（改实盘行为，另一次裁定；本脚本不写预登记）。

**数哪些 bar。** 不逐行读 `cycles.jsonl` 判结局：同一根 bar 可能有好几行（失败行、重启补的 SKIPPED 行），
按行读会让后一行覆盖前一行（09-23 G9 的事故）。这里走日报自己的入口：`StateStore.read_jsonl` 取行、
`_day_of` 分日、`restart_cost(rows)["failed_bars"]` 数失败 bar。`restart_cost` 只返回个数，所以逐 bar
的清单由 `failed_bars()` 按它的同一套规则（`_decided`、两种 SKIPPED 原因）算出，并逐日与它的个数对账，
对不上就停。另与归档的日报 JSON 逐日对一遍，差异逐条说明。

**再平衡窗口。** `rebalance_window_seconds = grace_seconds + throttle_interval_seconds + startup_seconds`
（`beidou_live/scheduler.py`）：「重启晚到多久仍可以对错过的那根 bar 再平衡」。它在周期开始时判（`engine.py`
的 `within_rebalance_window`），每个进程的值写在它的 ERROR 行上（`window_seconds`）。日报的
`widest_rebalance_window_seconds` 是当天各行取最大，09-27 的 90.656 就是手册引的「90.66 s」。这里每根 bar
用它自己那行的值。「失败后 T 秒重试一次」要在窗口内开始：`失败时刻 − 收盘 + T ≤ window_seconds`。

**失败前路径已经坏了多久。** 周期在收盘后 `grace_seconds`（20 s）开始，第一件事是拉行情（fapi，公共客户端）。
公共客户端 `AsyncPublicClient` 5 次尝试、间隔 1/2/4/8 s，最后一次失败把 `ProxyError` 原样抛出；签名客户端
`BinanceRestClient`（demo-fapi）4 次尝试、间隔 0.5/1/2 s，把传输错误包成 `VenueError`。所以 `ProxyError`
行是行情那一侧、`VenueError` 行是交易那一侧（09-15 那一次的 traceback 落在 `binance_public.py` 的 `klines`，
见 RESEARCH_LOG「2026-09-15 · 08:00Z 周期失败查清」）。代理探针 `explicit` 臂失败样本的耗时中位 5.08 s（代理大约
5 s 才回失败），于是一次 ProxyError 失败 ≈ 15 s 间隔 + 5 × 5.08 s ≈ 40 s，从 +20 s 起算落在 +60 s 上下——
6 条 ProxyError 行全落在收盘后 +61/+62 s，与此吻合：失败的就是周期的第一个请求，路径至少从 +20 s 坏到
+61 s。VenueError ≈ 3.5 s + 4 × 5.08 s ≈ 24 s。这两个数（`elapsed`）是推断，不是记录；分布估计以它为条件。

**证据，按强弱。**

1. 代理探针（`proxy-probe.jsonl`，2026-09-15T17:16Z 至 09-22T05:03Z，每个 host × arm 约 63 s 一次，读法见
   RESEARCH_LOG 09-22 那节；`"http":000` 坏行先修再解析）。循环走显式代理，所以判读只用 `explicit` 臂、
   用失败那一侧的 host。重试时刻 `r = 失败时刻 + T`：r 之前最近的证据（循环自己的失败，或代理探针样本）与 r 之后
   最近的代理探针样本都失败 → 「两侧失败，救不回」；都成功 → 「可救」；一败一成 → 「不定」。代理探针只有分钟
   分辨率，两侧失败之间路径是否短暂恢复过看不见，所以这一条是推断，不是观测。
2. 同一根 bar 上另外两个循环（shadow 与 paper-l3，各自的 `cycles.jsonl` 副本）同一时刻的结局：说明失败是
   整条路径的，不是 live 这个进程的。它们与 live 同时开跑、同时重试，给不出「稍后成功」的证据。
3. 分布估计（覆盖不到的 bar 只有这一条）：代理探针 `explicit` 臂上按 host 切簇（相邻失败样本间隔 ≤ 150 s 算
   同一簇；前后都有成功样本夹住的簇才用），每簇长度只知道一个区间 `[L_lo, L_hi]`：`L_lo` = 首个失败样本
   开始到末个失败样本结束，`L_hi` = 前一个成功样本结束到后一个成功样本开始。假定循环撞进簇的时刻对簇
   是随机的，撞上的簇就按长度加权、落点在簇内均匀。已知已经坏了 `elapsed` 秒，T 秒后已恢复的概率是
   `Σ max(0, min(T, L − elapsed)) / Σ max(0, L − elapsed)`，分别代入 `L_lo` 与 `L_hi` 给一个区间。
   这个前提用「与整点后 [+20, +61] s 相交的簇」检验：三个循环同一秒开跑，若是它们自己引发了簇，相交的簇
   会多于随机摆放的期望。忽略 T 秒内新起一簇（代理探针期内 fapi 约 1.1 小时一簇）。

复现（worktree 根目录下；`--state` 是 `.beidou/live` 的快照副本，别指向活目录）：

    PYTHONPATH=$PWD /Users/maguannan/beidou/.venv/bin/python scratchpad/gap_pr10_retry_window.py \\
        --state <快照>/state --probe <副本>/proxy-probe.jsonl \\
        --sibling shadow=<副本>/shadow/cycles.jsonl --sibling paper-l3=<副本>/paper-l3/cycles.jsonl \\
        --daily <副本>/daily

2026-09-28 的读数（快照 `live-snapshot-0815`，末行 2026-09-28T08:00:28Z；09-15T00:00Z 起）：

- 失败 bar 7 根，逐日与 `restart_cost` 对上，整段一次读也是 7。归档日报合计 5：09-15 那份写于 `failed_bars`
  出现之前（6e40ad1d，09-16），09-23 那份写于 #132 之前、把那根记成了重启跳过。
- 6 根 ProxyError（行情侧，收盘后 +61/+62 s，窗口 86.2–87.6 s，余量 24.2–26.6 s），1 根 VenueError
  （positionRisk，交易侧，+47 s，余量 39.2 s）。当时在跑的另外两个循环在同一秒同样失败（shadow 5/5，
  paper-l3 在 6 根 ProxyError 上 6/6；VenueError 那根 paper-l3 不走 demo-fapi，+23 s 成功）。
- 代理探针覆盖 4 根：T = 10/20/30 s 时 4 根全部救不回；该 host `explicit` 臂此后首个成功在收盘后 +187、+230、
  +187、+236 s。
- 分布（fapi `explicit` 臂 141 簇）：已坏 40 s 之后 T = 10/20/30/60 s 内恢复的概率 0.07–0.09、0.14–0.18、
  0.21–0.27、0.41–0.43。与整点后 [+20, +61] s 相交的簇，实数对期望：fapi 2 对 3.7（L_lo）、8 对 8.9（L_hi），
  demo-fapi 3 对 3.5、4 对 7.9，没有多出来。
- 窗口内合计可救：T = 10 s 0.21–0.27 根，20 s 0.41–0.55 根，30 s 与 60 s 为 0（窗口里开不了头），逐 bar 取
  窗口最后一刻 0.53–0.70 根；不看窗口、T = 60 s 也只有 1.24–2.29 根。阈值 4/7 不成立。

完整表与判读见 `docs/RESEARCH_LOG.md`「2026-09-28 · GAP-PR08 / GAP-PR10」一节。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import beidou_live
from beidou_live.engine import LiveConfig
from beidou_live.report_common import _day_of
from beidou_live.report_execution import restart_cost
from beidou_live.scheduler import ALREADY_REBALANCED_REASON, MISSED_REBALANCE_REASON
from beidou_live.soak import _decided
from beidou_live.state import StateStore

HOUR_MS = 3_600_000
GRACE_SECONDS = LiveConfig.__dataclass_fields__["grace_seconds"].default
# 重试节奏：`beidou_data/binance_public.py` 的 AsyncPublicClient（max_retries=4，delay 1.0 翻倍）与
# `beidou_exchange/binance_usdm/rest_client.py` 的 BinanceRestClient（max_retries=3，delay 0.5 翻倍）。
RETRY_SCHEDULE = {"fapi.binance.com": (5, 1.0 + 2.0 + 4.0 + 8.0), "demo-fapi.binance.com": (4, 0.5 + 1.0 + 2.0)}
CLUSTER_GAP_SECONDS = 150.0
BAD_HTTP = re.compile(r'"http":0+(?=[,}])')


def stamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%m-%d %H:%M:%S")


def failed_bars(rows: Sequence[Mapping[str, Any]]) -> set[int]:
    """`restart_cost` 里 `failed = lost - reached` 的逐 bar 版本，规则逐条照抄，个数由调用方与它对账。"""
    lost: set[int] = set()
    reached: set[int] = set()
    for row in rows:
        reason = row.get("reason")
        bar = row.get("bar_open_ms")
        if row.get("phase") == "SKIPPED" or reason == MISSED_REBALANCE_REASON:
            if reason == ALREADY_REBALANCED_REASON and isinstance(bar, int):
                reached.add(bar)
            continue
        if isinstance(bar, int):
            if row.get("phase") == "ERROR" and not _decided(row):
                lost.add(bar)
            else:
                reached.add(bar)
    return lost - reached


def category(error: str) -> tuple[str, str]:
    """错误类别（按 error 字段原文的异常类名）与它失败在哪个 host。"""
    kind = error.split(":", 1)[0].strip()
    if kind == "ProxyError":
        return ("代理 503（ProxyError，行情侧）" if "503" in error else "代理错误（ProxyError）"), "fapi.binance.com"
    if kind == "VenueError":
        label = "venue 错误（VenueError，交易侧；原文含 503）" if "503" in error else "venue 错误（VenueError）"
        return label, "demo-fapi.binance.com"
    return f"其它（{kind}）", "fapi.binance.com"


@dataclass
class Sample:
    at: float
    ok: bool
    seconds: float


def load_probe(path: Path) -> dict[tuple[str, str], list[Sample]]:
    series: dict[tuple[str, str], list[Sample]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(BAD_HTTP.sub('"http":0', line))
        ok = row.get("curl_rc") == 0 and row.get("http") == 200
        series.setdefault((row["host"], row["arm"]), []).append(Sample(stamp(row["at"]), ok, float(row["seconds"])))
    for samples in series.values():
        samples.sort(key=lambda s: s.at)
    return series


@dataclass
class Cluster:
    """前后都被成功样本夹住的一串失败样本。簇长只知道一个区间：`low` 到 `high`。"""

    first: float  # 首个失败样本开始
    last_end: float  # 末个失败样本结束
    before_end: float  # 前一个成功样本结束
    after: float  # 后一个成功样本开始
    count: int

    @property
    def low(self) -> float:
        return self.last_end - self.first

    @property
    def high(self) -> float:
        return self.after - self.before_end


def clusters(samples: list[Sample]) -> list[Cluster]:
    out: list[Cluster] = []
    i = 0
    while i < len(samples):
        if samples[i].ok:
            i += 1
            continue
        j = i
        while (
            j + 1 < len(samples) and not samples[j + 1].ok and samples[j + 1].at - samples[j].at <= CLUSTER_GAP_SECONDS
        ):
            j += 1
        before = samples[i - 1] if i > 0 else None
        after = samples[j + 1] if j + 1 < len(samples) else None
        if (
            before is not None
            and after is not None
            and before.ok
            and after.ok
            and samples[i].at - before.at <= CLUSTER_GAP_SECONDS
            and after.at - samples[j].at <= CLUSTER_GAP_SECONDS
        ):
            out.append(
                Cluster(
                    samples[i].at, samples[j].at + samples[j].seconds, before.at + before.seconds, after.at, j - i + 1
                )
            )
        i = j + 1
    return out


def rescue_probability(lengths: Sequence[float], elapsed: float, retry: float) -> float | None:
    """已经坏了 `elapsed` 秒，`retry` 秒后已恢复的概率。

    循环撞进簇的时刻对簇是随机的：撞上的簇按长度加权，落点在簇内均匀。这个前提由下面「与整点后
    [+20, +61] s 相交的簇」那张表检验。另一种撞法——簇恰好由周期开跑引发、从那一刻开始——在 63 s 的
    代理探针分辨率下算不出来：簇长只取几个离散值，条件分布在 T < 63 s 上全是 0，那是分辨率，不是读数。
    """
    numerator = sum(max(0.0, min(retry, length - elapsed)) for length in lengths)
    denominator = sum(max(0.0, length - elapsed) for length in lengths)
    return numerator / denominator if denominator else None


def rescue_range(found: Sequence[Cluster], elapsed: float, retry: float) -> tuple[float, float]:
    """两种簇长口径（`low` 与 `high`）各算一次，取最小与最大。"""
    values = [
        rescue_probability(lengths, elapsed, retry) for lengths in ([c.low for c in found], [c.high for c in found])
    ]
    known = [v for v in values if v is not None]
    return (min(known), max(known)) if known else (0.0, 0.0)


def overlaps_cycle_start(start: float, end: float, window: tuple[float, float]) -> bool:
    """[start, end] 与某个整点后 [window[0], window[1]] 秒的区间相交。"""
    hour = int(start // 3600) * 3600
    while hour <= end:
        if start <= hour + window[1] and end >= hour + window[0]:
            return True
        hour += 3600
    return False


def direct_evidence(explicit: list[Sample], failed_at: float, retry_at: float, cover: tuple[float, float]) -> str:
    """重试时刻两侧最近的证据。循环自己在 `failed_at` 的失败算作 r 之前的一条失败证据。"""
    if not cover[0] <= retry_at <= cover[1]:
        return "代理探针未覆盖"
    before = [s for s in explicit if s.at <= retry_at and s.at >= failed_at]
    after = [s for s in explicit if s.at > retry_at]
    left_ok = before[-1].ok if before else False  # 没有更近的代理探针样本时，最近的是循环自己的失败
    if not after or after[0].at - retry_at > CLUSTER_GAP_SECONDS:
        return "代理探针未覆盖"
    right = after[0]
    # r 落在一个样本自己的请求时段里：那个样本就是 r 时刻的观测
    if before and retry_at <= before[-1].at + before[-1].seconds:
        return f"可救（{iso(before[-1].at)} 成功）" if before[-1].ok else f"救不回（{iso(before[-1].at)} 失败）"
    if not left_ok and not right.ok:
        return f"两侧失败，救不回（下一代理探针 {iso(right.at)} 失败）"
    if left_ok and right.ok:
        return "两侧成功，可救"
    return f"不定（下一代理探针 {iso(right.at)} {'成功' if right.ok else '失败'}）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True, help="`.beidou/live` 的快照副本")
    parser.add_argument("--since", default="2026-09-15", help="UTC 日，含当天")
    parser.add_argument("--probe", type=Path, default=None)
    parser.add_argument("--sibling", action="append", default=[], help="name=path，另一个循环的 cycles.jsonl 副本")
    parser.add_argument("--daily", type=Path, default=None, help="归档日报 JSON 的副本目录，用来对账")
    parser.add_argument("--retry", type=float, nargs="+", default=[10.0, 20.0, 30.0, 60.0])
    args = parser.parse_args()

    print(f"beidou_live 从 {Path(beidou_live.__file__).parent} 导入；grace_seconds = {GRACE_SECONDS}")
    store = StateStore(args.state)
    rows = store.read_jsonl(store.cycles_path)
    last = rows[-1].get("at")
    first_day = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
    last_day = datetime.fromisoformat(str(last)).astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [(first_day + timedelta(days=n)).strftime("%Y-%m-%d") for n in range((last_day - first_day).days + 1)]
    print(f"{store.cycles_path}：{len(rows)} 行，末行 {last}")

    # --- 1. 数失败 bar：与 restart_cost 逐日对账，再与归档日报逐日对一遍 -------------------------
    print(
        "\n| 日 | 快照行数 | failed_bars（现行代码，快照） | 逐 bar 清单 | 归档日报 failed_bars | 归档 missed / skipped | 归档 cycles |"
    )
    print("| --- | ---: | ---: | --- | ---: | --- | ---: |")
    bars: list[int] = []
    for day in days:
        day_rows = [row for row in rows if _day_of(row) == day]
        counted = restart_cost(day_rows)["failed_bars"]
        mine = sorted(failed_bars(day_rows))
        if len(mine) != counted:
            print(f"停：{day} 逐 bar 清单 {len(mine)} 根，restart_cost 读 {counted} 根")
            return 1
        bars.extend(mine)
        archived: dict[str, Any] = {}
        if args.daily is not None and (args.daily / f"{day}.json").exists():
            archived = json.loads((args.daily / f"{day}.json").read_text(encoding="utf-8")).get("restarts") or {}
        listed = ", ".join(datetime.fromtimestamp(b / 1000, tz=UTC).strftime("%H:%M") for b in mine) or "—"
        print(
            f"| {day} | {len(day_rows)} | {counted} | {listed} | {archived.get('failed_bars', '（无此字段）')} "
            f"| {archived.get('missed_rebalances', '—')} / {archived.get('skipped_bars', '—')} "
            f"| {archived.get('cycles', '—')} |"
        )
    span = [row for row in rows if (_day_of(row) or "") >= args.since]
    whole = restart_cost(span)["failed_bars"]
    print(f"整段一次读（{args.since} 起 {len(span)} 行）：restart_cost failed_bars = {whole}；逐日合计 {len(bars)}")
    if whole != len(bars):
        print("停：整段与逐日合计对不上")
        return 1

    # --- 2. 每根失败 bar 的时刻、类别、窗口 --------------------------------------------------------
    siblings: dict[str, list[dict[str, Any]]] = {}
    for spec in args.sibling:
        name, _, path = spec.partition("=")
        siblings[name] = StateStore(Path(path).parent).read_jsonl(Path(path))
    probe = load_probe(args.probe) if args.probe is not None else {}
    events = []
    for bar in sorted(bars):
        errors = [row for row in rows if row.get("bar_open_ms") == bar and row.get("phase") == "ERROR"]
        row = errors[0]
        close = (bar + HOUR_MS) / 1000.0
        failed_at = stamp(str(row["at"]))
        kind, host = category(str(row.get("error") or ""))
        attempts, sleeps = RETRY_SCHEDULE[host]
        events.append(
            {
                "bar": bar,
                "close": close,
                "failed_at": failed_at,
                "after_close": failed_at - close,
                "window": float(row["window_seconds"]),
                "error": str(row.get("error")),
                "kind": kind,
                "host": host,
                "error_rows": len(errors),
                "attempts": attempts,
                "sleeps": sleeps,
                "others": {
                    name: [
                        f"{r.get('phase') or 'OK'} {str(r.get('at'))[11:19]} {str(r.get('error') or '')[:24]}".strip()
                        for r in sibling
                        if r.get("bar_open_ms") == bar
                    ]
                    for name, sibling in siblings.items()
                },
            }
        )

    latency = (
        statistics.median(
            s.seconds for key, series in probe.items() if key[1] == "explicit" for s in series if not s.ok
        )
        if probe
        else 5.0
    )
    print(f"\n代理探针失败样本（explicit 臂）耗时中位 {latency:.2f} s，用来推断循环失败前路径已坏了多久")
    print("| bar（UTC） | 失败时刻 | 收盘后 | 类别 | 窗口 | 窗口余量 | 已坏（推断） | 同一 bar 的另外两个循环 |")
    print("| --- | --- | ---: | --- | ---: | ---: | ---: | --- |")
    for e in events:
        e["elapsed"] = e["sleeps"] + e["attempts"] * latency
        e["slack"] = e["window"] - e["after_close"]
        others = "；".join(f"{name}: {'/'.join(v) or '无行'}" for name, v in e["others"].items()) or "—"
        print(
            f"| {datetime.fromtimestamp(e['bar'] / 1000, tz=UTC):%m-%d %H:%M} | {iso(e['failed_at'])} "
            f"| +{e['after_close']:.0f} s | {e['kind']} | {e['window']:.3f} s | {e['slack']:.1f} s "
            f"| ≈{e['elapsed']:.0f} s | {others} |"
        )
    if any(e["error_rows"] > 1 for e in events):
        print("注意：有 bar 不止一条 ERROR 行，上表取第一条")

    # --- 3. 代理探针的直接证据 ---------------------------------------------------------------------------
    if probe:
        cover = (
            min(s.at for series in probe.values() for s in series),
            max(s.at for series in probe.values() for s in series),
        )
        print(f"\n代理探针覆盖 {iso(cover[0])} → {iso(cover[1])}（UTC）；判读只用 explicit 臂、失败那一侧的 host")
        print(
            "| bar | host | " + " | ".join(f"T={t:.0f} s" for t in args.retry) + " | 该 host explicit 臂此后首个成功 |"
        )
        print("| --- | --- | " + " | ".join("---" for _ in args.retry) + " | --- |")
        for e in events:
            explicit = probe.get((e["host"], "explicit"), [])
            cells = []
            for retry in args.retry:
                verdict = direct_evidence(explicit, e["failed_at"], e["failed_at"] + retry, cover)
                inside = e["after_close"] + retry <= e["window"]
                cells.append(verdict + ("" if inside else "；窗口外"))
            recovered = next((s for s in explicit if s.at > e["failed_at"] and s.ok), None)
            first_ok = (
                "—"
                if recovered is None or not cover[0] <= e["failed_at"] <= cover[1]
                else f"{iso(recovered.at)}（收盘后 +{recovered.at - e['close']:.0f} s）"
            )
            print(
                f"| {datetime.fromtimestamp(e['bar'] / 1000, tz=UTC):%m-%d %H:%M} | {e['host'].split('.')[0]} | "
                + " | ".join(cells)
                + f" | {first_ok} |"
            )
        print("\n失败时刻前后 5 分钟的代理探针样本（E = explicit，T = transparent；+ 成功，x 失败；秒数相对收盘）：")
        for e in events:
            if not cover[0] <= e["failed_at"] <= cover[1]:
                continue
            marks = []
            for arm, letter in (("explicit", "E"), ("transparent", "T")):
                for s in probe.get((e["host"], arm), []):
                    if e["close"] - 300 <= s.at <= e["close"] + 300:
                        marks.append((s.at, f"{letter}{'+' if s.ok else 'x'}{s.at - e['close']:+.0f}"))
            line = " ".join(mark for _, mark in sorted(marks))
            print(f"- {datetime.fromtimestamp(e['bar'] / 1000, tz=UTC):%m-%d %H:%M}（{e['host']}）：{line}")

        # --- 4. 分布估计 -----------------------------------------------------------------------------
        print("\n代理探针 explicit 臂的失败簇（前后都被成功样本夹住的）：")
        print(
            "| host | 簇数 | 失败样本数 1 / 2 / ≥3 | L_lo 中位 / 最大 | L_hi 中位 / 最大 | 失败样本占比 "
            "| 下一样本成功：前一个失败 / 前两个都失败 |"
        )
        print("| --- | ---: | --- | --- | --- | ---: | --- |")
        spans: dict[str, list[Cluster]] = {}
        for host in RETRY_SCHEDULE:
            explicit = probe.get((host, "explicit"), [])
            found = clusters(explicit)
            spans[host] = found
            sizes = [c.count for c in found]
            # 分辨率原生的对照：约 63 s 一步，不需要簇长的口径
            steps = [
                (a, b, c)
                for a, b, c in zip(explicit, explicit[1:], explicit[2:], strict=False)
                if c.at - b.at <= CLUSTER_GAP_SECONDS and b.at - a.at <= CLUSTER_GAP_SECONDS
            ]
            one = [c.ok for _, b, c in steps if not b.ok]
            two = [c.ok for a, b, c in steps if not a.ok and not b.ok]
            print(
                f"| {host.split('.')[0]} | {len(found)} | {sizes.count(1)} / {sizes.count(2)} / "
                f"{sum(1 for k in sizes if k >= 3)} | {statistics.median(c.low for c in found):.0f} s / "
                f"{max(c.low for c in found):.0f} s | {statistics.median(c.high for c in found):.0f} s / "
                f"{max(c.high for c in found):.0f} s | {sum(1 for s in explicit if not s.ok) / len(explicit):.2%} "
                f"| {sum(one)}/{len(one)} = {sum(one) / len(one):.2f} / {sum(two)}/{len(two)} = {sum(two) / len(two):.2f} |"
            )

        # 分布估计的前提之一：簇与周期开跑的时刻无关。三个循环同一秒开跑，若是它们自己引发了簇，
        # 撞上的簇就不按长度加权。按「与整点后 [+20, +61] s 相交的簇」数一遍，与簇随机摆放时的期望比。
        window = (GRACE_SECONDS, GRACE_SECONDS + 41.0)
        print(f"\n与整点后 [+{window[0]:.0f}, +{window[1]:.0f}] s 相交的簇：实数 / 随机摆放时的期望")
        print("| host | 按 L_lo 的区间 | 按 L_hi 的区间 |")
        print("| --- | --- | --- |")
        width = window[1] - window[0]
        for host, found in spans.items():
            narrow = sum(1 for c in found if overlaps_cycle_start(c.first, c.last_end, window))
            wide = sum(1 for c in found if overlaps_cycle_start(c.before_end, c.after, window))
            expect_narrow = sum(min(1.0, (width + c.low) / 3600.0) for c in found)
            expect_wide = sum(min(1.0, (width + c.high) / 3600.0) for c in found)
            print(f"| {host.split('.')[0]} | {narrow} / {expect_narrow:.1f} | {wide} / {expect_wide:.1f} |")

        print("\n已坏 `elapsed` 秒之后，T 秒内恢复的概率（不看窗口；区间 = 代入 L_lo / L_hi）：")
        print("| host | elapsed | " + " | ".join(f"T={t:.0f} s" for t in args.retry) + " |")
        print("| --- | ---: | " + " | ".join("---" for _ in args.retry) + " |")
        for host, (attempts, sleeps) in RETRY_SCHEDULE.items():
            elapsed = sleeps + attempts * latency
            cells = ["{:.2f}–{:.2f}".format(*rescue_range(spans[host], elapsed, retry)) for retry in args.retry]
            print(f"| {host.split('.')[0]} | {elapsed:.0f} s | " + " | ".join(cells) + " |")

        # --- 5. 汇总：各 T 下可救几根 ------------------------------------------------------------------
        def tally(label: str, offsets: list[float], window: bool) -> None:
            """一行汇总：每根 bar 在失败后 `offsets[i]` 秒重试一次；`window` 为真时窗口外的 bar 记作救不回。"""
            yes = no = unsure = feasible = 0
            low = high = 0.0
            for e, retry in zip(events, offsets, strict=True):
                if window and e["after_close"] + retry > e["window"]:
                    continue
                feasible += 1
                explicit = probe.get((e["host"], "explicit"), [])
                verdict = direct_evidence(explicit, e["failed_at"], e["failed_at"] + retry, cover)
                if verdict.startswith("代理探针未覆盖"):
                    a, b = rescue_range(spans[e["host"]], e["elapsed"], retry)
                    low, high = low + a, high + b
                elif verdict.startswith(("可救", "两侧成功")):
                    yes += 1
                elif "救不回" in verdict:
                    no += 1
                else:
                    unsure += 1
            print(
                f"| {label} | {feasible}/{len(events)} | {yes} / {no} / {unsure} | {low:.2f}–{high:.2f} "
                f"| {yes + low:.2f}–{yes + unsure + high:.2f} / {len(events)} |"
            )

        print(
            "\n| 重试时刻 | 能开始重试的 bar | 代理探针直接证据：可救 / 救不回 / 不定 "
            "| 未覆盖 bar 的期望可救（分布区间） | 合计可救（区间） |"
        )
        print("| --- | ---: | --- | --- | --- |")
        for retry in args.retry:
            tally(f"失败后 {retry:.0f} s，窗口内", [retry] * len(events), window=True)
        tally("窗口的最后一刻（逐 bar 取余量）", [e["slack"] for e in events], window=True)
        for retry in args.retry:
            tally(f"失败后 {retry:.0f} s，不看窗口", [retry] * len(events), window=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
