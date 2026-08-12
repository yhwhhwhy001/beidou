# Regression report

## Fresh local evidence

| Gate | Result | Evidence |
|---|---|---|
| Full pytest | PASS | 1848 collected; 1847 passed, 1 skipped, 2 deprecation warnings |
| Testnet bypass mutation guard | PASS | The guard failed when a concurrent change re-enabled Testnet DEV_BYPASS, then passed after the bypass was removed |
| Changed-file critical lint | PASS | Ruff `F,E9` clean on the eight final modified source/test files |
| Changed-file format | PASS | Ruff format check: eight files already formatted |
| Type checking | PASS | Full configured mypy package/app scope passed; final rule/truth recheck passed |
| Compile/diff integrity | PASS | `py_compile` and `git diff --check` passed |
| Coverage threshold | FAIL | 64.25% total, below required 85%; the measurement also observed the transient concurrent bypass before it was fixed |
| Full Ruff lint | FAIL | 113 repository-wide findings remain |
| Full format gate | FAIL | 9 files were unformatted at measurement; files changed by this remediation were subsequently formatted and pass |
| Test-quality scan | FAIL | 7 findings: assertion detection, swallowed exceptions and a vacuous assertion |
| Hardcoded/silent-exception scan | FAIL | 1 `/tmp` warning and 16 swallowed-exception errors |

## Decision

The bounded order-flow remediation is functionally verified in the local test environment. The repository release gate is **FAIL/HOLD** because coverage and repository-wide quality gates do not meet policy. No Testnet or Mainnet readiness can be inferred from local tests.

## Unverified layers

- Real PostgreSQL outbox/WAL migration and restart replay
- User-stream disconnect, gap and reconnect behavior against a real venue
- Testnet submit/query/cancel/partial-fill/protection reconciliation
- Deployed process identity, loaded source SHA and post-restart observation
- Mainnet and real-fund behavior (prohibited and not authorized)
