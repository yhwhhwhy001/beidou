# Acceptance report (incident-remediation candidate)

Candidate branch: `codex/v4-incident-remediation`
Validated code SHA: `490ee9f697a791520a74a801331ad78f7ff81e24`
Validated PR head: `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`
Mainnet: `PROHIBITED`
Overall decision: `BLOCKED`

| Requirement | Current evidence | Result |
|---|---|---|
| AC-TN-001 | Mainnet/HTTP/credential-in-URL/host-confusion negatives remain covered by guard contracts. | PASS locally |
| AC-TN-002 | `apps.testnet_verify` has no G5/Ed25519 dependency. The frozen legacy launcher retains its historical G5 gate. | PASS locally |
| AC-TN-003 | Signed REST exact-query HMAC contract remains covered. | PASS locally |
| AC-TN-004/005 | Historical demo pool evidence and local pool/runtime contracts exist; risk increase remains ACTIVE-only. | PASS_WITH_INCIDENT_CAVEAT |
| AC-TN-006/007 | Historical traces contain kernel/component outputs; integration contracts cover VETO/NO_ACTION/output identity. | PASS_WITH_INCIDENT_CAVEAT |
| AC-TN-008 | Canonical adaptive sizing remains the verifier's sole sizing authority; architecture test forbids deprecated helpers. | PASS locally |
| AC-TN-009/010 | Historical leverage/quantity ACK facts exist and final-request identity is tested. No new write campaign was run after remediation. | PASS_WITH_INCIDENT_CAVEAT |
| AC-TN-011 | PREPARED-before-write remains append/fsync backed and tested. | PASS locally |
| AC-TN-012/013 | Same-client-id query-before-retry and ACK identity contracts remain tested. | PASS locally |
| AC-TN-014/015 | 2026-08-30 signed GET showed one-way mode, no nonzero positions, no regular/algo open orders, and durable unresolved=`0`. Startup recovery links the sole FILLED trace only to a later quantity-matched CLOSED reduce-only trace. | PASS for current account risk |
| AC-TN-016 | Historical trace chain exists; manifest current-run write semantics and restart close linkage were corrected in this candidate. | PASS_WITH_INCIDENT_CAVEAT |
| AC-TN-017 | Validated PR head passed clean detached full coverage (`4612 passed`, `45891/45891`) and Alpha line/branch coverage (`3497` statements + `1070` branches), both `100%`; all static/governance/security checks passed. PR #12 Actions run `33252017084` failed before any step because GitHub did not allocate runners under the account billing/spending state. | BLOCKED |
| AC-TN-018 | README, CLI, and runbook point to the verifier; confirmed writes require `--once`; daemon/KeepAlive verifier launchers are prohibited by architecture test. | PASS locally |

## Incident findings and remediation

1. `db2debcf` installed `apps.testnet_verify` with launchd `KeepAlive=true`
   and omitted `--once`, producing repeated episodes beyond the authorized
   bounded campaign. The job was booted out and disabled; the durable kill
   switch remains engaged.
2. Confirmed Testnet writes now fail configuration validation without
   `--once`. The launchd wrapper/plist were removed and a repository
   architecture test prevents their return.
3. Manifests previously derived `real_testnet_write` from every historical
   trace in the shared store. Current-run write attempted/ACK/UNKNOWN facts are
   now tracked directly, and an engaged kill switch reports write authority as
   disabled.
4. A later reduce-only close could leave the original opening trace at
   `FILLED`. Startup now closes that durable gap only when signed account facts
   show one-way flat position and a later direction/quantity-matched CLOSED
   reduce-only trace exists.
5. The legacy launcher G5 gate was unintentionally disabled. It is restored;
   only the separate V4 verifier bypasses frozen certification.

## Economic Truth separation

E0-E6 machinery remains `NOT_EVALUATED`. Historical Testnet execution facts
do not establish profitability, OOS robustness, or `ALPHA VERIFIED`.

## Independent acceptance attempt

A no-context reviewer was assigned the uploaded package, incident baseline,
and exact PR-head diff. The reviewer service reached its usage limit before
returning any finding or decision. This is missing evidence, not a pass and
not a review failure. G8 remains blocked until a genuinely independent review
is completed.

## Admission decision

The dedicated Testnet account is currently reconciled to zero risk and local
G7 is green for the exact validated PR head. This candidate is not admitted
until independent acceptance and required GitHub CI are current and green.
The user's requested Testnet campaign is recorded but is not executable while
the kill switch is engaged or any gate is unknown/blocked.
