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
| Critical | Unbounded risk-increasing write | Guard cap/kill-switch tests | Explicit confirmation, dedicated account, notional/leverage caps, kill switch | PASS locally |
| High | Duplicate exposure after lost POST response | Runtime fault-injection tests | Durable identity and query-before-retry with the same client id; transport UNKNOWN never retries blindly | PASS locally |
| High | False execution truth from malformed ACK or position query | Adapter/runtime contract tests | Identity-bound ACK validation and UNKNOWN fail-closed reconciliation | PASS locally |
| High | Secret leakage into traces/logs/evidence | Redaction code and redacted manifest fields | Secrets are not written to DecisionTrace; tests and docs use placeholders only | PASS locally; live log audit pending |
| High | Real venue behavior and account custody unverified | No live campaign in this turn | Dedicated Testnet account and explicit operator confirmation required before E2E | OPEN / BLOCKING |
| Medium | Local JSONL/evidence files are filesystem-local | Local path configuration and `0600` trace creation | Restrict paths/permissions and retain artifacts only in the intended workspace | CONDITIONAL |

Fresh tool evidence: Bandit reported `0` issues in an isolated venv. A clean
wheel environment reported `No known vulnerabilities found` for its installed
runtime dependencies. The populated development host's `pip-audit` instead
reported 26 vulnerabilities across unrelated installed packages (including
old `pytest`, `gitpython`, and `aiohttp`), so the repository CI dependency
audit is not claimed green from this host.

No API key or secret value was read or printed in this review. The HMAC
secret is used only by the transport boundary. Binance HMAC is exchange
authentication, not an internal approval bypass.

## Decision

`PASS_WITH_CONDITIONS` for local code/security controls. Real Testnet custody,
network, dependency, and operational evidence are absent, so Testnet admission
remains `HOLD` and Mainnet remains prohibited.
