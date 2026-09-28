"""Whether the data under the book is the data its evidence was measured on.

Checklist area #9 of `docs/analysis/2026-09-23-external-prompt-checklist-vs-beidou.md`: the research
archive's coverage of what the loop traded, metrics same-source parity (DL-D4 / M-011), dataset
provenance (D-041), the nightly verdict on the tests that read the archive itself (WP-P4), and how much
of each candidate data family live has recorded against what its startup gate asks (WP-A1).  Bar
sanity (G6) lives in `bar_sanity.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_alpha.mining.expr import METRICS_COLUMNS
from beidou_alpha.mining.search import enumerate_candidates
from beidou_alpha.model import AlphaModel
from beidou_alpha.panel import interval_seconds
from beidou_alpha.signals import get_signal
from beidou_data.alignment import SPOT_BASIS_COLUMN
from beidou_data.metrics import PERIOD_MS
from beidou_data.metrics_snapshot import live_coverage_bars, metrics_parity
from beidou_data.store import KlineStore, MetricsStore
from beidou_governance.scheduler import parity_satisfied
from beidou_live import lock
from beidou_live.engine import metrics_refusal
from beidou_live.execution_fidelity import ReplayInputs
from beidou_live.inputs import required_history
from beidou_live.report_common import _cycles, readable_state
from beidou_live.state import LiveState, StateStore


def metrics_parity_status(symbols: Sequence[str], data_root: str | Path, *, now: datetime) -> dict[str, Any]:
    """M-011 across the managed universe: archive against snapshot, per symbol, folded to one number.

    Two refusals rather than one convenient number.  A symbol with no overlap contributes nothing and
    is counted separately - `metrics_parity` already returns `rate: None` for that case, because zero
    disagreements out of zero comparisons is not agreement, and a dead snapshot stream would otherwise
    look healthiest exactly while it stopped recording.  And the fold takes the WORST symbol, not the
    mean: a book trades a universe, so parity the thinnest symbol does not have is not parity.

    Since 2026-09-27 it also says how RECENT the agreement is: `compared_through` is the newest bucket
    the stalest symbol shares with the archive, and `met` is `parity_satisfied` at `now` - the gate's
    own answer, not a copy of it.  From 09-09 to 09-26 this block reported agreement about the same
    buckets of 2026-09-07, because nothing had refreshed the archive since.

    It compares `METRICS_COLUMNS`, the columns some leaf reads, and says so in `columns` (operator ruling
    2026-09-27): the snapshot stamps the taker ratio five minutes early, and no candidate can read it.
    """
    archive, snapshot = MetricsStore(data_root), MetricsStore(data_root, kind="metrics_snapshot")
    compared: dict[str, float] = {}
    through: dict[str, int] = {}
    unmeasurable: list[str] = []
    for symbol in symbols:
        result = metrics_parity(snapshot.load(symbol), archive.load(symbol), columns=METRICS_COLUMNS)
        if result["rate"] is None:
            unmeasurable.append(symbol)
        else:
            compared[symbol] = float(result["rate"])
            through[symbol] = int(result["through"])
    if not compared:
        return {
            "enforced": False,
            "reason": f"no overlapping buckets for any of {len(symbols)} symbols",
            "unmeasurable": unmeasurable,
            "columns": list(METRICS_COLUMNS),
        }
    worst = max(compared, key=lambda symbol: compared[symbol])
    stalest = min(through, key=lambda symbol: through[symbol])
    status: dict[str, Any] = {
        "enforced": True,
        "symbols_compared": len(compared),
        "unmeasurable": unmeasurable,
        "worst_symbol": worst,
        "worst_differing_rate": compared[worst],
        "compared_through": datetime.fromtimestamp(through[stalest] / 1000, tz=UTC).isoformat(),
        "stalest_symbol": stalest,
        "columns": list(METRICS_COLUMNS),
    }
    status["met"], status["why"] = parity_satisfied(status, now=now)
    return status


def data_coverage(store: StateStore, root: str | Path = ".beidou/data", interval: str = "1h") -> dict[str, Any]:
    """Live symbols whose research klines are missing, so a backtest would silently drop them.

    ``load_panel`` excludes a symbol with no stored klines and logs a warning nobody reads; CYSUSDT was
    traded live for sixteen hours while every research run quietly ran without it.
    """
    state, unreadable = readable_state(store)
    if unreadable:
        return {"live_symbols": None, "missing_klines": None, "reason": unreadable}
    symbols = list(dict.fromkeys([*state.universe, *state.leaving]))
    try:
        stored = set(KlineStore(str(root)).symbols(interval))
    except Exception:  # a missing store is a research problem, never a reporting failure
        return {"live_symbols": len(symbols), "missing_klines": None}
    missing = [symbol for symbol in symbols if symbol not in stored]
    return {"live_symbols": len(symbols), "missing_klines": missing, "missing_count": len(missing)}


def _dataset_block(dataset: Mapping[str, Any] | None) -> dict[str, Any]:
    """D-041: what the cited evidence's dataset manifest says about the data on disk.

    Empty lists rather than ``None`` when nothing was passed, so a reader never has to distinguish
    "not checked" from "checked and clean" by the shape of the value - the same mistake the manifest
    itself made about funding.
    """
    block = dict(dataset or {})
    return {"blocking": list(block.get("blocking", [])), "advisory": list(block.get("advisory", []))}


#: `StandardOutPath` of `deploy/com.beidou.data.plist`, in the directory `lock.APP_SUPPORT` names.  Looked
#: up through the module at call time, not imported by value, so that the suite's redirect reaches it.
DATA_JOB_LOG = "data.stdout.log"
_ARCHIVE_VERDICT = re.compile(r"^\[(?P<at>[^\]]+)\] (?P<verdict>ok|FAIL) +archive tests: (?P<summary>.*)$")


def archive_tests_status(log: Path | None = None) -> dict[str, Any]:
    """WP-P4: the last verdict `deploy/run_data.sh` wrote on the tests that read `.beidou/` itself.

    They skip wherever the archive is absent - CI and every worktree - so the one checkout that ran them
    was the one nobody runs the gates in, and BNX's fixture sat red there from 2026-09-25, unseen.  The
    nightly data job runs them now and pages on a FAIL; this prints the last line it wrote.  Reported
    only: the job pages once a night, and this runs every hour.  The line carries its own time, so a job
    that stopped running reads as a date that stopped moving rather than as a pass.
    """
    path = log if log is not None else lock.APP_SUPPORT / DATA_JOB_LOG
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"status": "未跑", "why": f"没有 {path.name}：data job 没装载，或还没跑过"}
    for line in reversed(lines):
        if found := _ARCHIVE_VERDICT.match(line):
            status = "通过" if found["verdict"] == "ok" else "失败"
            return {"status": status, "at": found["at"], "summary": found["summary"]}
    return {"status": "未跑", "why": f"{path.name} 里没有 archive tests 的结果行：快进主 checkout 后要等下一个 01:20"}


class _ColumnView(MetricsStore):
    """The snapshot store as one column sees it: only the buckets in which that column holds a number.

    `live_coverage_bars` counts buckets whatever they carry, and that count is the gate's.  It is not a
    column's: the long/short endpoints joined the recording at 2026-09-12 12:55Z, five days after open
    interest (read 2026-09-28 off the managed symbols recorded since 09-07), so the gate counts five days
    in which that column has no number.  Handing the gate's own function this view keeps it the gate's
    arithmetic rather than a second copy of it.
    """

    def __init__(self, root: str | Path, column: str) -> None:
        super().__init__(root, kind="metrics_snapshot")
        self.column = column

    def load(self, symbol: str) -> pd.DataFrame:
        frame = super().load(symbol)
        return frame[frame[self.column].notna()] if self.column in frame else frame.iloc[:0]


def _leaf_lookbacks() -> dict[str, int]:
    """Per column, the longest `lookback()` of a leaf that reads it, over the miner's declared space."""
    longest: dict[str, int] = {}
    nodes = [candidate.expr for candidate in enumerate_candidates().candidates]
    while nodes:
        node = nodes.pop()
        nodes.extend(node.children())
        if not node.children():
            for column in (*node.reads_metrics(), *([SPOT_BASIS_COLUMN] if node.reads_spot() else [])):
                longest[column] = max(longest.get(column, 0), node.lookback())
    return longest


def _running_readers(model: AlphaModel, declaration: str) -> dict[str, int]:
    """Enabled strategies declaring `needs_metrics` or `needs_spot` under their registry params, with their warmup.

    The walk of `LiveEngine.strategies_needing_metrics` / `strategies_needing_spot`, which are methods of an
    engine this process does not hold.  The test that runs the startup gate beside this reading holds the two
    together.
    """
    readers: dict[str, int] = {}
    for entry in model.entries:
        spec = get_signal(entry.id)
        declared = getattr(spec, declaration, None)
        if callable(declared) and bool(declared(entry.params)):
            readers[entry.id] = spec.warmup_for(entry.params)
    return readers


def data_family_parity(store: StateStore, data_root: str | Path, fidelity: ReplayInputs | None) -> dict[str, Any]:
    """WP-A1 / M-PR06: per data family, the bars live holds against the bars the startup gate asks a reader for.

    Both numbers are `LiveEngine.startup`'s, from its own functions on its own inputs, so the verdict here is
    the gate's rather than a second opinion that could drift from it (KILL-027, `metrics_refusal`):

    * held is `live_coverage_bars` over `metrics_snapshot/` and the managed symbols, at the model's interval.
      In brackets, the same function over `_ColumnView`; the verdict does not read that one.
    * asked-for is the `required_bars` the engine hands `metrics_refusal`: its `history_bars`, which is
      `required_history(model, market_data.history_bars)`.  That is the whole request window - the listing-age
      filter plus the model's warmup - and not the reader's own lookback: on 2026-09-28 the long/short leaf
      needs 168 bars and the gate asks 1,442.  A reader whose lookback outran the model's would raise it, by
      `AlphaModel.warmup_bars`' `max(...) + 1`; that line is copied here, and a test holds it to a real model.
    * the readers are the enabled strategies that declare the need and the longest mining leaf on the column.
      With no enabled reader the row says so and prices the leaf alone.

    Spot is held at 0 by construction.  Nothing supplies spot to the live panel, and the engine asks spot for
    a measurement rather than for coverage (`spot_refusal`); `spot_klines/` is the nightly research archive,
    which the loop never reads, so it is not what this row counts.

    Reported only: nothing pages on it and no gate reads it.  Like `ReplayInputs.from_profile` it never
    raises - it runs inside the hourly check - so what it cannot compute blinds this block and says why.
    """
    if fidelity is None or fidelity.model is None:
        why = fidelity.problem if fidelity is not None else None
        return {"readable": False, "why": why or "没有模型：调用方没有传 registry 与 profile"}
    state, unreadable = readable_state(store)
    if unreadable:
        return {"readable": False, "why": unreadable}
    try:
        return _families(fidelity.model, fidelity.history_bars, state, data_root, _cycles(store))
    except Exception as error:
        return {"readable": False, "why": f"{type(error).__name__}: {error}"}


#: The newest cycles `snapshot_lag` reads: one day of hourly polls.
SNAPSHOT_LAG_CYCLES = 24


def snapshot_lag(rows: Sequence[Mapping[str, Any]], interval_ms: int, period_ms: int) -> dict[str, Any]:
    """Per REST page, how many 5m buckets the newest one polled sat behind the bar close, most-lagging symbol.

    Research gives a bar the bucket that closed AT the bar's close (`beidou_data.metrics.align_to_bars`), and
    `alignment.METRICS` leaves the latency past that close to a health report; this is that report.  The poll
    runs after the cycle's targets (`LiveEngine.run_cycle`), about 30 s past the close, so 0 means REST had
    already published research's bucket and 1 means it had not.  No decision reads metrics yet.
    """
    polled = [row for row in rows if (row.get("metrics_snapshot") or {}).get("newest_open_ms") and "bar_open_ms" in row]
    counts: dict[str, dict[str, int]] = {}
    for row in polled[-SNAPSHOT_LAG_CYCLES:]:
        close = int(row["bar_open_ms"]) + interval_ms
        for page, (oldest, _freshest) in row["metrics_snapshot"]["newest_open_ms"].items():
            lag = str((close - (int(oldest) + period_ms)) // period_ms)
            counts.setdefault(page, {})[lag] = counts.get(page, {}).get(lag, 0) + 1
    return {"cycles": min(len(polled), SNAPSHOT_LAG_CYCLES), "by_page": counts}


def _families(
    model: AlphaModel, floor: int, state: LiveState, data_root: str | Path, rows: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    if state.stopped_books:  # as `LiveEngine.__init__` does, before anything reads `history_bars`
        model = model.without_books(list(state.stopped_books))
    symbols = list(dict.fromkeys([*state.universe, *state.leaving]))  # `LiveEngine.managed_symbols`
    interval_ms = interval_seconds(model.interval) * 1000  # `LiveConfig.interval_ms`, off the same profile key
    snapshot = MetricsStore(data_root, kind="metrics_snapshot")
    if not snapshot.directory.is_dir():
        blind = f"没有 {snapshot.directory}"
    else:
        blind = "" if symbols else "state.json 里没有 universe"
    window = required_history(model, floor)  # `LiveEngine.history_bars`
    leaves = _leaf_lookbacks()
    families = []
    for column in (*METRICS_COLUMNS, SPOT_BASIS_COLUMN):
        readers = _running_readers(model, "needs_spot" if column == SPOT_BASIS_COLUMN else "needs_metrics")
        # One more entry can only raise `AlphaModel.warmup_bars` (the entries' max, plus one) to its own + 1.
        asked = max(window, model.min_history_bars + max([leaves.get(column, 0), *readers.values()]) + 1)
        why = "实盘循环没有 spot 源" if column == SPOT_BASIS_COLUMN else blind
        held: int | None = 0 if column == SPOT_BASIS_COLUMN else None
        own: int | None = None
        if not why:
            held = live_coverage_bars(snapshot, symbols, interval_ms=interval_ms)
            own = live_coverage_bars(_ColumnView(data_root, column), symbols, interval_ms=interval_ms)
        enough: bool | None = None
        if held is not None:  # the gate's own comparison; the column's name stands in for a reader nobody runs
            enough = metrics_refusal(needs_metrics=[column], live_coverage_bars=held, required_bars=asked) is None
        families.append(
            {
                "column": column,
                "held_bars": held,
                "column_bars": own,
                "required_bars": asked,
                "ratio": None if held is None else held / asked,
                "enough": enough,
                "readers": readers,
                "leaf_lookback": leaves.get(column),
                "why": why,
            }
        )
    lag = snapshot_lag(rows, interval_ms, PERIOD_MS["5m"])
    return {
        "readable": True,
        "symbols": len(symbols),
        "request_window": window,
        "families": families,
        "snapshot_lag": lag,
    }


def _data_family_lines(block: Mapping[str, Any]) -> dict[str, Any]:
    """WP-A1 in the daily report: per family, held / asked-for, the verdict, and who reads it."""
    if not block.get("readable"):
        return {"status": "不可读", "why": block.get("why") or "no reading"}
    lines: dict[str, Any] = {}
    for row in block["families"]:
        readers = "、".join(f"{name}（warmup {bars}）" for name, bars in row["readers"].items()) or "没有在跑的读者"
        leaf = f"挖掘叶 lookback {row['leaf_lookback']}" if row.get("leaf_lookback") else "没有挖掘叶读它"
        asked = f"需要 {row['required_bars']} bars"
        if row["held_bars"] is None:
            lines[row["column"]] = f"覆盖不可读（{row['why']}）/ {asked}；{readers}；{leaf}"
            continue
        own = f"本列有值 {row['column_bars']}" if row["column_bars"] is not None else row["why"]
        verdict = "够" if row["enough"] else "不够"
        lines[row["column"]] = (
            f"覆盖 {row['held_bars']}（{own}）/ {asked} = {row['ratio']:.2f}，{verdict}；{readers}；{leaf}"
        )
    lag = block.get("snapshot_lag") or {}
    lines["快照时的桶延迟"] = (
        "；".join(
            f"{page} " + "、".join(f"落后 {n} 桶 {c} 次" for n, c in sorted(tally.items(), key=lambda kv: int(kv[0])))
            for page, tally in sorted((lag.get("by_page") or {}).items())
        )
        + f"（最近 {lag['cycles']} 个周期，取最慢的 managed symbol；研究按 bar 收盘时刚关的那个桶对齐，即 0 桶）"
        if lag.get("cycles")
        else "还没有读数：周期行里没有 newest_open_ms，载入它的那次重启之后才开始记"
    )
    lines["口径"] = (
        f"覆盖是启动门的 live_coverage_bars：{block['symbols']} 个 managed symbol 里最薄的一个，5m 桶折成 bar；"
        "括号里只数本列有值的桶，不进判定。需要是门交给 metrics_refusal 的 required_bars，"
        f"即 required_history(模型 + 最长的读者)，现有模型的请求窗口 {block['request_window']} bars。"
        "只报告：不告警，不改门"
    )
    return lines
