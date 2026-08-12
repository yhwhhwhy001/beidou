# Test strategy

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| OFR-001 fail-closed parity | unit + architecture | controlled engine/supervisor fixtures | local | CI | red-green pytest output |
| OFR-002 approved intent integrity | unit + engine contract | controlled execution context | local | CI | plan/slice assertions |
| OFR-003 nonce replay | unit | real signer with temporary persistence | local | CI | first verify true, replay false |
| OFR-004 REST UNKNOWN/idempotency | contract | mocked HTTP transport only | local | CI | method and retry-attempt assertions |
| OFR-005 exact quantization | unit/property boundary cases | real Decimal/rule snapshot | local | CI | step/tick/min/max assertions |
| OFR-006 algorithm typing | unit | real algorithm objects | local | CI | Money and safe selector assertions |
| OFR-007 protection/UNKNOWN | store + engine contract | temporary local store fixtures | local | CI | conservative restore/coverage assertions |
| OFR-008 semantic detector | architecture mutation tests | repository source/AST | local | CI | injected bypass is detected |

## Omissions and residual risk

No real PostgreSQL, WebSocket, Testnet, exchange readback, chaos host, deployed runtime, or production telemetry is authorized. Those gates remain NOT_VERIFIABLE even if local tests pass.
