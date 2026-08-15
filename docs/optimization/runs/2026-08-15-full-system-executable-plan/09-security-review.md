# Security review

## Assets and trust boundaries

- Assets: exchange credentials, account/position/order/protection truth, risk approvals, intent/outbox/ledger records, certificates, Git history and running process state.
- Trust boundaries: thread authorization→local agent; current checkout→isolated worktree; launcher/scripts→Engine/adapter; producer→verifier; local facts→PostgreSQL/exchange; code PASS→runtime activation.
- The existing Testnet process is outside the implementation mutation boundary and remains UNKNOWN.

## Authorization and data handling

- Authorized: offline source/test/docs, isolated worktree, commit and push main after complete verification.
- Not authorized: reading secrets/account state, exchange writes, running DB writes, process restart, deployment, Mainnet or real funds.
- Do not read, print, copy or persist secret values. Tests use non-secret sentinels and network-disabled fakes until a separately authorized integration gate.

| Severity | Risk | Evidence | Mitigation | Status |
|---|---|---|---|---|
| P0 | Current Testnet process could be affected by editing its checkout | PID 15276 runs from /Users/maguannan/beidou | Develop in separate worktree; do not stop/restart/activate | CONTROL_SELECTED |
| P0 | Direct adapter/script path can bypass authorization | BYP-002/003/007/010 | Write Capability Registry + terminal choke-point + negative tests | OPEN_IMPLEMENTATION |
| P0 | UNKNOWN cancel/reduce/delete can touch unowned objects | RISK-P0-013 | All terminal writes require owned object facts; UNKNOWN means zero write | OPEN_IMPLEMENTATION |
| P0 | Producer/verifier can self-approve fake evidence | E-003/E-011 | Approved verifier digest, dual Owner rotation, anti-rollback | OPEN_DECISION |
| P0 | Secret/default policy generation in startup | start_beidou.sh evidence | Remove side effects; no value inspection | OPEN_IMPLEMENTATION |

## Decision

APPROVED_WITH_CONDITIONS for TASK-M00-C00 and subsequent offline, task-scoped implementation in an isolated worktree. All runtime, exchange, running-database and deployment mutations remain NOT_AUTHORIZED.
