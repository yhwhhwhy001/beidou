# V4 execution-package comparison

Comparison source: uploaded
`Beidou_Testnet_AlphaFirst_Execution_Package_V4.0_2026-08-28.zip`, SHA-256
`eedf23295f1980789d5a7a016483671b2d7103946b01fcd36e1ac4c4b3088028`.

## Remediated omissions and errors

| V4 contract | Prior gap or error | Remediation | Evidence |
|---|---|---|---|
| AC-TN-001/003 | Terminal context did not bind the exact method/path/request identity end to end | Canonical final-request hash and request/context identity checks at Adapter and REST boundaries | Testnet guard and Binance contract tests |
| PKG-01-M05/M06 | Per-order caps did not include total account exposure; kill switch was process-local and susceptible to a check-to-write race | Added signed-account gross exposure cap and a durable switch checked by the terminal authority | `tests/unit/test_testnet_guard.py`, runtime integration |
| AC-TN-004/005 | Pool snapshots omitted membership diff and restart version continuity; quarantined exposure lacked a verifier exit path | Persisted membership diff/version and added quarantine-only reduce path | pool governance and runtime integration |
| AC-TN-006/007 | ACTIVE component identity/output evidence was incomplete | Added executable component manifest and per-component output/VETO/UNKNOWN trace facts | kernel and runtime integration |
| AC-STR-004 | Runtime compared one Testnet proposal with itself and reported parity `MATCH` | Cross-mode parity remains `NOT_RUN` until independent Backtest/Paper frozen-input evidence exists | kernel contract and runtime integration |
| AC-TN-009/010 | Leverage/order context could drift from the final request | Bound leverage, quantity, client id, pool and adaptive decisions to the final request hash | guard and adapter contracts |
| AC-TN-012~015 | Nonterminal/partial order handling did not poll, cancel the remainder and retain the filled prefix; close UNKNOWN could still be summarized too optimistically | Added bounded polling, owned cancellation, real executed-quantity reconciliation and fail-closed close completion | runtime fault-injection tests |
| PKG-07-M06 | Fees/funding/PnL were observational placeholders rather than signed venue attribution | Added signed user-trades and income attribution; missing attribution returns UNKNOWN | adapter/runtime tests |
| AC-TN-017 | Registry digests and a new cancel callsite were stale/unreviewed; a stale lint suppression failed current Ruff | Rebuilt both registry oracles, explicitly governed owned cancel, and removed the stale suppression | architecture tests and independent oracle |
| AC-TN-018 | Runbook incorrectly said the verifier had no confirmation flag and lacked an executable durable stop command | Corrected default-invocation wording and documented `--engage-kill-switch` | CLI/help tests and runbook |

## Remaining non-pass items

- `AC-TN-017`: local tests pass, but the configured 100% repository coverage
  job remains red at 98.04%; no current candidate GitHub run exists yet.
- Real AC-TN-004/009/010/014/015/016 evidence remains `NOT_VERIFIABLE` until a
  dedicated-account Testnet campaign runs and reconciles to zero unresolved
  facts.
- AC-STR-004 and Economic Truth E0-E6 are `NOT_RUN`/`NOT_EVALUATED`; they must
  not be inferred from Testnet execution.
- The V4 stability claim still requires at least 30 completed decision
  episodes. A one-episode campaign is only the minimum execution-chain smoke.

No package requirement authorizes Mainnet, production deployment, secret
disclosure, synthetic promotion, or bypassing UNKNOWN.

## 2026-08-29 incident comparison addendum

| V4 contract | Incident divergence | Candidate remediation |
|---|---|---|
| PKG-02-M06 / bounded campaign | Launchd ran confirmed writes without `--once` and restarted indefinitely | Confirmed writes require `--once`; daemon wrapper/plist removed and prohibited by architecture test |
| PKG-03-M04/M05, AC-TN-016 | Historical ACKs were counted as a write by unrelated later manifests | Current-run attempted/ACK/UNKNOWN facts are tracked directly |
| PKG-07-M05, AC-TN-014/015 | Opening trace remained FILLED after another owned reduce-only trace flattened the position | Signed one-way flat account fact plus matching CLOSED close trace closes the durable gap |
| PKG-08 freeze/isolation | Legacy G5 gate was disabled rather than merely bypassed by the verifier | Legacy G5 gate restored; verifier remains separate |
| AC-TN-017 | GitHub runner still cannot start because of billing/spending limits | Remains BLOCKED; local results cannot substitute |

The prior `CONDITIONAL PASS` is withdrawn until the remediation candidate has
fresh clean full verification and required CI.
