# Code review

- Reviewer independence: fresh post-implementation review performed after the final full-suite,
  coverage, static-gate and restart runs; no prior PASS was reused.
- Scope: execution-package V3 implementation diff from baseline
  `b156881710aeeec278bfbbe75973aa8dbc83b614` in
  `/Users/maguannan/beidou-worktrees/alpha-v3-20260821`.
- Review timestamp: `2026-08-21T19:32:19+08:00`.
- Applicable instructions: execution package non-negotiables, repository fail-closed safety
  boundaries, no Mainnet/live write, no magic venue/cost defaults, UNKNOWN preservation, and
  the user's authorized local modifications plus isolated Paper restart.

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P1 | `beidou_research/backtest/alpha_v3_challenger.py:345-381`; `evidence/G-A7.json` | A7 cannot be accepted because real sealed same-data/same-cost OOS and Paper shadow evidence is absent. | Challenger correctly returns `NOT_VERIFIABLE` when `same_data`, `same_cost_model`, `point_in_time` or `paper_shadow` is missing; fixture tests pass but do not prove economics. | Keep `G-A7=FAIL`, do not promote or claim V3 superiority; supply externally governed evidence in a later run. | OPEN / external evidence |
| P1 | `beidou_strategy/alpha/pipeline.py:70-134`; `beidou_launcher/runtime.py:67-87,151-164` | V3 is intentionally a read-only shadow chain. Missing published calibration, derivative risk, account or venue facts produces `NOT_VERIFIABLE`/zero target; it is not an active order path. | Tests cover calibration rejection, no-order-intent and duplicate-path boundaries; runtime Paper probe reported algorithm probe PASS while keeping write interlock and readiness fail-closed. | Preserve the shadow/cutover boundary; require a separate authorized activation review after A7 and signed governance evidence. | ACCEPTED BY SCOPE |
| P1 | Paper restart evidence: `evidence/RESTART.json`; runtime safety checks in `beidou_launcher/runtime.py:279-320` | The environment lacks a valid signed policy and has UNKNOWN protection ownership/reconciliation; `/ready=503` and `NO_NEW_RISK` are correct. Treating this as ready would be unsafe. | Two isolated Paper starts returned `/health=200`, liveness/algorithm probe/write interlock PASS, but `/ready=503`; both stopped with port closed and unowned orders untouched. | Obtain real signed policy and independently reconcile protection/order facts before any resume; do not inject defaults or clean UNKNOWN state. | OPEN / environment evidence |
| P2 | `beidou_core/engine.py:1258-1285,2145-2150`; `beidou_strategy/components/mean_reversion_fixed.py`; `beidou_strategy/portfolio/simple_optimizer.py` | Legacy V2 estimator/projection and optimizer compatibility surfaces remain present. | `RealMarketStateEstimator` delegates to canonical `MarketStateEstimator`; compatibility modules have no-new-V3-caller tests and are documented as removal-gated. | Retain explicit boundary now; remove only after a separately approved compatibility-removal gate. | ACCEPTED / removal gate |

## Review checks

- Critical path review: canonical state → five forecasts → calibration → N-entry fusion →
  ExposureGovernor → active optimizer → attribution/trace was traced through the source and tests.
- Failure-path review: non-finite data, missing cost/venue/risk/account facts, invalid calibration,
  derivative risk UNKNOWN, conflict and protection/reconciliation UNKNOWN all remain fail-closed.
- Test-validity review: `tests/unit/test_alpha_v3_core_coverage_complete.py` contains semantic
  negative/boundary assertions; `tests/integration/test_alpha_v3_challenger.py:47` preserves
  NOT_VERIFIABLE for missing economic evidence. Coverage is not used as economic evidence.
- CI review: `.github/workflows/ci.yml:78-113` uses the global `85%` gate and the explicit
  100% line/branch gate for the 22 V3 modules; the dependency audit is bound to `python -m pip_audit`.
- Fresh evidence: `3532` tests passed; global coverage `85.02117828263381%`; V3 coverage 3497 statements/1070
  branches at 100%/100%; compileall, Ruff, mypy, test-quality, hardcoded, forbidden, package,
  registry oracle, Bandit and `python -m pip_audit` passed; `git diff --check` passed.

## Decision

`PASS_WITH_CONDITIONS`

The reviewed code and safety boundaries are acceptable for the authorized offline/read-only
shadow scope. The remaining conditions are explicitly external and blocking: A7 economic
evidence, signed runtime governance, and protection/reconciliation facts. No production,
Paper-promotion or Mainnet/live readiness claim is supported by this review.
