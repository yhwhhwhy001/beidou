# Validation evidence

## Acceptance matrix

| Requirement | Result | Fresh controlled evidence |
|---|---|---|
| P1-EXEC-001 | PASS | SQLite restart, PostgreSQL atomic-plan SQL contract and engine multi-slice behavior tests |
| P1-EXEC-002 | PASS | Parent aggregate and real engine-entry tests require all child venue ACKs |
| P1-EXEC-003 | PASS | Invalid transition, terminal regression, command-hash and duplicate-event tests |
| P1-EXEC-004 | PASS | Long, short, flat, current-position, reversal and in-flight delta matrix |
| P1-EXEC-005 | PASS | Partial-fill replay and restart `SENDING -> UNKNOWN` tests |
| P1-EXEC-006 | PASS_CONTROLLED | Migration and fenced SQL adapter tests; live PostgreSQL remains NOT_VERIFIABLE |
| P1-EXEC-007 | PASS_CONTROLLED | Partial/fill/duplicate/non-aggregate user-event projection tests; live WebSocket remains NOT_VERIFIABLE |
| P1-EXEC-008 | PASS_SCOPED | Full functional tests, full mypy, critical Ruff, format and diff checks pass |

## Passing gates

- Final full functional regression: 1886 passed, 1 skipped.
- Full strict mypy: zero errors.
- Touched execution boundary Ruff and critical correctness rules: pass.
- Touched-file format check and `git diff --check`: pass.
- New `command_aggregate.py`: 98.99% line coverage (198 statements, 2 missed), above the 95% critical-package target.

## Repository-level blockers not hidden by this P1 result

- Full repository coverage: 64.55% in the pre-final audit, below the configured 85% threshold.
- Full repository Ruff: 119 historical findings.
- Test-quality scan: 7 historical findings.
- Hardcoded scan: 16 swallowed-exception errors plus 1 temporary-path warning.
- Real PostgreSQL migration/crash recovery, double-worker fencing, live user-stream replay,
  Testnet restart and venue identity queries were not authorized and remain NOT_VERIFIABLE.

The P1 code slice can be committed, but overall trading/release readiness remains HOLD.

## Git evidence

- P1 implementation commit: `630a9fc94155702eb11a1713f1dc6738ac5d9a4b`.
- Direct push to `origin/main`: succeeded; final documentation commit records the completed run.
