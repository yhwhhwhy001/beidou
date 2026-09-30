"""Live record vs a research replay of the same bars: does the loop's signal equal research's, bar by bar?

backtest-guard 2026-09-30 (docs/analysis/2026-09-30-backtest-guard-audit.md).  Read-only and zero ledger: it
reads `.beidou/live/cycles.jsonl` and the archive, writes nothing, and never calls `research validate`.

* Replay: M-Q08's own path - `ReplayInputs.from_profile` builds the model the files on disk describe, the
  universe is the one the loop recorded bar by bar (`replay_membership`), and `model.evaluate` scores it.
  The panel starts where `execution_fidelity.backtest_turnover` starts it: warmup plus 60 days before the window.
* Window: every armed, priced cycle under the registry digest on disk, up to the archive's last close.  The
  digest pins every signal parameter, so the replay scores the configuration each of those cycles ran.
* Live side: the row's `contributions`, the per-strategy convictions the loop combined on that bar.

tsmom trades direction only (`conviction_mode: sign`), so its sign is the reading; flow is a continuous
score, so its value is compared as well.  Run from the repository root:

    PYTHONPATH=$PWD .venv/bin/python scratchpad/signal_parity_live_vs_replay.py
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
    _decided, _combined, per_strategy = model.evaluate(panel, membership)
    last_close = panel.close.index[-1]
    print(f"registry {inputs.registry_on_disk}; bars {_stamp(since_ms)} .. {last_close.isoformat()} (archive's last)")

    for strategy in sorted(per_strategy):
        replay = per_strategy[strategy]
        states: list[tuple[str, str]] = []
        cells = same_sign = value_differs = shorts_live = shorts_replay = 0
        worst = 0.0
        differing: dict[str, int] = {}
        signs: dict[str, dict[pd.Timestamp, dict[str, float]]] = {"live": {}, "replay": {}}
        for row in window:
            at = pd.Timestamp(int(row["bar_open_ms"]), unit="ms", tz="UTC")
            if at > last_close or at not in replay.index:
                continue
            universe = sorted(set(row.get("universe") or []))
            live = {s: float(v) for s, v in ((row.get("contributions") or {}).get(strategy) or {}).items()}
            scored = replay.loc[at]
            rep = {s: float(scored[s]) for s in universe if s in scored.index and np.isfinite(scored[s])}
            states.append((state({s: live.get(s, 0.0) for s in universe}), state(rep)))
            signs["live"][at] = {s: float(np.sign(live.get(s, 0.0))) for s in universe}
            signs["replay"][at] = {s: float(np.sign(rep.get(s, 0.0))) for s in universe}
            for symbol in universe:
                a, b = live.get(symbol, 0.0), rep.get(symbol, 0.0)
                if a == 0.0 and b == 0.0:
                    continue
                cells += 1
                same_sign += int(np.sign(a) == np.sign(b))
                shorts_live += int(a < 0)
                shorts_replay += int(b < 0)
                if abs(a - b) > 1e-9:
                    value_differs += 1
                    differing[symbol] = differing.get(symbol, 0) + 1
                worst = max(worst, abs(a - b))
        n = len(states)
        print(f"\n{strategy}: {n} bars, {cells} non-zero symbol-bars")
        print(f"  same sign {same_sign}/{cells}; shorts live {shorts_live} / replay {shorts_replay}")
        print(f"  value differs (> 1e-9) on {value_differs}, max |live - replay| {worst:.4g}, by symbol {differing}")
        print(f"  same book state on {sum(a == b for a, b in states)}/{n} bars")
        # The finer half: a sign or an entry/exit that changes between two consecutive bars.  A one-bar look-ahead
        # on either side would move each change by a bar, which a sign-only count cannot see.
        live_frame = pd.DataFrame.from_dict(signs["live"], orient="index").sort_index().fillna(0.0)
        replay_frame = pd.DataFrame.from_dict(signs["replay"], orient="index").reindex_like(live_frame).fillna(0.0)
        consecutive = live_frame.index.to_series().diff().eq(pd.Timedelta(hours=1)).to_numpy()[:, None]
        moved_live = (live_frame != live_frame.shift(1)).to_numpy() & consecutive
        moved_replay = (replay_frame != replay_frame.shift(1)).to_numpy() & consecutive
        flips = ((live_frame * live_frame.shift(1)) == -1.0).to_numpy() & consecutive
        print(
            f"  changes on consecutive bars: live {int(moved_live.sum())}, replay {int(moved_replay.sum())}, "
            f"same bar and symbol {int((moved_live & moved_replay).sum())}; of the live ones {int(flips.sum())} full flips"
        )
        for label, side in (("live", 0), ("replay", 1)):
            keys = ("all long", "all short", "two-sided", "flat")
            print(f"  {label:6s} {({key: round(sum(p[side] == key for p in states) / n, 4) for key in keys})}")


if __name__ == "__main__":
    main()
