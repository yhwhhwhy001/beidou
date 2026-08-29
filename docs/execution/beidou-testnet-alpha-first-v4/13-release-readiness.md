# Release readiness

Decision: `BLOCKED`

Candidate branch: `codex/v4-incident-remediation`
Incident baseline: `db2debcfed1a969e627b8a34b4e6bb89d8815184`
Mainnet: `PROHIBITED`

## Current evidence

- Testnet verifier launchd job: booted out and disabled.
- Durable kill switch: engaged.
- Fresh signed GET reconciliation: account readable, one-way mode, no nonzero
  positions, no open orders, no open algo orders.
- DecisionTrace recovery: the sole FILLED trace linked to a later
  quantity/direction-matched CLOSED reduce-only trace; unresolved=`0`.
- Focused incident regressions: green.
- Clean full regression, 100% repository coverage, current registry oracle,
  Ruff, mypy, compile, packaging, and governance scans: pending on this
  candidate.
- GitHub Actions: runner allocation remains blocked by account
  billing/spending limits; this is not a passing CI result.

## Release blockers

1. AC-TN-017 requires current required GitHub jobs to complete successfully.
2. All clean candidate gates must be rerun after the final remediation diff.
3. Independent review must confirm that current-run manifest semantics,
   restart recovery, write authority, and legacy G5 isolation are correct.
4. The historical episode count cannot be described as one bounded campaign
   because launchd KeepAlive exceeded the authorized scope.

## Rollback and safety state

Do not clear the kill switch or start another campaign. Keep the disabled
LaunchAgent installed only as inert local incident evidence until the user
chooses to remove it. Mainnet and production remain unauthorized.

This document is not `GREEN_LIGHT_TO_SHIP`.
