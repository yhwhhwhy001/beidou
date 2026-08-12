# P1 execution aggregate context

- Repository: `/Users/maguannan/beidou`
- P1 start baseline: `60eb91f931adc615181aa89d39d38ccdbfcf8b69`
- Final integration parent: `2afdbad5652aacd38099bb91b1c7a386490b7e65` on local and `origin/main`
- Concurrent history observed during implementation: `2d8b203`, `c66e073` and `2afdbad` restored Testnet `DEV_BYPASS`; this P1 diff removes those four Testnet bypass conditions because they violate the fail-closed environment-parity contract.
- Branch: `main`
- Authority: local source/test/document changes plus scoped commit and push to `origin/main`
- Not authorized: exchange submit/cancel/close, leverage or account mutation, service restart, deployment, Mainnet or real-fund operation
- Risk class: P0/P1 financial execution state, idempotency, restart recovery and portfolio exposure
- Selected specialists: TDD, incremental implementation, exchange execution validation, integration/contract testing, Git workflow, verification before completion
- Stop conditions: unknown environment identity, need for venue writes, destructive migration, evidence loss, unrelated worktree mutation, or inability to preserve UNKNOWN
