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

All changes are pending clean full candidate verification.
