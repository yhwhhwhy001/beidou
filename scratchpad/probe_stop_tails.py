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

Two populations, because the 09-12 script ranked over whatever symbols the store held (`strategy_targets(panel,
None)`: 241 then, 878 on 2026-09-30) while the book report it cites - and the live loop - trade the point-in-time
membership.  `panel` (the default) repeats the 09-12 run exactly; `pit` restricts the panel to the symbols the
membership table ever names and masks each bar with it, the way `research book --universe pit` does.

Measures the configuration already in force.  No grid, no selection, no comparison: not a trial, so no ledger
row (the 09-12 pre-registration states the same for its own run).  Run from the checkout root:
`python scratchpad/probe_stop_tails.py [panel|pit] [out.json]`.  It reads `.beidou/data` and writes one JSON.
About 3 minutes and 17 GB peak on the 878-symbol panel; less on `pit`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

import pandas as pd

from beidou_cli.research_panel import _membership_table
from beidou_data.pool import membership_at_bars, tenure_mask
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


def main(universe: str, out: str, keep_series: bool = False) -> None:
    payload = load_profile(PROFILE)
    model, registry = build_model_from_profile(payload)
    with open(REPORT, encoding="utf-8") as handle:
        window = json.load(handle)["universes"]["pit"]["range"]
    store = KlineStore(ROOT)
    symbols = sorted(store.symbols("1h"))
    if universe == "pit":
        members = _membership_table(ROOT)
        symbols = sorted({str(s) for s in members.columns[members.any(axis=0)]} & set(symbols))
    panel = load_panel(
        store,
        symbols,
        "1h",
        funding_store=FundingStore(ROOT),
        start=str(window["start"])[:10],
        end=str(window["end"])[:10],
    )
    membership = membership_at_bars(tenure_mask(members, 0), panel.index) if universe == "pit" else None
    books = model.book_weights(model.strategy_targets(panel, membership), panel.close, panel.bars_per_year)
    returns = panel.close.pct_change(fill_method=None)
    series = {}
    in_force = {probe.book: probe.max_loss for probe in probes_from_registry(registry)}
    commit = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
    result: dict[str, object] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "universe": universe,
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
        series[name] = rolling
        sigma_30 = float(rolling.std(ddof=1))
        quantile = float(rolling.quantile(QUANTILE))
        rule = round(-quantile, 3)
        candidates = {"in_force": in_force.get(name), "rule_today": rule, "queued_0912": QUEUED_0912.get(name)}
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
                for label, value in candidates.items()
                if value
            },
        }
        print(f"{name:12s} sigma_30d {sigma_30:.3%}  q2.28 {quantile:.3%}  windows {rolling.size}")
        for label, value in candidates.items():
            if value:
                share = float((rolling <= -value).mean())
                print(f"    {label:12s} -{value:.1%}  {value / sigma_30:5.2f} sigma  P(30d <= -x) {share:.3%}")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {out}")
    if keep_series:
        # The rolling series beside the JSON, so a threshold nobody listed can be read later without re-running.
        # Only for an explicit path: `scratchpad/*.json` is ignored, a parquet there would not be.
        pd.DataFrame(series).to_parquet(out.removesuffix(".json") + ".parquet")
        print(f"wrote {out.removesuffix('.json')}.parquet")


if __name__ == "__main__":
    chosen = sys.argv[1] if len(sys.argv) > 1 else "panel"
    if chosen not in ("panel", "pit"):
        raise SystemExit(f"universe must be panel or pit, got {chosen!r}")
    stamp = f"{datetime.now(UTC):%Y%m%d}"
    explicit = len(sys.argv) > 2
    main(chosen, sys.argv[2] if explicit else f"scratchpad/probe_stop_tails-{chosen}-{stamp}.json", keep_series=explicit)
