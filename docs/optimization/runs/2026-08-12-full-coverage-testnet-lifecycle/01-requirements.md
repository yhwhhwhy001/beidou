# Requirements

| ID | Requirement | Acceptance evidence | Status |
|---|---|---|---|
| FC-001 | Genuine full-repository line coverage reaches 100% | Fresh coverage JSON and configured `--cov-fail-under=100` pass | IN_PROGRESS: 74.1790% baseline |
| FC-002 | Coverage is behavior-backed | Assertions cover success, failure and invariants; no new exclusions, pragma abuse or execution-only tests | IN_PROGRESS |
| FC-003 | Historical defects found while covering code are repaired | Red-green regression tests and implementation log | IN_PROGRESS |
| TX-001 | Target cannot be mistaken for Mainnet | Strict Testnet host allowlist and exchange identity | PASS |
| TX-002 | Real submit/query/final-state lifecycle is verified | Unique client ID, venue ACK/readback and terminal order state | BLOCKED_BY_EXISTING_UNKNOWN_EXPOSURE |
| TX-003 | Filled exposure, reconciliation and protection are verified | Venue order/position/protection facts and final flat reconciliation | BLOCKED_BY_EXISTING_UNKNOWN_EXPOSURE |
| TX-004 | No unrelated account state is changed | Before/after order and position reconciliation | PENDING |
| QG-001 | Full tests, Ruff, format, mypy and repository scans pass | Fresh full gate outputs | PENDING |

## Non-goals

- No Mainnet or real-money operation.
- No transfer, withdrawal, credential, permission or account-risk configuration change.
- No service restart or deployment without separate authorization.
- No weakening of coverage scope or trading safety gates to claim completion.
