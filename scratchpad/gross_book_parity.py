"""Live record vs research replay, one layer later than signal_parity: does the loop's MAIN BOOK SIZE equal research's?

backtest-guard 2026-10-19 (docs/analysis/2026-10-19-backtest-guard-audit.md).  Read-only and zero ledger.
signal_parity_live_vs_replay.py compares the per-strategy convictions (`contributions`); tsmom is sign-only, so it
cannot see a difference in vol targeting.  This compares the size of the main book after vol targeting
(`model.book_weights`, before the book guards) with the row's `book_weights["main"]`, bar by bar, on the same replay
inputs (`ReplayInputs.from_profile`, the universe the loop recorded).  It also prints the post-guard live `targets`.

Window: armed priced cycles under the registry digest ON DISK.  On 2026-10-19 the 7f8adb754962 window (09-15 ..
10-19, 623 bars) was read from a detached worktree at a3acbea9 with `.beidou` symlinked to the main checkout's.
Reading: ratio 3.429 before 09-27 (= 0.60 / 0.175, the k change), exactly 1.000 on every bar after.

    PYTHONPATH=$PWD .venv/bin/python scratchpad/gross_book_parity.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from beidou_live.composition import load_registry
from beidou_live.execution_fidelity import (
    WARMUP_DAYS,
    FundingStore,
    KlineStore,
    ReplayInputs,
    _membership_table,
    _stamp,
    load_panel,
    replay_membership,
)
from beidou_shared.config import load_yaml

ROOT = Path.cwd()
DATA = ROOT / ".beidou" / "data"
BAR_MS = 3_600_000
DAY_MS = 86_400_000


def state(convictions: dict[str, float]) -> str:
    signs = {float(np.sign(value)) for value in convictions.values() if value != 0.0}
    if not signs:
        return "flat"
    return "all long" if signs == {1.0} else "all short" if signs == {-1.0} else "two-sided"



def main() -> None:
    profile = load_yaml(str(ROOT / "config" / "live.demo.yaml"))
    inputs = ReplayInputs.from_profile(profile, load_registry(str(ROOT / "config" / "alpha_registry.yaml")), DATA)
    if inputs.model is None:
        sys.exit(f"no model: {inputs.problem}")
    model = inputs.model
    rows = [json.loads(line) for line in (ROOT / ".beidou" / "live" / "cycles.jsonl").open()]
    rows = [row for row in rows if not row.get("dry_run") and row.get("equity") is not None]
    window = [r for r in rows if r.get("registry") == inputs.registry_on_disk and isinstance(r.get("bar_open_ms"), int)]
    if not window:
        sys.exit(f"no armed cycle ran registry {inputs.registry_on_disk}")
    since_ms = min(int(row["bar_open_ms"]) for row in window)
    # Same replay inputs as signal_parity_live_vs_replay.main, unchanged.
    start_ms = since_ms - (model.min_history_bars + model.warmup_bars) * BAR_MS - WARMUP_DAYS * DAY_MS
    recorded = sorted({int(r["bar_open_ms"]): frozenset(r["universe"]) for r in rows if r.get("universe")}.items())
    names = {s for bar, universe in recorded if bar >= start_ms for s in universe}
    names |= {s for universe in [u for bar, u in recorded if bar < start_ms][-1:] for s in universe}
    table = _membership_table(DATA) if (not recorded or recorded[0][0] > start_ms) else None
    if table is not None:
        early = table.loc[pd.Timestamp(start_ms, unit="ms", tz="UTC") - pd.Timedelta(days=1) :]
        early = early.loc[: pd.Timestamp(recorded[0][0], unit="ms", tz="UTC")] if recorded else early
        names |= {str(symbol) for symbol in early.columns[early.any(axis=0)]}
    panel = load_panel(
        KlineStore(str(DATA)), sorted(names), model.interval, funding_store=FundingStore(str(DATA)), start=_stamp(start_ms)
    )
    membership = replay_membership(panel.index, panel.symbols, recorded, table)
    per = model.strategy_targets(panel, membership)
    books = model.book_weights(per, panel.close, panel.bars_per_year)
    out = []
    for row in window:
        at = pd.Timestamp(int(row["bar_open_ms"]), unit="ms", tz="UTC")
        if at not in panel.close.index or not row.get("book_weights"):
            continue
        out.append(
            {
                "at": at,
                "live_main": sum(abs(v) for v in row["book_weights"].get("main", {}).values()),
                "replay_main": float(books["main"].loc[at].abs().sum()),
                "live_targets_after_guards": sum(abs(v) for v in row["targets"].values()),
            }
        )
    frame = pd.DataFrame(out).set_index("at")
    frame["ratio_main"] = frame.live_main / frame.replay_main
    print(f"registry {inputs.registry_on_disk}; {len(frame)} bars")
    print(frame.resample("1D").mean().dropna().round(3).to_string())


if __name__ == "__main__":
    main()
