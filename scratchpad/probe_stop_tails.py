"""Re-measure the probe-stop thresholds on the construction the checkout holds, and say where each candidate sits.

`probe-stop-caliber` (governance/window_changes.yaml) was queued on 2026-09-12 with thresholds measured by
`scratchpad/probe_stop_recalibration.py` (pre-registered b883d01e, verdict 3e584cbf) at vol_target 0.30.  The
threshold is a share of equity and the book's P&L scales with its vol target, so the numbers belong to that
construction and not to any later one.  k moved twice after (0.60 on 09-14, 0.175 on 09-27) and the queue
entry did not.  This is the same measurement, unchanged in method, on whatever `config/live.demo.yaml` and the
registry say today:

  each book's own weight path (`model.book_weights`, before `combine_books`), w_{t-1} . r_t summed over 720
  bars (30 days), as a share of equity, on the panel the book report was validated on; the EMPIRICAL 2.28%
  quantile (= Phi(-2)), rounded to 0.1% - the rule written into the 09-12 pre-registration.

It adds one reading the 09-12 script did not print: the empirical probability that a 30-day window lands at or
below each threshold on the table (the one in force, the rule's value today, the value queued in the file).

Measures the configuration already in force.  No grid, no selection, no comparison: not a trial, so no ledger
row (the 09-12 pre-registration states the same for its own run).  Run from the checkout root; it reads
`.beidou/data` and writes one JSON next to itself.  About 3 minutes and 17 GB peak on the 878-symbol panel.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import load_panel
from beidou_live.config import build_model_from_profile, load_profile
from beidou_live.probe import probes_from_registry

ROOT = ".beidou/data"
PROFILE = "config/live.demo.yaml"
WINDOW = 720  # 30 days of hourly bars, the probe stop's own window
QUANTILE = 0.0228  # Phi(-2): the tail probability D-019 claimed for its stop
REPORT = "reports/research/book-tsmom-flow-20260908T105322Z.json"  # the panel the 09-12 run used
# The thresholds queued in governance/window_changes.yaml on 2026-09-12 (measured at vol_target 0.30).
QUEUED_0912 = {"flow_short": 0.075, "main": 0.112}


def main(out: str) -> None:
    payload = load_profile(PROFILE)
    model, registry = build_model_from_profile(payload)
    with open(REPORT, encoding="utf-8") as handle:
        window = json.load(handle)["universes"]["pit"]["range"]
    store = KlineStore(ROOT)
    panel = load_panel(
        store,
        sorted(store.symbols("1h")),
        "1h",
        funding_store=FundingStore(ROOT),
        start=str(window["start"])[:10],
        end=str(window["end"])[:10],
    )
    books = model.book_weights(model.strategy_targets(panel, None), panel.close, panel.bars_per_year)
    returns = panel.close.pct_change(fill_method=None)
    in_force = {probe.book: probe.max_loss for probe in probes_from_registry(registry)}
    commit = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
    result: dict[str, object] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "vol_target": (payload.get("portfolio") or {}).get("vol_target"),
        "fractions": {name: book.fraction for name, book in registry.books.items()},
        "panel": {"start": str(window["start"]), "end": str(window["end"]), "bars": int(panel.close.shape[0])},
        "symbols": int(panel.close.shape[1]),
        "books": {},
    }
    for name, weights in books.items():
        aligned = weights.shift(1).reindex(columns=returns.columns).fillna(0.0)
        pnl = (aligned * returns.fillna(0.0)).sum(axis=1)
        rolling = pnl.rolling(WINDOW).sum().dropna()
        sigma_30 = float(rolling.std(ddof=1))
        quantile = float(rolling.quantile(QUANTILE))
        rule = round(-quantile, 3)
        table = {"in_force": in_force.get(name), "rule_today": rule, "queued_0912": QUEUED_0912.get(name)}
        result["books"][name] = {  # type: ignore[index]
            "annual_sigma": float(pnl.std(ddof=1) * (8760**0.5)),
            "sigma_30d": sigma_30,
            "quantile_228": quantile,
            "windows": int(rolling.size),
            "thresholds": {
                label: {
                    "max_loss": value,
                    "in_sigma_30d": value / sigma_30,
                    "p_window_at_or_below": float((rolling <= -value).mean()),
                }
                for label, value in table.items()
                if value
            },
        }
        print(f"{name:12s} sigma_30d {sigma_30:.3%}  q2.28 {quantile:.3%}  windows {rolling.size}")
        for label, value in table.items():
            if value:
                share = float((rolling <= -value).mean())
                print(f"    {label:12s} -{value:.1%}  {value / sigma_30:5.2f} sigma  P(30d <= -x) {share:.3%}")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else f"scratchpad/probe_stop_tails-{datetime.now(UTC):%Y%m%d}.json")
