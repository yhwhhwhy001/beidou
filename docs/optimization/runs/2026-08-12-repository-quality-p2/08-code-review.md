# Code review

- Reviewer independence: self-review required by current single-agent constraint; no independent agent requested by user
- Version/diff: baseline `9c03be4fcd07fb5b5b0e6001ceb10752899ed911`

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P0 | launcher/preflight, bootstrap/supervisor | Testnet could consume development bypasses and an automatically generated G5 PASS artifact | Source-path and behavior regression tests | Remove bypass routes and require externally produced certification evidence | FIXED |
| P0 | monitoring/account, core/engine | Testnet permission/protection failures could be downgraded relative to production | Environment parity tests | Apply identical fail-closed permission/protection semantics to every writable environment | FIXED |
| P0 | alpha/typed_graph | Risk-increasing output from an exit adapter became DEGRADED after exception handling | New legacy alpha regression test | Return BLOCK and retain error evidence | FIXED |
| P1 | position/execution/infra | Average price, cross-zero, reduce-only, protection replacement, ledger notional and retry identity had incomplete invariants | New legacy safety suite | Bind economic fields and require venue ACK/UNKNOWN resolution | FIXED |
| P1 | research generators/selection | GP offspring could repeat forever, last generation was unevaluated, Bayesian trials leaked across runs, ensemble mean used inconsistent sample count | New generator/selection suites | Bound search, reset run state and validate aligned samples | FIXED |
| P1 | mean reversion | Half-life used `-ln(2)/slope`, producing invalid negative estimates for stationary coefficients | Deterministic AR(1) regression test | Use `-ln(2)/ln(slope)` and reject non-stationary/invalid inputs | FIXED |
| P1 | recovery/versioning/universe batching | Restart counted at plan and execute; recovery target path could drift; versions sorted lexically; Testnet rotation starved symbols after first batch | New zero-coverage contract suite | Count at execution, persist atomically, sort numeric versions, rotate against full universe | FIXED |
| P1 | repository coverage | Core engine 27%, supervisor 45%, PostgreSQL store 44%, mining persistence 33% remain dominant uncovered areas | `artifacts/coverage-p2-final.json` | Add vertical integration/contract tests without exclusions or fake assertions | OPEN |

## Decision

CHANGES_VALIDATED; RELEASE HOLD because repository coverage is 74.18%, below the configured 85% gate. No unresolved test, Ruff, typing, test-quality or hardcoded-scan failure remains in this run.
