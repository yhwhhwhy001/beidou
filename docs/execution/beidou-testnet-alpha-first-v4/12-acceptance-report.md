# Acceptance report

| Requirement ID | Expected | Actual | Environment | Evidence | Result |
|---|---|---|---|---|---|
| AC-TN-001 | Mainnet/unsafe destination is hard denied | Guard rejects Mainnet, HTTP, credentials-in-URL, host confusion | local | `tests/unit/test_testnet_guard.py` | PASS |
| AC-TN-002 | Verifier does not require Ed25519/G5 certificate | CLI/runtime dependency boundary has no frozen certification imports | local | `tests/architecture/test_testnet_verification_boundaries.py`, CLI tests | PASS |
| AC-TN-003 | Signed REST retains Binance HMAC | Exact encoded query signature contract passes | local | `tests/unit/test_binance_rest_client.py` | PASS |
| AC-TN-004 | Pool comes from exchangeInfo/market facts with source hashes | Runtime fixture consumes dynamic exchangeInfo and records source hashes | local fixture | runtime integration tests | PASS_WITH_CONDITIONS; live source not verified |
| AC-TN-005 | Only ACTIVE pool symbols can increase risk | Lifecycle/runtime tests block non-active paths | local fixture | pool/runtime tests | PASS_WITH_CONDITIONS; live quarantine not verified |
| AC-TN-006/007 | StrategyKernel and active components affect proposal/trace | Kernel is evaluated and component/proposal hashes are persisted; cross-mode parity is explicitly NOT_RUN rather than self-certified | local fixture | runtime integration and kernel contract tests | PASS_WITH_CONDITIONS; live cycle and frozen-input parity not verified |
| AC-TN-008 | One sizing authority | Verifier calls canonical adaptive sizing and architecture boundaries pass | local | sizing/runtime/architecture tests | PASS |
| AC-TN-009/010 | Venue leverage and final quantity are bound and ACKed | Adapter contracts and runtime fault fixtures pass | local fixture | Binance contract/runtime tests | PASS_WITH_CONDITIONS; real venue ACK absent |
| AC-TN-011 | PREPARED is durable before write | Append/fsync store and runtime ordering tests pass | local | `tests/unit/test_decision_trace.py`, runtime tests | PASS |
| AC-TN-012/013 | UNKNOWN uses same-id query and ACK identity checks | Fault fixtures distinguish query-found, deterministic reject, UNKNOWN, partial-fill polling and owned remainder cancellation | local fixture | runtime/adapter tests | PASS |
| AC-TN-014/015 | Position reconciliation and reduce-only close converge | Simulated fill/partial-fill/close paths reach zero unresolved facts; UNKNOWN close is not completed | local fixture | runtime integration tests | PASS_WITH_CONDITIONS; live fill absent |
| AC-TN-016 | Trace covers pool through execution truth | Trace stores pool, factor, strategy, portfolio, sizing, rule, ACK, position, and reconciliation fields | local fixture | runtime/trace tests | PASS_WITH_CONDITIONS; live trace absent |
| AC-TN-017 | Current HEAD CI is green | `4251` tests, mypy, compile, scans, package validation, Bandit and write-registry oracle pass; full repository coverage is `98.04% < 100%`, and no candidate GitHub CI run exists yet | local/CI unavailable | fresh 2026-08-29 local gate outputs | BLOCKED |
| AC-TN-018 | README/CLI point to one verifier and distinguish alpha | README/CLI/help, installed-wheel smoke, and offline manifest agree on `apps.testnet_verify` | local | `README.md`, `python -m apps.testnet_verify --help`, `evidence/testnet-verification/20260828T122130Z-9b558584273c/manifest.json` | PASS |

## Independent verifier

The write-capability registry was checked by the independent repository oracle
after regeneration and returned `{"issues": [], "status": "PASS"}`. The
acceptance report itself is not an independent human sign-off. Local fixture
tests are not real Binance Testnet evidence.

Evidence hashes: offline manifest SHA-256
`86144883431b5f71086ada6c3146a6d3035e1536fe571de51f33dc551c9acb2e`; registry
SHA-256 `bd7c31142139e961428e94f33eebe005a38037621b0b24869300422f9ee270be`;
registry governance digest
`b55c47fb11f6fe7b26d2a755cfef0d39fd1e68834620e40b9a04a307e5dfcaaf`.

## Economic Truth separation

E0 through E6 are `NOT_EVALUATED`. No Testnet execution fact can be promoted to
profitability or alpha certification, and the package's 30-episode stability
criterion has not been met with fresh evidence.

## Decision: BLOCKED

Local implementation contracts are `PASS_WITH_CONDITIONS`, but the Testnet
admission decision is `BLOCKED` by missing real Testnet credentials/evidence,
the failed 100% coverage release gate, absent candidate GitHub CI evidence,
and independent acceptance. No `Testnet Ready`, `Completed`, or `Alpha
VERIFIED` claim is authorized.
