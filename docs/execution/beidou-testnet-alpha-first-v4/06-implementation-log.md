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

No Testnet write, kill-switch removal, deployment or Mainnet action was part
of this verification.
