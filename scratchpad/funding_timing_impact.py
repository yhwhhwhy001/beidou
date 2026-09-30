"""Zero ledger: what charging each settlement to the position held INTO its bar does to the shipped book.

backtest-guard 2026-09-25 (🔵) and 2026-09-30: the backtest charged a settlement floored into bar t to t's NEW
position, while the loop - trading about 27 s after the close that decided it - pays it on the position it held
into t.  Library calls only (`research backtest` writes a ledger row).  Engine-agnostic: the book is priced once
without funding, and each convention is subtracted from that, so the script reads the same before and after
the fix; the last line checks that the engine now charges the second one.

    PYTHONPATH=$PWD .venv/bin/python scratchpad/funding_timing_impact.py
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from beidou_alpha.overlays.exits import ExitParams
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_alpha.validation.metrics import compound, sharpe
from beidou_alpha.validation.pipeline import score_book
from beidou_cli.research_panel import _book_weights, _load, _membership, _resolve_symbols
from beidou_live.composition import cost_model, load_registry
from beidou_live.config import live_overlay_blocks
from beidou_shared.config import load_yaml

ROOT = "/Users/maguannan/beidou/.beidou/data"
BARS_PER_YEAR = 8760.0


def main() -> None:
    profile = load_yaml("config/live.demo.yaml")
    entry = next(e for e in load_registry("config/alpha_registry.yaml").strategies if e.id == "tsmom")
    panel = _load(ROOT, _resolve_symbols(ROOT, "", "1h", "pit"), "1h", None, None, funding=True)
    weights = _book_weights(entry, profile, "1h", None, panel, _membership(ROOT, "pit", panel))
    blocks = live_overlay_blocks(profile)
    guards = BookGuardParams(**(blocks["book_guards"] or {}))
    exits = None if blocks["exits"] is None else ExitParams.from_mapping(blocks["exits"])
    cost = cost_model(load_yaml(str(profile.get("costs", "config/costs.yaml"))))
    unfunded, _ = score_book(panel, weights, replace(cost, use_funding=False), guards=guards, exits=exits)
    funded, _ = score_book(panel, weights, cost, guards=guards, exits=exits)

    executed = unfunded.weights
    funding = panel.funding[executed.columns].reindex(executed.index).fillna(0.0)
    at_new_position = (executed * funding).sum(axis=1)
    at_held_position = (executed.shift(1).fillna(0.0) * funding).sum(axis=1)
    before, after = unfunded.portfolio_net - at_new_position, unfunded.portfolio_net - at_held_position
    pauses = int(funded.guard_events["daily_loss_pause"].sum()) if funded.guard_events is not None else None
    print(f"bars {len(executed)} ({len(executed) / BARS_PER_YEAR:.2f} years), {executed.index[0]} .. {executed.index[-1]}")
    print(f"daily-loss pauses in the funded replay: {pauses} (the two conventions share one path only while this is 0)")
    print(f"funding summed per bar: at the new position {at_new_position.sum():+.6f}, at the held one {at_held_position.sum():+.6f}")
    print(f"settlement bars where the two differ: {int((np.abs(at_held_position - at_new_position) > 0).sum())}")
    print(f"sharpe: new position {sharpe(before, BARS_PER_YEAR):.6f}, held position {sharpe(after, BARS_PER_YEAR):.6f}")
    print(f"compound: new position {compound(before):+.4f}, held position {compound(after):+.4f}")
    engine = funded.portfolio_net
    print(f"the engine charges the held position: {bool(np.allclose(engine, after, rtol=0.0, atol=1e-15))}")


if __name__ == "__main__":
    main()
