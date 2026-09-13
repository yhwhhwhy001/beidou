"""P32d: the q95 of a path-dependent control, replayed INSIDE each bootstrap path - and read by the
ruler the live loop actually uses.

Supersedes `p32c_rescaled_ladder.py`'s `q95_of` for every q95 / P(breach) number.  `p32c` is left
exactly as it ran, because its numbers are what E-010 and C-006 cite and rewriting the evidence under
a claim is how a record stops being one.

TWO defects, found by the 2026-09-14 audit
(`docs/analysis/2026-09-14-backtest-guard-k060-ladder-audit.md`):

1. `p32c.q95_of` block-bootstraps `thr` - a series whose throttle scalars were decided on the
   HISTORICAL ordering.  A draw that reaches -70% would have tripped the ladder far earlier than
   history did, so the shipped ladder's protection is missing from its own tail estimate.  Replaying
   the ladder inside each draw moves the shipped arm's q95 from -70.8% to -52.6% and reverses C-006.

2. Both arms were replayed against the book's mark-to-market NAV.  R8 does not read that.
   `attributed_drawdown_state` (`beidou_live/risk_budget.py:223`) reads `base + cumsum(attributed)` -
   income rows only, no unrealised P&L - so a book holding losers sees a far shallower drawdown than
   the one the operator is watching.  Measured by `p32e`: on this same panel the mark-to-market ruler
   spends 785 bars past the first rung and the realised ruler spends 0.

So the arms below are (ladder) x (ruler).  The drawdown REPORTED is always the mark-to-market one,
because that is the drawdown the operator's budget is about; the ruler only decides when the ladder
acts.

Inherited approximations, unchanged and stated: the scalar multiplies the realised net series rather
than re-deriving the weight path (P32b); the weekly block bootstrap destroys multi-month regime
structure, so every q95 here is OPTIMISTIC (A-003); and the realised/unrealised split is itself path
dependent, so resampling blocks of an already-split series carries each block's split from history.

    python scratchpad/p32d_ladder_bootstrap_pathwise.py pit|static [draws]
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "scratchpad")

from p32e_ruler_divergence import DATA, K, drawdown_path, realised_split

from beidou_alpha.backtest import run_backtest
from beidou_alpha.overlays.exits import ExitParams, apply_exits
from beidou_alpha.overlays.exposure import BookGuardParams
from beidou_cli.research_cmd import _load, _membership, _resolve_symbols
from beidou_governance.policy import Policy
from beidou_live.composition import build_model, cost_model, load_registry
from beidou_shared.config import load_yaml

BUDGET, BLOCK, SEED = 0.70, 168, 20260904


def rescaled(k: float) -> tuple[tuple[float, float], ...]:
    """The shipped rule transcribed to the -70% budget (P32c's E-010), not a new rule."""
    return ((-0.70 * BUDGET, 0.75 * k), (-1.00 * BUDGET, 0.50 * k))


def ladder(mark: np.ndarray, realised: np.ndarray, policy: Policy, ruler: str) -> tuple[np.ndarray, np.ndarray]:
    """(throttled mark-to-market returns, scalars).  `ruler` decides what the ladder reads."""
    equity = hwm = 1.0
    path = peak = 1.0
    cycles, standing = 0, None
    out = np.empty(len(mark))
    scalars = np.ones(len(mark))
    for t in range(len(mark)):
        drawdown = (equity / hwm - 1.0) if ruler == "mtm" else (path / peak - 1.0 if peak > 0 else 0.0)
        rung = policy.throttle_scalar(drawdown)
        if rung is None:
            cycles, standing = 0, None
        else:
            cycles += 1
            standing = rung if cycles > policy.drawdown_grace_cycles else standing
        scalar = (standing / K) if standing is not None else 1.0
        scalars[t] = scalar
        out[t] = scalar * mark[t]
        path += scalar * realised[t] * equity
        equity *= 1.0 + out[t]
        peak = max(peak, path)
        hwm = max(hwm, equity)
    return out, scalars


def main(mode: str, draws: int) -> None:
    profile = load_yaml("config/live.demo.yaml")
    registry = load_registry(Path("config/alpha_registry.yaml"))
    panel = _load(DATA, _resolve_symbols(DATA, "", "1h", mode), "1h", None, None, True)
    membership = _membership(DATA, mode, panel, 0)
    cost = cost_model(load_yaml("config/costs.yaml"), use_funding=True)
    exits = ExitParams.from_mapping({**(profile.get("exits") or {}), "bars_per_day": 24})
    pf = profile.get("portfolio", {}) or {}
    guards = BookGuardParams(max_weight=float(pf["max_weight"]), max_gross=float(pf["max_gross"]),
                             daily_loss_pause=float(profile["guards"]["daily_loss_pause"]))
    tuned = copy.deepcopy(profile)
    tuned.setdefault("portfolio", {})["vol_target"] = K
    w, _c, _p = build_model(registry, tuned).evaluate(panel, membership)
    w = apply_exits(w, panel.close, exits).weights if exits.enabled else w
    result = run_backtest(panel, w, cost, guards=guards)
    realised, mark = realised_split(
        np.nan_to_num(result.weights.to_numpy(dtype=float), nan=0.0),
        np.nan_to_num(result.asset_returns.to_numpy(dtype=float), nan=0.0),
        np.nan_to_num(result.costs.to_numpy(dtype=float), nan=0.0),
    )
    n = len(mark)
    blocks = n // BLOCK
    years = (blocks * BLOCK) / 8760.0
    # one seed, one set of block starts, reused by every arm and both methods: the comparison is PAIRED
    rng = np.random.default_rng(SEED)
    starts = np.stack([rng.integers(0, n - BLOCK, size=blocks) for _ in range(draws)])
    print(f"[{mode}] k={K} bars={n} blocks/draw={blocks} draws={draws}")
    print(f"{'ladder':>12}{'ruler':>10}{'A q95':>9}{'A P<-50':>9}{'B q95':>9}{'B P<-50':>9}"
          f"{'B CAGR':>9}{'B cost':>9}{'trip%':>8}")
    out = []
    for name, rungs in (("shipped", Policy().drawdown_ladder), ("rescaled", tuple(rescaled(K)))):
        policy = replace(Policy(), drawdown_ladder=tuple(rungs))
        for ruler in ("mtm", "realised"):
            thr, sc = ladder(mark, realised, policy, ruler)
            a = np.array([drawdown_path(np.cumprod(1.0 + np.concatenate([thr[s:s + BLOCK] for s in row]))).min()
                          for row in starts])
            b = np.empty(draws)
            cagr = np.empty(draws)
            raw = np.empty(draws)
            for d, row in enumerate(starts):
                m = np.concatenate([mark[s:s + BLOCK] for s in row])
                r = np.concatenate([realised[s:s + BLOCK] for s in row])
                stepped, _ = ladder(m, r, policy, ruler)
                b[d] = drawdown_path(np.cumprod(1.0 + stepped)).min()
                cagr[d] = np.prod(1.0 + stepped) ** (1.0 / years) - 1.0
                raw[d] = np.prod(1.0 + m) ** (1.0 / years) - 1.0
            row_out = {
                "ladder": name, "ruler": ruler, "rungs": list(map(list, rungs)),
                "A_q95": float(np.percentile(a, 5)), "A_p_breach_50": float((a <= -0.50).mean()),
                "B_q95": float(np.percentile(b, 5)), "B_p_breach_50": float((b <= -0.50).mean()),
                "B_cagr_median": float(np.median(cagr)),
                "B_cagr_cost_median": float(np.median(raw - cagr)),
                "B_cagr_cost_q90": float(np.percentile(raw - cagr, 90)),
                "historical_throttled_share": float((sc < 1.0 - 1e-9).mean()),
                "B_draws_that_ever_tripped": float(np.mean(raw - cagr > 1e-9)),
            }
            out.append(row_out)
            print(f"{name:>12}{ruler:>10}{row_out['A_q95']:>9.1%}{row_out['A_p_breach_50']:>9.1%}"
                  f"{row_out['B_q95']:>9.1%}{row_out['B_p_breach_50']:>9.1%}"
                  f"{row_out['B_cagr_median']:>9.1%}{row_out['B_cagr_cost_median']:>9.1%}"
                  f"{row_out['B_draws_that_ever_tripped']:>8.1%}")
    Path(f"scratchpad/p32d-{mode}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote scratchpad/p32d-{mode}.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pit", int(sys.argv[2]) if len(sys.argv) > 2 else 2000)
