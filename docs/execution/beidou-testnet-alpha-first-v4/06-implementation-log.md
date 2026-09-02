# Incident remediation implementation log

## Safety containment

- Engaged and retained `.beidou/testnet_verification/KILL_SWITCH`.
- Booted out and disabled `com.beidou.testnet-verify`.
- Confirmed no verifier process restarted during a greater-than-70-second
  observation.

## Code changes

- Removed unbounded verifier launchd deployment files and runbook procedure.
- Added confirmed-write `--once` validation and architecture regression.
- Scoped manifest real-write facts to the current runtime and made effective
  authority kill-switch aware.
- Added signed-flat plus matching-reduce-close recovery for durable FILLED
  traces.
- Restored the frozen legacy launcher's G5 certificate preflight.

## Test reliability

- Explicitly pinned PostgreSQL DSN in the clean-checkout preflight fixture.
- Prevented repository evidence rescans in engine lifecycle tests.
- Made realtime clock and sleep doubles deterministic and yielding.

## Candidate verification

- Committed the remediation as five scoped commits ending at validated code
  SHA `490ee9f697a791520a74a801331ad78f7ff81e24`.
- Ran clean detached full regression and coverage: `4612 passed`,
  `45891/45891` statements, `100.00%`.
- Ran Ruff format/lint, mypy, compileall, registry independent oracle,
  test-quality/hardcoded/forbidden-pattern scans, package validator, Bandit and
  dependency audit; all project-controlled checks passed with the local
  `beidou` distribution excluded from PyPI vulnerability lookup as expected.
- Pushed the remediation and documentation commits, opened draft PR #12, and
  re-ran every local CI-equivalent gate on exact PR head
  `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`. Full repository and Alpha V3
  line/branch gates both passed at `100%`.
- Repeated signed read-only account reconciliation on 2026-08-30: one-way
  mode, zero nonzero positions, zero regular/algo open orders, and durable
  unresolved count `0`.

No Testnet write, kill-switch removal, deployment or Mainnet action was part
of this verification.

## 2026-08-30 bounded-attempt remediation

- Consumed one explicit user-authorized Demo Testnet attempt while keeping
  `--once`, 25 USDT notional, 3x leverage, one configured instrument and the
  reduce-only close option.
- Restored the durable kill switch on process exit and reconciled the account
  to one-way, flat, no regular/algo orders, and unresolved `0`.
- Recorded the attempt as `NOT_VERIFIABLE` and zero-write; no automatic second
  episode was started.
- Added a startup boundary that quarantines restored ACTIVE symbols outside
  the current bounded exchangeInfo candidate universe. This preserves
  reduce-only exit handling while preventing a smaller `max_instruments`
  configuration from inheriting a larger risk-increasing universe.
- Added a restore-from-five-to-one regression test and rebuilt the mechanical
  write-capability source digest/line identity without changing the existing
  `HARD_HOLD` governance decision.

## 2026-08-30 completion-status follow-up

- Added a red regression proving that flat quarantined recovery cannot turn an
  ACTIVE no-action cycle into a completed verification campaign.
- Split quarantined recovery results from ACTIVE verification results for the
  overall completion decision. Quarantined exposure still uses the owned
  reduce-only path, but a safety exit alone leaves campaign status
  `NOT_VERIFIABLE`.
- Rebuilt the write registry and passed fresh local static, governance,
  security, full-coverage and Alpha branch-coverage gates.
- Consumed one newly authorized bounded Demo episode. It ended
  `NOT_VERIFIABLE` without an exchange write; no automatic retry occurred and
  the durable kill switch was restored before signed post-reconciliation.

## Bounded execution-probe soak implementation

- Added `apps.testnet_soak` as a finite foreground orchestrator. It delegates
  all exchange writes to the existing `VerificationRuntime` and contains no
  direct REST client, adapter, or order-writer implementation.
- Added immutable campaign bounds: 30 episodes, 3600 seconds, 120-second
  interval, 10 USDT per-order terminal cap, 25 USDT absolute account-exposure
  cap, 3x leverage, one allowlisted symbol, and forced close-after-verify.
- Added a deterministic BUY/SELL alternating `ExecutionProbeKernel` labelled
  `EXECUTION_PROBE` and `alpha_evidence=false`.
- Added unique per-episode identity namespaces while preserving the original
  verifier's same-closed-bar idempotency when no namespace is supplied.
- Added between-episode signed reconciliation requiring one-way mode, every
  account position flat, no regular/algo orders, and zero durable unresolved
  traces. Any non-CLOSED sub-fact engages the durable kill switch before a
  final read-only reconciliation and stops the campaign.
- Added campaign manifests, cancellation/exception cleanup, no-sleep-after-
  final-episode behavior, and a CLI confirmation gate that rejects before
  runtime/network construction.
- Rebuilt and independently verified the write-capability registry. The new
  entrypoint remains `HARD_HOLD`; local test completion does not grant real
  Testnet authority.

## 100/500 USDT authorization revision

- Raised only the immutable execution-probe order cap from 10 to 100 USDT and
  absolute account-exposure cap from 25 to 500 USDT, including CLI validation;
  30 episodes, 3600 seconds, 120-second interval, 3x, one symbol, forced close
  and UNKNOWN-stop behavior were unchanged.
- Confirmed the old defaults failed the revised test before implementation,
  then passed focused, affected, registry and full 100%-coverage gates.
- Rebuilt the write-capability registry; independent oracle returned PASS with
  zero issues. Authorized source bundle SHA-256:
  `f2092dd35fccfb0ba5d1a12c37523a2840f4e75d3f519f2b2f815d96cce0e2fc`.
- Consumed one explicitly authorized Demo campaign. It produced 28 complete
  open/fill/reduce-only-close/reconcile cycles before the 60-minute budget
  prevented the next 120-second wait. No retry or parameter change occurred.

## Fixed start-to-start cadence

- Replaced the unconditional post-episode 120-second sleep with an absolute
  deadline derived from campaign start plus `episode_number * interval`.
- Execution and signed reconciliation time now consume the interval; an
  overrun never causes a compensating negative sleep, and the existing
  campaign deadline still blocks a next start at or beyond the wall-clock cap.
- Added `cycle_interval_semantics=START_TO_START` to redacted config and
  manifests so timing evidence is hash-bound and unambiguous.
- Red/green, actual-composition, affected and full-repository gates passed.
  Current authorized source bundle SHA-256:
  `983f298ab9ed2fec8fdad23031104def95eba51ce5b68dceab0c6751c66cac44`.

## 2026-09-01 health-remediation completion pass

- Hardened Alpha research-data provenance and manifest validation across exact
  USD-M Kline endpoint classification, duplicate JSON keys, integer/range-safe
  timestamps, canonical content hashing, cross-source sample bounds and
  fail-closed backfill resealing. Focused data-boundary verification passed 62
  tests; `dataset_manifest.py` and `backfill.py` each reached 100% line
  coverage.
- Closed the final full-repository coverage gap by extending the existing safe
  CLI forwarding test to exercise every optional legacy-launch parameter. No
  runtime implementation or authorization boundary was changed.
- Final verification passed 4740 tests with 46455/46455 full-repository
  statements covered, and passed the exact Alpha V3 registry with 3497/3497
  statements plus 1070/1070 branches. Static, governance, security and
  dependency gates also passed on the same stable digest.
- Preserved operational containment: no service was loaded, no trading process
  was active, and the kill switch remained engaged. A GET-only Demo account
  refresh was bounded and interrupted after network timeouts; account truth is
  recorded as `UNKNOWN`. No exchange write or release action was performed.
