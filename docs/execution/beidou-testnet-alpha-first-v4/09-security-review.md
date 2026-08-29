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
| Critical | Unbounded risk-increasing write | KeepAlive incident plus guard/config tests | Confirmed writes require `--once`; verifier daemon launchers removed/prohibited; dedicated account caps and durable kill switch remain terminal controls | FIXED IN CANDIDATE / REVALIDATION REQUIRED |
| High | Duplicate exposure after lost POST response | Runtime fault-injection tests | Durable identity and query-before-retry with the same client id; transport UNKNOWN never retries blindly | PASS locally |
| High | False execution truth from malformed ACK or position query | Adapter/runtime contract tests | Identity-bound ACK validation and UNKNOWN fail-closed reconciliation | PASS locally |
| High | Secret leakage into traces/logs/evidence | Redaction code and redacted manifest fields | Secrets are not written to DecisionTrace; tests and docs use placeholders only | PASS locally; live log audit pending |
| High | Account risk after unbounded loop | Fresh signed account, position, open-order and algo-order GETs | LaunchAgent disabled, kill switch retained, flat/no-order readback, durable trace recovery to unresolved=0 | PASS for current account risk |
| High | False current-run write claim | Historical ACKs made a no-write manifest report `real_testnet_write=true` | Current-runtime attempt/ACK/UNKNOWN tracking and kill-switch-aware authority fields | FIXED |
| High | Legacy governance weakening | Default legacy preflight no longer required G5 | Restore historical G5 gate; V4 verifier remains isolated | FIXED |
| Medium | Local JSONL/evidence files are filesystem-local | Local path configuration and `0600` trace creation | Restrict paths/permissions and retain artifacts only in the intended workspace | CONDITIONAL |

Fresh tool evidence: Bandit reported `0` issues in an isolated venv. A clean
wheel environment reported `No known vulnerabilities found` for its installed
runtime dependencies. The populated development host's `pip-audit` instead
reported 26 vulnerabilities across unrelated installed packages (including
old `pytest`, `gitpython`, and `aiohttp`), so the repository CI dependency
audit is not claimed green from this host.

No API key or secret value was printed. Existing `.env` variables were loaded
only inside signed GET processes for account reconciliation; output was
restricted to permissions, positions, orders, and trace identities. Binance
HMAC is exchange authentication, not an internal approval bypass.

## Decision

`BLOCKED` for release/new campaign until clean full verification, independent
review, and GitHub CI pass. Testnet account risk is currently flat and
reconciled; the kill switch remains engaged. Mainnet remains prohibited.
