"""D-020 arm split: what the crowding-off and crowding-on configurations each score, separately.

RESEARCH_LOG.md records the numbers this reproduces (off 1.6450 / t 3.7333, on 1.5291 / 3.4798,
per-fold-selected mixture 1.5445 / 3.5027) and points at `scratchpad/verify_d020.py`, which is not in
the repo - the same class of unresolvable pointer that commit 3a4d7fd went back and fixed for P14.
This is that script, restored by recomputation rather than by memory.

Why it matters: the headline in tsmom-validation-20260903T181803Z is the per-fold-selected MIXTURE
(`chosen_params` crowding_window [72, 0, 72, 72, 0]), so three of its five folds run a modifier the
registry does not.  Reading 1.5445 as the shipped configuration's score is wrong in both directions.

It does not touch reports/research/trials.jsonl: counting a verification re-run as new trials would
inflate the DSR denominator for every future report.  Pure computation, no side effects.

RESULT, 2026-09-05 - the recomputation DISAGREES with the log, and only on one arm:

    arm                        RESEARCH_LOG:385      recomputed (min_train 4000 / 8000)
    crowding off (registry)    1.6450 / t 3.7333     1.6521 / 3.7824    1.7122 / 3.7301
    crowding on                1.5291 / t 3.4798     1.7647 / 4.0266    1.7850 / 3.8776

The off arm reproduces; the on arm does not, and it moves the comparison the other way.  min_train is
not the cause (both fold protocols agree).  The arm that differs is the only one that consumes funding,
and RESEARCH_LOG:353 records that before D-023 a funding-consuming configuration could run silently
without the history - "把 `crowding_window` 改回 72 现在要么正确运行、要么明确失败，不会再有第三种结果"
exists because there had been a third outcome.  181803Z ran 2026-09-03, before that hole was closed;
the funding panel is complete today (977,327 settled cells, 205/205 symbols, from 2021-01-01).

That is a hypothesis with named evidence, not a finding: this script cannot show what 181803Z was fed.
What it does show is that the number the crowding decision rests on is not reproducible today.  Flipping
`crowding_window` on this basis would be wrong - a registered change needs `research validate`, which
registers its trials.  This only says the question is open again.
"""

from __future__ import annotations

from beidou_alpha.backtest import run_backtest
from beidou_alpha.validation.walk_forward import walk_forward_evaluate, walk_forward_folds
from beidou_cli.research_cmd import _entry, _load, _membership, _model, _resolve_symbols
from beidou_live.composition import cost_model
from beidou_shared.config import load_yaml

FOLDS, MIN_TRAIN, PURGE = 5, 4_000, 50  # the registry's recorded protocol for tsmom
ARMS = {"crowding off (registry)": 0, "crowding on": 72}


def main() -> None:
    profile = load_yaml("config/live.demo.yaml")
    cost = cost_model(load_yaml("config/costs.yaml"), use_funding=True)
    chosen = _resolve_symbols(".beidou/data", "", "1h", "pit")
    panel = _load(".beidou/data", chosen, "1h", None, None, True)
    membership = _membership(".beidou/data", "pit", panel, 0)
    bpy = panel.bars_per_year

    nets: dict[str, object] = {}
    for label, window in ARMS.items():
        entry = _entry("tsmom", "config/alpha_registry.yaml", f'{{"crowding_window": {window}}}')
        model = _model(entry, profile, "1h", None)
        weights, _c, _p = model.evaluate(panel, membership)
        nets[label] = run_backtest(panel, weights, cost).portfolio_net

    length = len(next(iter(nets.values())))
    folds = walk_forward_folds(length, FOLDS, min_train=MIN_TRAIN, purge=PURGE)
    print(f"pit universe  bars={length}  folds={FOLDS}  min_train={MIN_TRAIN}  purge={PURGE}\n")
    print(f"{'arm':<26} {'OOS Sharpe':>11} {'NW t':>8} {'consistency':>12}   folds")
    print("-" * 88)

    for label in ARMS:
        result = walk_forward_evaluate({label: nets[label]}, {label: {}}, folds, bpy)
        summary = result.summary(bpy)
        per_fold = [f"{s:.2f}" if s is not None else "  na" for s in summary["fold_sharpes"]]
        print(
            f"{label:<26} {summary['oos_sharpe']:>11.4f} {summary['oos_t_stat']:>8.4f} "
            f"{summary['fold_consistency']:>12.2f}   [{', '.join(per_fold)}]"
        )

    mixed = walk_forward_evaluate(nets, {k: {"crowding_window": v} for k, v in ARMS.items()}, folds, bpy)
    ms = mixed.summary(bpy)
    picked = [p.get("crowding_window") for p in ms["chosen_params"]]
    print(
        f"{'per-fold-selected mixture':<26} {ms['oos_sharpe']:>11.4f} {ms['oos_t_stat']:>8.4f} "
        f"{ms['fold_consistency']:>12.2f}   picked {picked}"
    )
    print("\nThe mixture is what the report headlines; neither arm is what it scores.")


if __name__ == "__main__":
    main()
