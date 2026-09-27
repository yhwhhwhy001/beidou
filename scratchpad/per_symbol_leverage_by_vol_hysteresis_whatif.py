"""T-11 what-if, read-only: tier changes per UTC day under several `leverage_hysteresis` lengths, and which ones.

Runs the replay in `scratchpad/per_symbol_leverage_by_vol_replay.py` itself (imported, not copied), with a wrapper
around `plan_leverage` that records each decision, so the counts are the replay's own. Reads cycles.jsonl, prints
to the terminal, writes nothing.

Kinds: `entry` = the symbol joined the managed set this cycle (its first tier after 5x); `raise` = the margin
invariant lifted it; `vol` = everything else, i.e. its sigma crossed a tier boundary and held for the hysteresis.

Run on the Mac 2026-09-27 for T-11 (hysteresis 24 failed it): 168 went into the profile.  The committed copy binds
`calls` through `recorder` for ruff's B023; the copy that ran first closed over the loop variable, which reads
the same because each closure is called only inside its own iteration.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, required=True, help="the exported branch tree")
    parser.add_argument("--cycles", type=Path, required=True)
    parser.add_argument("--days", type=float, default=14.0)
    parser.add_argument("--hysteresis", type=int, nargs="+", default=[24, 48, 72, 96, 120, 168])
    args = parser.parse_args()

    sys.path.insert(0, str(args.src / "scratchpad"))
    import per_symbol_leverage_by_vol_replay as rp

    config = rp.load_config(args.src / "config" / "live.demo.yaml")
    rows = [json.loads(line) for line in args.cycles.read_text(encoding="utf-8").splitlines() if line.strip()]
    newest = max(int(row.get("bar_open_ms") or 0) for row in rows)
    rows = [row for row in rows if int(row.get("bar_open_ms") or 0) > newest - args.days * rp.DAY_MS]
    kept = [
        row
        for row in rows
        if row.get("phase") in (None, "OK")
        and not row.get("skip")
        and row.get("targets")
        and float(row.get("equity") or 0.0) > 0
    ]
    print(f"rows kept {len(kept)} (last {args.days:g} days); tiers {list(config.leverage_tiers)}, sigma_ref {config.leverage_sigma_ref}")

    real = rp.plan_leverage

    def recorder(calls: list[tuple[dict[str, int], dict[str, int], list[str], list[str]]]) -> Any:
        def recording(**kwargs: Any) -> Any:
            before = dict(kwargs["standing"])
            plan = real(**kwargs)
            calls.append((before, dict(plan.wanted), list(plan.raised), list(kwargs["managed"])))
            return plan

        return recording

    for cycles in args.hysteresis:
        calls: list[tuple[dict[str, int], dict[str, int], list[str], list[str]]] = []
        rp.plan_leverage = recorder(calls)
        try:
            result = rp.replay(rows, replace(config, leverage_hysteresis=cycles))
        finally:
            rp.plan_leverage = real
        assert len(calls) == len(kept), (len(calls), len(kept))

        per_day: Counter[str] = Counter()
        kinds: dict[str, Counter[str]] = defaultdict(Counter)
        per_symbol: Counter[str] = Counter()
        detail: dict[str, list[str]] = defaultdict(list)
        seen: set[str] = set()
        for index, (row, (before, wanted, raised, managed)) in enumerate(zip(kept, calls, strict=True)):
            moved = {symbol: (before.get(symbol), value) for symbol, value in wanted.items() if before.get(symbol) != value}
            if index > 0:  # the first switch (every symbol from 5x to its tier) happens once, at the restart
                day = rp._day(int(row["bar_open_ms"]))
                for symbol, (old, new) in sorted(moved.items()):
                    kind = "entry" if symbol not in seen else ("raise" if symbol in raised else "vol")
                    per_day[day] += 1
                    kinds[day][kind] += 1
                    per_symbol[symbol] += 1
                    detail[day].append(f"{symbol} {old}->{new} {kind}")
            seen |= set(managed)
        assert dict(per_day) == {day: n for day, n in result["changes_per_day"].items() if n}, "classification drifted"

        worst = max(per_day.values(), default=0)
        no_entries = max((n - kinds[day]["entry"] for day, n in per_day.items()), default=0)
        total_kinds = sum(kinds.values(), Counter())
        t6 = not result["by_vol_scaled"] and not result["orders_differ"]
        print()
        print(
            f"hysteresis {cycles}: T-11 {'PASS' if worst <= 5 else 'FAIL'} (max {worst}/day, limit 5; "
            f"max without entries {no_entries}); total {sum(per_day.values())} changes {dict(total_kinds)}; "
            f"T-6 {'PASS' if t6 else 'FAIL'}"
        )
        print(f"  per day: {dict(sorted(result['changes_per_day'].items()))}")
        print(f"  per symbol: {dict(per_symbol.most_common())}")
        for day in sorted(per_day):
            if per_day[day] >= 5:
                print(f"  {day} ({per_day[day]}): {', '.join(detail[day])}")
        print(f"  tiers at the end: {result['final']}")


if __name__ == "__main__":
    main()
