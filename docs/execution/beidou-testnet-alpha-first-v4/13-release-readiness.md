# Release readiness

Decision: `BLOCKED`

Candidate branch: `codex/v4-incident-remediation`
Validated code SHA: `490ee9f697a791520a74a801331ad78f7ff81e24`
Validated PR head: `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`
Incident baseline: `db2debcfed1a969e627b8a34b4e6bb89d8815184`
Mainnet: `PROHIBITED`

## Current evidence

- Testnet verifier launchd job: booted out and disabled.
- Durable kill switch: engaged.
- 2026-08-30 signed GET reconciliation: account readable, one-way mode, no
  nonzero positions, no regular/algo open orders, durable unresolved=`0`.
- DecisionTrace recovery: the sole FILLED trace linked to a later
  quantity/direction-matched CLOSED reduce-only trace; unresolved=`0`.
- Focused incident regressions: green.
- Clean detached G7 on validated PR head: full repository `4612 passed`,
  `45891/45891` statements; Alpha gate `3497` statements and `1070` branches;
  both `100%`. Registry oracle, Ruff, mypy, compileall, package validator,
  governance scans, Bandit and dependency audit passed.
- GitHub PR #12 Actions run `33252017084`: both required jobs failed before
  any step, with annotations requiring payment/spending-limit remediation.
  This is not a passing CI result.
- Independent reviewer attempt: no findings or decision were produced because
  the reviewer service reached its usage limit.

## Release blockers

1. AC-TN-017 requires current required GitHub jobs to complete successfully.
2. Independent review must confirm that current-run manifest semantics,
   restart recovery, write authority, and legacy G5 isolation are correct.
3. The historical episode count cannot be described as one bounded campaign
   because launchd KeepAlive exceeded the authorized scope.

## Rollback and safety state

Do not clear the kill switch or start another campaign. Keep the disabled
LaunchAgent installed only as inert local incident evidence until the user
chooses to remove it. Mainnet and production remain unauthorized.

Binary G9 decision: `BLOCKED`. This document is not
`GREEN_LIGHT_TO_SHIP`.

## Execution-probe soak readiness slice

Local code/test readiness: `READY_WITHIN_BOUNDS` for the current 30-episode,
3600-second, 100/500 USDT, 3x, one-symbol execution-probe configuration.

Runtime activation: `NOT_AUTHORIZED`. The global kill switch remains engaged
and the current request explicitly stopped before real Testnet. GitHub CI and
independent acceptance were outside this local-development phase and remain
unpassed; no release claim is inferred from the local result.

The authorized real campaign later validated 28 repeated submit/fill/
reduce-only-close/reconcile cycles but stopped on `TIME_BUDGET_EXHAUSTED`
before episodes 29-30. This is execution-path evidence, not a G9 release PASS;
runtime activation remains unauthorized after the consumed campaign.
