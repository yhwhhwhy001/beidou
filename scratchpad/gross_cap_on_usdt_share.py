"""What the live gross cap costs when it binds: research replays max_gross 2.0 on total equity, the loop on USDT.

2026-09-28 (operator ruling risk-g11-denominator, #237) the live guard multiplies `max_gross` by
min(1, usdt_equity / equity).  The research path (`beidou_cli/research_book_eval.py:_book_guards`) reads
`max_gross` alone, so every validation report replays the cap at 2.0 x total equity.  On the live record the
USDT share sat at 0.555-0.561 from 10-08 to 10-18, so the loop's cap was ~1.10 x total equity, and it bound on
229 of 285 completed cycles.

This prices the same configuration (the registry's tsmom, pit universe, the profile's exits and guards, the
validation range end) under three caps: 2.0 (what the evidence replays), 1.10 (the live share), 0.90 (a lower
share).  No grid, no selection, no comparison feeding a choice of the strategy: it measures a constraint that is
already in force.  Not a trial, so no ledger row (same stance as scratchpad/probe_stop_tails.py).

Run from the MAIN checkout root (it reads `.beidou/data` relative to the cwd; a session worktree has none):
`.venv/bin/python scratchpad/gross_cap_on_usdt_share.py [out.json]`.  About 20 s.  2026-10-19 on eeb88eed: full-sample
Sharpe 1.8541 / 1.8121 / 1.7812 and OOS-tail (last 46,088 bars) 1.7621 / 1.7153 / 1.6794 at caps 2.0 / 1.10 / 0.90;
10.3% of bars carry a model gross above 1.10.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import UTC, datetime

from beidou_alpha.validation.pipeline import score_book
from beidou_cli.research_backtest_cmd import (
    _entry,
    _load,
    _membership,
    _model,
    _resolve_symbols,
)
from beidou_cli.research_book_eval import _book_guards, _exit_params
from beidou_live.composition import cost_model
from beidou_shared.config import load_yaml

ROOT = ".beidou/data"
PROFILE = "config/live.demo.yaml"
REGISTRY = "config/alpha_registry.yaml"
COSTS = "config/costs.yaml"
END = "2026-10-19"
CAPS = (2.0, 1.10, 0.90)


def main(out: str) -> None:
    profile = load_yaml(PROFILE)
    entry = _entry("tsmom", REGISTRY, "", "")
    chosen = _resolve_symbols(ROOT, "", "1h", "pit", profile)
    panel = _load(ROOT, chosen, "1h", None, END, True)
    membership = _membership(ROOT, "pit", panel, 0, profile)
    model = _model(entry, profile, "1h", None)
    cost = cost_model(load_yaml(COSTS), use_funding=True)
    weights, _combined, _per = model.evaluate(panel, membership)
    gross_model = weights.abs().sum(axis=1)
    exits = _exit_params(profile, True, "1h")
    base = _book_guards(profile, True)
    assert base is not None
    commit = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
    result: dict[str, object] = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "range": {"start": str(weights.index[0]), "end": str(weights.index[-1]), "bars": int(weights.shape[0])},
        "model_gross_quantiles": {q: float(gross_model.quantile(q)) for q in (0.5, 0.75, 0.9, 0.95, 0.99)},
        "share_model_gross_above": {str(c): float((gross_model > c).mean()) for c in CAPS},
        "caps": {},
    }
    print(json.dumps({k: result[k] for k in ("range", "model_gross_quantiles", "share_model_gross_above")}, indent=1))
    for cap in CAPS:
        guards = dataclasses.replace(base, max_gross=cap)
        scored, _priced = score_book(panel, weights, cost, execution="open_to_close", guards=guards, exits=exits)
        s = scored.summary()
        keep = {k: s.get(k) for k in ("annualized_sharpe", "net_return", "max_drawdown", "average_absolute_exposure", "turnover_units")}
        keep["guards"] = s.get("guards")
        # The last 365 days alone: the regime the live record sits in.
        tail = scored.portfolio_net.iloc[-8760:]
        keep["sharpe_last_365d"] = float(tail.mean() / tail.std(ddof=1) * (8760**0.5))
        # The evidence's walk-forward OOS is the tail of one full-sample series (oos_is_full_sample_tail, 46,088 bars).
        oos = scored.portfolio_net.iloc[-46088:]
        keep["sharpe_oos_tail_46088"] = float(oos.mean() / oos.std(ddof=1) * (8760**0.5))
        result["caps"][str(cap)] = keep  # type: ignore[index]
        print(cap, json.dumps(keep, default=str))
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else f"scratchpad/gross_cap_on_usdt_share-{datetime.now(UTC):%Y%m%d}.json")
