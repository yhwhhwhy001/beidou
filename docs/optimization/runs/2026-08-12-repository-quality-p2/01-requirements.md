# Requirements

## Goal

Remove repository-wide historical quality failures, raise test coverage toward the configured release threshold, and collect bounded real Testnet connectivity evidence without weakening trading safety.

## Scope

- Full Ruff, formatting, mypy, test-quality and hardcoded-pattern gates.
- Risk-based tests for uncovered production code, prioritizing execution, risk, exchange, reconciliation and launcher boundaries.
- Full regression and coverage measurement.
- Read-only Binance USD-M Testnet endpoint, clock, exchange metadata, credential identity/permission and account-state validation when safely available.

## Non-goals

- No Mainnet or real-money validation.
- No order, cancellation, leverage, margin, balance, permission, protection or trading-mode mutation.
- No service restart, deployment, commit or push without separate authorization.
- Coverage will not be raised through exclusions, pragma abuse, fake assertions or tests that only execute lines without checking behavior.

## Requirements

| ID | Requirement | Acceptance criteria | Risk | Status |
|---|---|---|---|---|
| P2-Q-001 | Full Ruff and format gates pass | Zero repository findings under configured commands | Medium | PASS |
| P2-Q-002 | Test-quality and hardcoded scans pass | Zero error findings; any warnings explicitly justified | High | PASS |
| P2-Q-003 | Strict typing remains clean | Full configured mypy exits zero | High | PASS |
| P2-Q-004 | Coverage materially increases without weakening policy | Risk-backed tests increase full coverage; configured 85% is the target and any gap remains explicit | High | PARTIAL: 64.70% → 74.18%; 85% unmet |
| P2-Q-005 | Existing behavior remains stable | Full pytest regression passes | High | PASS: 1,956 passed |
| P2-X-001 | Exchange target is unambiguously Testnet | Host is Testnet and Mainnet host checks fail closed | Critical | PASS |
| P2-X-002 | Read-only exchange contracts are real | Fresh server time/exchange metadata and, if credentials are present, account permission/position facts are obtained without mutations | Critical | PASS_GET_ONLY |
| P2-X-003 | External evidence is redacted and bounded | No secret/signature/account-sensitive payload is persisted in reports | Critical | PASS |

## Unknowns and decisions

- Credential availability and exact Testnet permission scope will be inspected without printing values.
- An authenticated account read is permitted only after endpoint identity is independently proven.
- External order writes remain blocked because the user authorized connectivity validation, not bounded order actions.
