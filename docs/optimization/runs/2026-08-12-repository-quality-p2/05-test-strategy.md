# Test strategy

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| Static/type defects | repository gate | real source tree | local | CI/release | Ruff, format, mypy |
| Vacuous or swallowed test behavior | test-quality mutation review | real tests | local | CI | scanner plus focused tests |
| Critical execution/risk uncovered branches | unit/property/contract | real domain code; mocks only at network/DB boundary | local | CI/release | focused coverage and assertions |
| Cross-module regressions | full regression | real source tree | local | CI/release | full pytest |
| Testnet environment identity | HTTP/adapter integration | real Binance Testnet public API | Testnet | pre-release | host allowlist, server time, exchange info |
| Testnet authenticated identity | adapter/account integration | real Testnet credential if present | Testnet | pre-release | redacted permissions/positions summary |
| Order mutation paths | omitted | none | none | blocked | not authorized |

## Omissions and residual risk

- No order lifecycle, partial fill, cancel or protection mutation can be certified from read-only connectivity.
- No Mainnet, restart/deployment or real PostgreSQL crash exercise is authorized.
- Line coverage is supporting evidence; behavior and failure assertions remain mandatory.
