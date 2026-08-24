# Alpha V3 Baseline Drift Report

Run: `2026-08-21-alpha-v3`

## Baseline comparison

| Item | Evidence | Result |
|---|---|---|
| Package baseline | `b156881710aeeec278bfbbe75973aa8dbc83b614` | Recorded |
| Current HEAD | `b156881710aeeec278bfbbe75973aa8dbc83b614` | Exact SHA match |
| Initial working tree | baseline observation | Clean |
| Current working tree | isolated implementation worktree | Dirty by authorized changes |
| Package manifest | all 13 file SHA-256/byte checks | PASS |
| Baseline targeted tests | 41 passed before implementation | PASS |

The Git SHA did not drift. The package assertions had semantic drift from the
locked source snapshot; the current worktree now contains the required adapter,
contract, gate, test, registry and evidence changes.

## Semantic differences resolved in the worktree

1. The package-named `MarketStateEstimator` was previously an always-UNKNOWN
   skeleton, while the engine had a separate legacy estimator. The V3 canonical
   estimator now owns deterministic state, regime probabilities, quality and
   policy/hash evidence; legacy dictionaries remain boundary projections only.
2. The running strategy path previously entered through `kernel_parity`. The V3
   state hash and context hash are now propagated through the typed kernel and
   read-only runtime trace without creating a second execution path.
3. Benchmark, attribution, AlphaForecast, Trend, Relative Strength, Breakout,
   Residual Momentum, calibration, fusion, ExposureGovernor and active portfolio
   contracts are now present with negative/fail-closed tests.
4. V3 portfolio sizing rejects missing venue/cost/liquidity facts; the old
   strength-based optimizer is explicitly baseline/test-only.

## Final implementation and verification

- Full repository tests: `3400 passed`.
- CI global coverage command: `80.59%`, above the preserved `78%` gate.
- User-authorized V3 gate: `3497` statements and `1070` branches, `0` missed and
  `0` partial, `100%` line and branch coverage across all 22 declared core
  modules.
- Fresh compileall, Ruff format/lint, full mypy, test-quality, hardcoded-value,
  forbidden-pattern, package validation and write-capability registry oracle:
  PASS.
- Bandit: no issues identified. pip-audit: no known vulnerabilities.

## Runtime boundary and restart evidence

- The isolated worktree was supplemented with the required writable runtime
  directories `.beidou/` and `evidence/bootstrap/`; Paper preflight then passed.
- The modified worktree was started twice in Paper mode on `127.0.0.1:19090`
  (`BTCUSDT` only), with `write=OFF`, `NO_NEW_RISK`, and `--no-self-heal`.
- Both starts exposed healthy liveness/HTTP/market-data/algorithm-probe facts;
  `/ready` correctly returned `503` because protection ownership and
  reconciliation facts were UNKNOWN. Both stops closed port 19090 and persisted
  `phase=STOPPED`; unowned orders were left untouched.
- The original Testnet PID 98924 in `/Users/maguannan/beidou` remains a separate,
  untouched process on port 9090. Its state is not evidence for this worktree.

## Remaining evidence boundary

- `G-A7` remains `FAIL/NOT_VERIFIABLE`: no sealed real same-data/same-cost OOS
  window or completed Paper shadow window is present. Fixture tests are not
  economic evidence.
- The Paper runtime observed `SIGNED_POLICY_UNAVAILABLE`, unowned protections and
  reconciliation UNKNOWN, so it correctly remained degraded and non-ready. No
  synthetic signed policy, account fact or protection state was invented.
- No Paper promotion, live activation, Mainnet action, exchange write, order
  placement/cancellation, deployment or external message was performed.

Fresh gate adjudication is recorded under
`docs/optimization/runs/2026-08-21-alpha-v3/`.
