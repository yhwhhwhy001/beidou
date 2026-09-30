"""Zero ledger, read-only: M-Q08's turnover clause over the whole b8f215ab window, and where the excess sits.

backtest-guard 2026-09-30.  The daily reports read this window four times (09-23 .. 09-26, ratio 2.07 -> 2.38) and
never judged it: the construction changed on 09-27 before 14 complete days.  This closes it with the instrument's
own functions (`turnover_fidelity`, `backtest_turnover`, `live_turnover`) on the record truncated at two points,
with the model the files described while that construction ran - main 8895c422, before #163 moved k to 0.175.
Read-only: it reads `.beidou/live` and the archive and writes a temporary copy of three config files.

    PYTHONPATH=$PWD .venv/bin/python scratchpad/mq08_b8f215ab_window.py

Decomposition: an exit only one side took costs that side a close and, `cooldown_bars` later, a re-entry.
Each side's one-sided exits are taken out WITH the next trade of that symbol after them, and the ratio is
read again.  A split, not a counterfactual: the positions after a one-sided exit differ for a while.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from beidou_live.composition import load_registry
from beidou_live.config import live_config
from beidou_live.construction import canonical_construction
from beidou_live.engine import construction_fingerprint
from beidou_live.execution_fidelity import (
    DAY_MS,
    ReplayInputs,
    _exits_taken,
    _ms,
    _stamp,
    backtest_turnover,
    by_day,
    live_turnover,
    paired_ratio,
    turnover_fidelity,
)
from beidou_live.risk_budget import one_row_per_order
from beidou_shared.config import load_yaml

ROOT = Path("/Users/maguannan/beidou")
DATA = ROOT / ".beidou" / "data"
OLD = "8895c4221e5181d39ad8e69d4ec984d6ff421e94"  # main just before #163 moved k 0.60 -> 0.175 (2026-09-27T07:10Z)
WANT = "0c555e1c837e"  # canonical digest of b8f215ab (CONSTRUCTION_ALIASES)


def ms(stamp: str) -> int:
    return int(pd.Timestamp(stamp).timestamp() * 1000)


def _old_config(into: Path) -> Path:
    """The three files the loop was started from while b8f215ab ran, as `git show` gives them."""
    (into / "config").mkdir(parents=True, exist_ok=True)
    for name in ("live.demo.yaml", "alpha_registry.yaml", "costs.yaml"):
        text = subprocess.run(["git", "show", f"{OLD}:config/{name}"], capture_output=True, text=True, check=True).stdout
        (into / "config" / name).write_text(text, encoding="utf-8")
    return into / "config"


def load() -> tuple[ReplayInputs, list[dict[str, Any]], list[dict[str, Any]]]:
    here = _old_config(Path(tempfile.mkdtemp(prefix="mq08-")))
    profile = load_yaml(str(here / "live.demo.yaml"))
    profile["costs"] = str(here / "costs.yaml")
    registry = load_registry(str(here / "alpha_registry.yaml"))
    digest = construction_fingerprint(live_config(profile, list(registry.universe), registry, dry_run=True))["digest"]
    canonical = str(canonical_construction(digest))[:12]
    # Today's fingerprint hashes two field sets more (v11 leverage, v12 gross denominator), so it cannot equal
    # the digest the loop wrote.  Checked with 8895c422's OWN code instead (2026-09-30): b8f215ab706c -> 0c555e1c.
    print(f"construction of the 8895c422 files under today's code: {digest[:12]} -> {canonical}; ran as {WANT}")
    inputs = ReplayInputs.from_profile(profile, registry, DATA)
    if inputs.model is None:
        sys.exit(f"no model: {inputs.problem}")
    live = ROOT / ".beidou" / "live"
    cycles = [json.loads(line) for line in (live / "cycles.jsonl").open()]
    rows = [r for r in cycles if r.get("equity") is not None and not r.get("dry_run")]
    trades = [json.loads(line) for line in (live / "trades.jsonl").open()]
    return inputs, rows, trades


def cut(records: Sequence[Mapping[str, Any]], until_ms: int) -> list[dict[str, Any]]:
    return [dict(r) for r in records if not isinstance(r.get("bar_open_ms"), int) or int(r["bar_open_ms"]) <= until_ms]


def reading(label: str, block: Mapping[str, Any]) -> None:
    print(
        f"{label}: ratio {block.get('ratio'):.3f} +- {block.get('se'):.3f}, live {block['live']:.3f} / backtest"
        f" {block['backtest']:.3f}, {block['since']} .. {block['until']}, complete days {block['complete_days']},"
        f" exits {block['exits']}, unpriced {sorted(block['unpriced'])}"
    )


def next_trade_ms(by_symbol: Mapping[str, list[int]], symbol: str, after_ms: int, until_ms: int) -> int | None:
    later = [bar for bar in by_symbol.get(symbol, []) if after_ms < bar <= until_ms]
    return min(later) if later else None


def main() -> None:
    inputs, rows, trades = load()
    end_ms = max(int(r["bar_open_ms"]) for r in rows if str(r.get("construction", "")).startswith("b8f215ab"))
    for label, until in (("to 09-26T15:00 (the 09-26 report)", ms("2026-09-26T15:00Z")), ("whole window", end_ms)):
        reading(label, turnover_fidelity(cut(rows, until), cut(trades, until), inputs))

    rows_w, trades_w = cut(rows, end_ms), cut(trades, end_ms)
    since_ms = ms("2026-09-17T16:00Z")
    replay = backtest_turnover(inputs, rows_w, since_ms=since_ms)
    decision_ms = _ms(pd.DatetimeIndex(replay.turnover.index))
    inside = (decision_ms >= since_ms) & (decision_ms <= end_ms)
    cells = replay.turnover.loc[inside]
    cells.index = decision_ms[inside]
    live = live_turnover(rows_w, trades_w, since_ms=since_ms, until_ms=end_ms, priced_until=replay.priced_until)

    # Per (bar, symbol) on the live side, to take one-sided exits and their re-entries out.
    sized = sorted((int(r["bar_open_ms"]), float(r["equity"])) for r in rows_w if r.get("equity"))
    live_cells: dict[tuple[int, str], float] = {}
    for trade in one_row_per_order(trades_w):
        bar = trade.get("bar_open_ms")
        if trade.get("flatten") or not isinstance(bar, int) or not since_ms <= bar <= end_ms:
            continue
        notional = abs(float(trade.get("executed_qty") or 0.0) * float(trade.get("avg_price") or 0.0))
        equity = next((e for b, e in reversed(sized) if b <= bar), None)
        if notional > 0 and equity:
            key = (bar, str(trade.get("symbol")))
            live_cells[key] = live_cells.get(key, 0.0) + notional / equity
    live_by_symbol: dict[str, list[int]] = {}
    for bar, symbol in live_cells:
        live_by_symbol.setdefault(symbol, []).append(bar)
    bt_by_symbol = {s: [int(b) for b, v in cells[s].items() if v > 0] for s in cells.columns}

    live_exits = {(b, s) for b, s in _exits_taken(rows_w) if since_ms <= b <= end_ms}
    bt_exits = {(b, s) for b, s in replay.exits if since_ms <= b <= end_ms}
    only_live, only_bt = sorted(live_exits - bt_exits), sorted(bt_exits - live_exits)
    print(f"exits: live {len(live_exits)}, replay {len(bt_exits)}, both {len(live_exits & bt_exits)}")
    for side, taken in (("live only", only_live), ("replay only", only_bt)):
        print(f"  {side}: {[(_stamp(b)[5:16], s) for b, s in taken]}")

    removed_live: set[tuple[int, str]] = set()
    for bar, symbol in only_live:
        removed_live.add((bar, symbol))
        reentry = next_trade_ms(live_by_symbol, symbol, bar, end_ms)
        if reentry is not None:
            removed_live.add((reentry, symbol))
    removed_bt: set[tuple[int, str]] = set()
    for bar, symbol in only_bt:
        removed_bt.add((bar, symbol))
        reentry = next_trade_ms(bt_by_symbol, symbol, bar, end_ms)
        if reentry is not None:
            removed_bt.add((reentry, symbol))
    live_left = {}
    for (bar, symbol), value in live_cells.items():
        if (bar, symbol) not in removed_live and bar <= replay.priced_until.get(symbol, -1):
            live_left[bar] = live_left.get(bar, 0.0) + value
    bt_left = {}
    for symbol in cells.columns:
        for bar, value in cells[symbol].items():
            if value and (int(bar), symbol) not in removed_bt and int(bar) <= replay.priced_until.get(symbol, -1):
                bt_left[int(bar)] = bt_left.get(int(bar), 0.0) + float(value)
    taken_live = sum(v for k, v in live_cells.items() if k in removed_live)
    taken_bt = sum(float(cells.loc[b, s]) for b, s in removed_bt if b in cells.index and s in cells.columns)
    days = by_day(live_left, bt_left)
    ratio, se = paired_ratio([(d["live"], d["backtest"]) for d in days])
    print(f"taken out: live {taken_live:.3f} over {len(removed_live)} cells, replay {taken_bt:.3f} over {len(removed_bt)} cells")
    print(f"without one-sided exits and their re-entries: ratio {ratio:.3f} +- {se:.3f}, live {sum(live_left.values()):.3f} / backtest {sum(bt_left.values()):.3f}")
    print(f"days: {[(d['day'][5:], round(d['live'], 3), round(d['backtest'], 3)) for d in days]}")
    print(f"live turnover check (instrument's own): {sum(live['by_bar'].values()):.3f}; flatten fills {live['flatten_fills']}")
    print(f"window days {(end_ms - since_ms) / DAY_MS:.2f}, last bar {_stamp(end_ms)}")


if __name__ == "__main__":
    main()
