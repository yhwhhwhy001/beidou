# Test strategy

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| Execution/risk/protection invariants | unit, component, contract | real domain code; network mocked only below adapter | local | CI/release | focused red-green suites |
| Engine readiness and fact chain | vertical integration | real state machines and SQLite; controlled exchange doubles | local | CI/release | engine contract tests |
| PostgreSQL/Outbox recovery | contract/integration | protocol-accurate connection doubles, then real configured service if available | local/integration | CI/release | recovery and idempotency tests |
| Repository 100% line target | full regression | real source tree | local | CI/release | coverage JSON and fail-under 100 |
| Exchange order lifecycle | live integration | real Binance USD-M Testnet | Testnet | pre-release | unique client ID, venue readback, terminal reconciliation |
| Existing account ownership/protection | live safety gate | real Binance USD-M Testnet | Testnet | pre-write | positions plus ordinary/Algo protections |

## Omissions and residual risk

- Real Testnet writes are stopped while six pre-existing positions have no observed ordinary or Algo protection orders and ownership is not proven.
- Coverage is not allowed to use exclusions, blanket mocks, empty assertions or import-only execution as substitutes for behavior.
