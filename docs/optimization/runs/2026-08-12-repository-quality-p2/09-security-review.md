# Security review

## Assets and trust boundaries

- Exchange API key/secret, Testnet account identity, positions and permissions.
- Local configuration/environment to outbound Binance Testnet HTTPS boundary.
- Reports must contain only endpoint host and redacted summaries.

## Authorization and data handling

| Severity | Risk | Evidence | Mitigation | Status |
|---|---|---|---|---|
| Critical | Mainnet mistaken for Testnet | effective configuration resolved to `https://demo-fapi.binance.com` | strict allowlist accepted only Binance Futures Testnet hosts before every request | PASS |
| Critical | Credential or signature disclosure | output contains presence and schema facts only | values, signatures, balances, positions and identifiers were never printed or persisted | PASS |
| High | Read validation mutates account | invoked client methods map to GET server time, exchange info, ticker, depth, account, open orders and position mode | no POST, PUT or DELETE method invoked; no order intent created | PASS |
| High | Configuration capability mistaken for operator authority | Testnet configuration reports write capability | retained explicit authority boundary: exchange writes, restart, deployment, Mainnet and real funds remain unauthorized | PASS |
| Critical | Development/certification bypass reaches writable Testnet | source review found DEV_BYPASS and auto-generated G5 evidence routes | bypass now rejects Testnet; preflight never manufactures PASS evidence | PASS |
| High | Cleanup utility cancels orders by default | historical utility used direct network transport and destructive default behavior | default is read-only inventory; cancellation requires two explicit flags and unified client | PASS |
| High | SBOM helper executes a supplied interpreter path | arbitrary interpreter execution expanded command-injection boundary | reads installed distribution metadata in-process | PASS |
| High | Recovery state is partially written or silently lost | recovery success depended on non-atomic JSON state | temp write, flush/fsync, atomic replace and orchestration failure on persistence error | PASS |

## Decision

PASS for the bounded GET-only Testnet validation. This is not approval for order submission, cancellation, leverage/margin changes, restart, deployment, Mainnet or real funds.
