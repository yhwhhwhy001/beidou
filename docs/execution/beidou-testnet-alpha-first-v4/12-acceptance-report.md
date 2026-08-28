# Acceptance report (updated 2026-08-29 after V4 gap-fix campaign)

| Requirement ID | Expected | Actual | Environment | Evidence | Result |
|---|---|---|---|---|---|
| AC-TN-001 | Mainnet/unsafe destination is hard denied | Guard rejects Mainnet, HTTP, credentials-in-URL, host confusion | local + live | `tests/unit/test_testnet_guard.py`, `beidou_exchange/testnet_guard.py` | PASS |
| AC-TN-002 | Verifier does not require Ed25519/G5 certificate | CLI/runtime dependency boundary has no frozen certification imports; real campaign ran with API key/secret + local confirm only | local + live | `tests/architecture/test_testnet_verification_boundaries.py`, campaign manifests | PASS |
| AC-TN-003 | Signed REST retains Binance HMAC | Exact encoded query signature contract passes | local | `tests/unit/test_binance_rest_client.py` | PASS |
| AC-TN-004 | Pool comes from exchangeInfo/market facts with source hashes | Live pool built from demo-fapi exchangeInfo with 5 ACTIVE symbols and per-source hashes (BCH/BTC/ETH/LTC/XRP) | live | `evidence/testnet-verification/20260828T20*Z-*/manifest.json` | PASS |
| AC-TN-005 | Only ACTIVE pool symbols can increase risk | Runtime pool gate + live orders only from ACTIVE symbols | local + live | pool/runtime tests + live traces | PASS |
| AC-TN-006/007 | StrategyKernel and active components affect proposal/trace | Kernel evaluated every closed-bar cycle; component input/output hashes and proposal hashes persisted in DecisionTrace | live | trace store `decision-trace.jsonl` | PASS |
| AC-TN-008 | One sizing authority | Verifier calls canonical `compute_adaptive_sizing`; architecture test forbids deprecated helpers; helpers marked DEPRECATED | local | `tests/architecture/*`, `beidou_core/engine.py` markers | PASS |
| AC-TN-009/010 | Venue leverage and final quantity are bound and ACKed | Live leverage set/readback equality enforced (mismatch blocks); quantity identity numeric + ACK equality | live | live traces (`leverage_request/readback`), `test_testnet_guard.py` | PASS |
| AC-TN-011 | PREPARED is durable before write | Append/fsync store and runtime ordering tests pass | local | `tests/unit/test_decision_trace.py`, runtime tests | PASS |
| AC-TN-012/013 | UNKNOWN uses same-id query and ACK identity checks | Live recovery used same clientOrderId query-before-retry; 11 stale UNKNOWN traces adjudicated against venue facts (all confirmed absent → FAILED) | live | trace store `adjudicated` metadata | PASS |
| AC-TN-014/015 | Position reconciliation and reduce-only close converge | Live fills reconciled against position readback; reduce-only closes executed (8 fill→close chains) | live | trace store CLOSED traces | PASS |
| AC-TN-016 | Trace covers pool through execution truth | Live traces store pool/market/factor/strategy/sizing/rule hashes, ACK, position, reconciliation | live | trace store | PASS |
| AC-TN-017 | Current HEAD CI is green | Local gates: full suite passes, coverage 100% (see 15-final-summary). GitHub Actions runner allocation remains blocked by account billing limits (external) | local + GitHub | local gate logs, GitHub run `33195803547` | PARTIAL (external blocker) |
| AC-TN-018 | README/CLI point to one verifier and distinguish alpha | README/CLI/runbook point to `apps.testnet_verify`; retired probe docstring updated | local | `README.md`, `tools/strategy_live_trade.py`, runbook | PASS |

## Campaign evidence (2026-08-29)

- 100 completed decision episodes (terminal traces), including 8 real fill→close
  chains on the live Binance demo venue (BCHUSDT, LTCUSDT).
- 0 unresolved UNKNOWN traces; 11 stale UNKNOWNs adjudicated against venue
  facts (venue confirmed absent).
- 2 defects found and fixed during the campaign: demo-fapi 24hr ticker lacks
  bid/ask (depth top-of-book fallback) and Decimal quantity trailing-zero
  identity mismatch in the write guard (numeric identity + canonical hash).

## Economic Truth separation

E0–E6 gate machinery implemented (`beidou_research/economic_truth.py`,
fail-closed, prerequisite-chained) and surfaced in every verifier manifest.
All gates remain `NOT_EVALUATED`: no research evidence was supplied, and no
Testnet execution fact is promoted to profitability or alpha certification.

## Decision: CONDITIONAL PASS (Testnet execution)

The bounded Testnet verification chain is real, evidenced, and reproducible.
Full admission remains conditional on GitHub Actions runner availability
(account billing) and an independent human review of this report.
`Testnet READY` / `Completed` / `Alpha VERIFIED` are still NOT claimed.
Mainnet remains PROHIBITED.
