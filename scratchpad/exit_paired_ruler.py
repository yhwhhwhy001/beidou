"""The paired ruler for exit-overlay decisions: NW t of the per-bar OOS difference, not two lone Sharpes.

Why (2026-09-08).  `docs/RESEARCH_LOG.md:481` already retracted the habit this fixes: judging an overlay
change against the 0.43 annualised standard error of a SINGLE Sharpe estimate, when the two books run on
the same data, the same folds and the same signals and are therefore ~99% correlated.  The quantity that
decides is the precision of the PAIRED difference.  P10 established the instrument
(`reports/research/paired-p10-band-20260904T030015Z.json`, NW t 4.182 on dSharpe 0.142); nothing has ever
applied it to the exit overlay, so every exits verdict so far has been read off two point estimates.

LEDGER: this compares only the pair the overlay reports ALREADY publish - the report baseline (no exits)
against the setting that is live today (`config/live.demo.yaml`).  It evaluates no new configuration and
can select nothing, so it is not a trial.  Any OTHER overlay setting run through this script IS a
diagnostic trial under the K-EX07 rule and must be declared and charged before it is run, even offline.

Writes nothing: no report, no ledger row, no config change.

    python scratchpad/exit_paired_ruler.py
"""

from __future__ import annotations

import math
import statistics as st
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from beidou_alpha.backtest import run_backtest
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.panel import interval_seconds
from beidou_alpha.validation.metrics import newey_west_tstat, sharpe
from beidou_alpha.validation.walk_forward import walk_forward_folds
from beidou_data.pool import MEMBERSHIP_FILE, membership_at_bars
from beidou_data.store import FundingStore, KlineStore
from beidou_live.composition import build_model, cost_model, load_panel, load_registry
from beidou_shared.config import load_yaml

ROOT = ".beidou/data"
INTERVAL = "1h"


def main() -> None:
    profile = load_yaml("config/live.demo.yaml")
    costs = load_yaml("config/costs.yaml")

    table = pd.read_parquet(Path(ROOT) / MEMBERSHIP_FILE).astype(bool)
    idx = pd.DatetimeIndex(table.index)
    table.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    store = KlineStore(ROOT)
    stored = set(store.symbols(INTERVAL))
    symbols = [str(s) for s in table.columns[table.any(axis=0)] if s in stored]
    if len(sys.argv) > 1 and sys.argv[1] == "static":
        # the 18 symbols the loop holds today - the same set P22's static report used
        import json as _json

        keep = set(_json.loads(Path(".beidou/live/state.json").read_text())["universe"]) & set(symbols)
        symbols = [s for s in symbols if s in keep]
        table = table[symbols]
        table.loc[:, :] = True  # static = always a member, which is what the static reports mean
    print(f"universe: {'static' if len(sys.argv) > 1 and sys.argv[1] == 'static' else 'pit'}, {len(symbols)} symbols")
    panel = load_panel(store, symbols, INTERVAL, funding_store=FundingStore(ROOT))
    membership = membership_at_bars(table, panel.index)

    registry = load_registry(Path("config/alpha_registry.yaml"))
    model = build_model(registry, profile)
    cost = cost_model(costs, use_funding=True)
    bpy = panel.bars_per_year
    weights, _c, _p = model.evaluate(panel, membership)

    bars_per_day = max(1, 86_400 // interval_seconds(INTERVAL))
    live = ExitParams.from_mapping({**(profile.get("exits") or {}), "bars_per_day": bars_per_day})
    print(f"live exits: sl={live.stop_loss} tr={live.trailing_stop} tp={live.take_profit} "
          f"cooldown={live.cooldown_bars} unit_mode={live.unit_mode}")
    overlay = apply_exits(weights, panel.close, live)
    print(f"exit events: {overlay.summary()}\n")

    net_a = run_backtest(panel, weights, cost, execution="open_to_close").portfolio_net
    net_b = run_backtest(panel, overlay.weights, cost, execution="open_to_close").portfolio_net
    folds = walk_forward_folds(len(net_a), 5, min_train=4000, purge=50)

    oos_a = pd.concat([net_a.iloc[f.test_slice] for f in folds])
    oos_b = pd.concat([net_b.iloc[f.test_slice] for f in folds])
    diff = oos_b - oos_a

    print(f"{'':<34}{'OOS Sharpe':>12}{'own NW t':>10}")
    print(f"{'A  no exits (report baseline)':<34}{sharpe(oos_a, bpy):>12.4f}{newey_west_tstat(oos_a)['t_stat']:>10.3f}")
    print(f"{'B  + live exits (sl6 + tp6)':<34}{sharpe(oos_b, bpy):>12.4f}{newey_west_tstat(oos_b)['t_stat']:>10.3f}")

    nw = newey_west_tstat(diff)
    print(f"\nPAIRED, per bar (B - A):  mean {diff.mean():.3e}  NW t = {nw['t_stat']:+.3f}  lags {nw.get('lags')}")
    print(f"  correlation(A, B) on OOS bars: {oos_a.corr(oos_b):.4f}")
    print(f"  dSharpe (B - A): {sharpe(oos_b, bpy) - sharpe(oos_a, bpy):+.4f}")

    per_fold = [sharpe(net_b.iloc[f.test_slice], bpy) - sharpe(net_a.iloc[f.test_slice], bpy) for f in folds]
    mean, se = st.mean(per_fold), st.stdev(per_fold) / math.sqrt(len(per_fold))
    print(f"\nPAIRED, fold level (5 folds): mean {mean:+.4f}  SE {se:.4f}  t = {mean / se:+.2f}")
    print(f"  per fold: {[round(x, 4) for x in per_fold]}")
    print(f"\n  for scale: the SE of a single Sharpe estimate on this history is ~0.43 (RESEARCH_LOG D-020)")
    print(f"  fold-level paired SE is {0.43 / se:.1f}x smaller; that ratio is what was misapplied.")


if __name__ == "__main__":
    main()
