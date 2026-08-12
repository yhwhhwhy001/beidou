# Context

- Run ID: `2026-08-12-order-flow-remediation`
- Repository/version: `/Users/maguannan/beidou`, `main@893c81dfabc4063f1428c85b844838cda4c32baf`, equal to `origin/main` at run start
- Applicable instructions: `00_EXECUTION_MASTER.md`, `01_AGENT_OPERATING_PROTOCOL.md`, ADR-001 single execution fact chain, CIOS quantitative-trading safety contract
- User authority: local source and test changes requested; no exchange writes, order/cancel, leverage change, service restart, deployment, Git commit, or Git push authority
- Technical stack: Python 3.14.6 virtual environment, pytest, asyncio, Binance USD-M adapter, PostgreSQL-backed execution facts with local test doubles
- Commands and baseline: full baseline suite collected 1832 tests; 1822 passed, 9 failed, 1 skipped; targeted source checks reproduced Testnet fail-open, HTTP method/retry, nonce, precision, and algorithm defects
- Dirty work / protected areas: worktree clean at run start; preserve all unrelated files and existing delivery packages
- Risks and unknowns: no current PostgreSQL, user-stream, exchange, or deployed-runtime evidence; production/Testnet readiness cannot be established by this run
- Selected specialists: test-driven-development, incremental-implementation, exchange-order-execution-validation, integration-and-contract-testing, verification-before-completion
- Omitted specialists: production-validation-and-rollback and shipping-and-launch are not authorized; performance optimization is not the primary safety bottleneck
- Stop conditions: any need for exchange writes, leverage/order changes, restart/deploy, credentials, destructive migration, Git commit/push, or weakening fail-closed behavior
- G0 decision: READY for local fail-closed remediation only
