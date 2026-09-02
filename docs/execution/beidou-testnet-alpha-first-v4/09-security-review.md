# Security review

## Assets and trust boundaries

- Assets: Binance API key/secret, dedicated Testnet account, risk-increasing
  order capability, durable decision trace, and local evidence files.
- Trust boundaries: CLI/environment to config, verifier to Testnet guard,
  verifier to Adapter, Adapter to REST/HMAC transport, and venue responses to
  local state/reconciliation.
- Mainnet and production modules are outside the verifier trust graph and are
  not deleted.

## Authorization and data handling

| Severity | Risk | Evidence | Mitigation | Status |
|---|---|---|---|---|
| Critical | Mainnet, HTTP, credential-in-URL, or host-confusion write | Guard allowlist/negative tests | Exact HTTPS host normalization and hard Mainnet deny | PASS locally |
| Critical | Unbounded risk-increasing write | KeepAlive incident plus guard/config tests | Confirmed writes require `--once`; verifier daemon launchers removed/prohibited; dedicated account caps and durable kill switch remain terminal controls | FIXED / LOCALLY VERIFIED |
| High | Duplicate exposure after lost POST response | Runtime fault-injection tests | Durable identity and query-before-retry with the same client id; transport UNKNOWN never retries blindly | PASS locally |
| High | False execution truth from malformed ACK or position query | Adapter/runtime contract tests | Identity-bound ACK validation and UNKNOWN fail-closed reconciliation | PASS locally |
| High | Secret leakage into traces/logs/evidence | Redaction code and redacted manifest fields | Secrets are not written to DecisionTrace; tests and docs use placeholders only | PASS locally; live log audit pending |
| High | Account risk after unbounded loop | Fresh signed account, position, open-order and algo-order GETs | LaunchAgent disabled, kill switch retained, flat/no-order readback, durable trace recovery to unresolved=0 | PASS for current account risk |
| High | False current-run write claim | Historical ACKs made a no-write manifest report `real_testnet_write=true` | Current-runtime attempt/ACK/UNKNOWN tracking and kill-switch-aware authority fields | FIXED |
| High | Legacy governance weakening | Default legacy preflight no longer required G5 | Restore historical G5 gate; V4 verifier remains isolated | FIXED |
| Medium | Local JSONL/evidence files are filesystem-local | Local path configuration and `0600` trace creation | Restrict paths/permissions and retain artifacts only in the intended workspace | CONDITIONAL |

Clean detached PR-head evidence at
`a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`: Bandit passed with no findings.
The dependency audit reported no known vulnerabilities in auditable
third-party dependencies; the local editable `beidou` distribution cannot be
resolved from PyPI and is explicitly not claimed audited. Required GitHub CI
is still absent.

No API key or secret value was printed. Existing `.env` variables were loaded
only inside signed GET processes for account reconciliation; output was
restricted to aggregate status and counts. The 2026-08-30 refresh returned
one-way mode, no nonzero positions, no regular/algo open orders, unresolved
`0`, and confirmed the kill switch remained present. Binance HMAC is exchange
authentication, not an internal approval bypass.

## Decision

Local security verification: `PASS_WITH_CONDITIONS`. Release/new campaign is
`BLOCKED` until independent review and required GitHub CI pass. Testnet account
risk is currently flat and reconciled; the kill switch remains engaged.
Mainnet remains prohibited.

## Execution-probe soak security delta

| Severity | Risk | Control | Local decision |
|---|---|---|---|
| Critical | Campaign escapes requested size/time bounds | immutable validation plus current authorized 100 USDT order cap, 500 USDT account cap, 3x, one symbol, 30 episodes, 3600 seconds | PASS |
| Critical | UNKNOWN is followed by another risk increase | all sub-facts must be CLOSED; immediate durable kill switch then read-only reconciliation | PASS |
| High | Same closed bar aliases a prior order | bounded namespace included in stable trace/intent/client-order identity | PASS |
| High | Probe fills are promoted to Alpha evidence | dedicated kernel/entrypoint/manifest labels set `EXECUTION_PROBE` and `alpha_evidence=false` | PASS |
| High | Separate soak writer bypasses verifier controls | source/AST boundary tests prohibit REST client, Binance adapter and direct `create_order` in soak package | PASS |
| High | Residual risk carries into next episode | signed one-way/flat/no-orders/unresolved-zero reconciliation is mandatory before sleep/continue | PASS |
| Medium | Process cancellation leaves authority open | durable kill switch is fsync/replace engaged on every runner exit path | PASS locally |

Bandit and dependency audit passed after the delta. Real Testnet credentials
were not used during local acceptance. Decision: `PASS` for local development
and `PENDING_EXPLICIT_AUTHORIZATION` for a real Testnet campaign.

The later 100/500 USDT authorization was consumed by campaign
`20260830T105901Z-eb1a3ebe4be7`. The size, leverage, symbol, UNKNOWN-stop,
forced-close and kill-switch controls held for 28 real episodes; the campaign
then stopped on the 3600-second budget before episodes 29-30. Fresh signed
post-reconciliation was flat/order-free/unresolved-zero. Security decision:
`PASS` for bounded containment; exact 30-round acceptance remains failed.
