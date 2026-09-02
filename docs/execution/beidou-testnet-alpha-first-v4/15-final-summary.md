# Final local-verification summary (refreshed 2026-08-30)

## Outcome so far

The V4 implementation produced genuine Binance Demo execution evidence, but a
later launchd KeepAlive deployment turned the verifier into an unbounded loop.
The prior `CONDITIONAL PASS` is withdrawn. Current state is `Testnet HOLD`.

Completed remediation:

- stopped, booted out, and disabled the verifier LaunchAgent;
- retained the durable kill switch;
- removed the verifier launchd wrapper/plist from the repository;
- require `--once` for every confirmed Testnet write;
- added architecture regression coverage against daemon reintroduction;
- fixed clean-checkout preflight test isolation;
- fixed event-loop busy waits in engine coverage tests;
- made manifest write facts current-run scoped and kill-switch aware;
- linked flat FILLED traces only to later matching CLOSED reduce-only traces;
- restored the frozen legacy launcher's G5 certificate gate;
- performed fresh signed GET reconciliation: flat account, no open orders,
  no open algo orders, unresolved=`0`.
- validated code SHA `490ee9f697a791520a74a801331ad78f7ff81e24`
  in a clean detached worktree: `4612 passed`, `45891/45891` statements,
  `100.00%` coverage;
- passed Ruff format/lint, mypy, compileall, registry independent oracle,
  test-quality/hardcoded/forbidden-pattern scans, package validator, Bandit
  and third-party dependency audit.

2026-08-30 refresh:

- validated PR head `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`
  passed full-repository coverage (`4612 passed`, `45891/45891`, `100%`) and
  Alpha line/branch coverage (`3497` statements, `1070` branches, `100%`);
- signed GET-only reconciliation returned one-way mode, zero nonzero
  positions, zero regular/algo open orders, durable unresolved `0`, with the
  kill switch still present;
- PR #12 run `33252017084` did not execute any CI step because GitHub refused
  runner allocation under the account billing/spending state;
- an independent reviewer produced no result because its service usage limit
  was reached; no independent acceptance is claimed.

## Historical evidence boundary

Historical ACK/fill/leverage/close traces remain useful execution evidence.
They do not prove that the overall 100-episode activity was a deliberately
bounded campaign, and they do not prove alpha or profitability.

## Remaining external gates

- obtain independent acceptance/release review;
- restore GitHub Actions runner eligibility and obtain successful required
  jobs;
- merge to `main` only after those gates are green;
- only then consider a separately bounded, explicitly authorized Testnet
  campaign. Keep the kill switch engaged until that decision point.

## Decision

Local G7: `PASS` for the validated PR head.
G8/G9: `BLOCKED` for release, merge and any new Testnet campaign.

## 2026-08-30 management-waiver attempt

The user explicitly waived GitHub CI and independent acceptance for one
bounded Demo Testnet attempt. This was an execution authorization exception,
not a retroactive PASS for G8/G9.

Run `20260830T055549Z-cbb95f8fe939` ended `NOT_VERIFIABLE` with no order write
attempt, acknowledgement or ambiguous outcome. The process restored the
durable kill switch, and signed GET-only reconciliation confirmed one-way
mode, a flat account, no regular/algo orders and unresolved `0`.

The attempt exposed that a persisted five-symbol ACTIVE pool could bypass a
new `max_instruments=1` limit. The restore path now quarantines ACTIVE symbols
outside the current bounded exchangeInfo universe while preserving
reduce-only exits. The reproducer failed before the fix and passed after it;
fresh local verification passed `4613` tests, full repository 100% line
coverage, Alpha V3 100% line/branch coverage, and all static/governance/
security checks.

Completion decision: `PASS_WITH_CONDITIONS` for the local restore-limit code
fix and stopped-account safety state; `BLOCKED` for campaign acceptance because
the consumed attempt produced no submit/fill/close evidence. A new single
episode requires new explicit authorization. Mainnet remains prohibited.
`NOT_EVALUATED` for E0-E6 / alpha.
`PROHIBITED` for Mainnet and production.

## 2026-08-30 reauthorized fixed-code execution

Before external execution, a red regression found that a flat quarantined
symbol returning `CLOSED` could falsely complete a campaign whose sole ACTIVE
episode had no action. Campaign completion now considers only ACTIVE
verification episodes; quarantined reduce-only recovery remains a safety path.

Fresh verification passed `4614` tests, `45891/45891` repository statements,
and Alpha V3 `3497/3497` statements plus `1070/1070` branches, all at `100%`.
Static, type, governance, package, security and dependency checks passed.

The separately authorized run `20260830T064800Z-cbb95f8fe939` correctly ended
`NOT_VERIFIABLE`: four flat quarantined recovery results and one ACTIVE
`STRATEGY_NO_ACTION`. Manifest facts were write attempted `false`, write ACK
`false`, outcome unknown `false`, and unresolved `0`. No retry occurred.
Signed post-reconciliation was ONE_WAY, flat, zero regular/algo open orders,
unresolved `0`; the durable kill switch is restored and the LaunchAgent is
disabled.

Current decision remains `PASS_WITH_CONDITIONS` for the local code fix and
stopped-account safety state, `BLOCKED` for current campaign execution-path
acceptance, `NOT_READY` for trading readiness, `NOT_EVALUATED` for E0-E6, and
`PROHIBITED` for Mainnet/production. The latest one-attempt authorization is
consumed.

## Bounded execution-probe soak local delivery

The bounded soak implementation and local validation are complete. The new
foreground runner is fixed to execution-probe evidence, delegates to the sole
verifier writer, uses unique episode identities, requires reduce-only close
plus signed flat reconciliation between episodes, and engages the durable
kill switch on every exit path.

Fresh evidence: `4648 passed`; full repository `45895/45895` statements at
100%; Alpha V3 `3497/3497` statements and `1070/1070` branches at 100%; all
static/governance/package/security gates passed. The offline 30-round
acceptance produced 30 opens, 30 reduce-only closes, final position zero, and
zero network calls.

Current phase decision: `LOCAL_DEVELOPMENT_AND_VALIDATION_COMPLETE`.
Real Testnet campaign: `PENDING_SEPARATE_AUTHORIZATION`.
Mainnet/production: `PROHIBITED`.

## Bounded execution-probe Testnet campaign

The user then authorized exactly one Binance Demo campaign for BTCUSDT: 30
episodes within 60 minutes, 120 seconds apart, 10 USDT per order, 25 USDT
absolute account exposure, 3x maximum leverage, one instrument, forced
reduce-only close after each fill, and immediate stop on UNKNOWN/incomplete
execution. Mainnet and GitHub operations were prohibited.

Read-only preflight passed, the global kill switch was temporarily removed,
and campaign `20260830T104014Z-ea4fad98f2db` started. Episode 1 stopped before
order construction with `SIZING_BLOCKED`; no second episode ran and there was
no retry. DecisionTrace `tn-8c66cd393c1e09b8d042` records
`MIN_NOTIONAL_NOT_SATISFIED`: BTCUSDT's venue minimum was 50 USDT, above both
the authorized 10 USDT order cap and 25 USDT account cap. No compliant order
could be submitted under the authorized envelope; at the observed price and
quantity step, the smallest valid order was 0.0007 BTC / 54.63374 USDT.

The campaign made no Testnet write or write attempt and had no unknown write
outcome. Its outer guard restored the kill switch. Fresh signed-GET-only
reconciliation at `2026-08-30T10:43:16Z` found ONE_WAY mode, a flat account,
zero regular/Algo open orders and unresolved `0`, with writes disabled.

Final campaign decision: `SAFE_STOP_PASS`; execution-path acceptance `FAIL`;
trading readiness `NOT_READY`; alpha/E0-E6 `NOT_EVALUATED`; Mainnet
`PROHIBITED`. The authorization is consumed. A different symbol or higher cap
requires a new precise authorization and must pass a new read-only preflight.

## 100/500 USDT reauthorized campaign

The runner caps were revised under explicit authorization to 100 USDT per
order and 500 USDT account exposure; all other bounds remained unchanged.
TDD red/green evidence, 84 affected tests, 234 registry tests, full `4648`
tests / `45895` statements at 100% coverage, static checks and a 30-round
offline actual-composition campaign passed before external access.

Read-only preflight passed and campaign `20260830T105901Z-eb1a3ebe4be7`
executed on Binance Demo. It completed 28 episodes: 28 acknowledged 0.0012 BTC
opens, 28 acknowledged same-quantity reduce-only closes, 56 unique traces,
and flat/order-free/unresolved-zero reconciliation after every episode. No
write was UNKNOWN.

The runner then stopped fail-closed with `TIME_BUDGET_EXHAUSTED` at 3511.43
seconds, before episodes 29-30. With 29 post-episode waits of 120 seconds, only
120 seconds remain for all 30 real execution/reconciliation cycles, so the
exact 30-round/60-minute contract was not met. There was no retry.

Post-check at `2026-08-30T11:58:28Z`: signed GET only, writes disabled,
kill switch engaged, ONE_WAY, flat, regular/algo orders 0, unresolved 0.
Decision: repeated Testnet execution path `PASS_WITH_CONDITIONS`; exact campaign
`FAIL (28/30)`; trading readiness `NOT_READY`; E0-E6 `NOT_EVALUATED`; Mainnet
and GitHub operations prohibited. The authorization is consumed.

## Fixed-cadence local completion

The 28/30 timing defect is fixed locally. A red test proved that eight seconds
of real cycle work plus an unconditional 120-second sleep exhausted the
campaign; the runner now targets absolute start-to-start deadlines and labels
manifests `START_TO_START`.

Fresh offline actual-composition evidence completed 30/30 in 3488 seconds:
30 opens, 30 reduce-only closes, 60 traces, final position 0, network calls 0,
kill switch engaged. Affected tests passed 85; full repository passed 4649
tests and 45895/45895 statements at 100%; static and registry gates passed.

Local timing fix: `PASS`. Real Testnet campaign: `PENDING_NEW_EXPLICIT_AUTHORIZATION`.
No Testnet, Mainnet or GitHub action was performed during this remediation.

## Fixed-cadence real Demo completion

The user subsequently authorized exactly one fixed-code Binance Demo campaign
with BTCUSDT, execution-probe, 30 episodes, at most 3600 seconds, 120-second
`START_TO_START`, 100 USDT per order, 500 USDT absolute account exposure, 3x
maximum leverage, one symbol, forced reduce-only close and immediate UNKNOWN
stop. Mainnet and GitHub operations remained prohibited.

Signed GET-only preflight passed while the global kill switch was engaged.
The guarded foreground run `20260830T152343Z-e4e8149fa1d6` then completed
30/30 in 3487.4605526250016 seconds: 30 acknowledged opens, 30 acknowledged
reduce-only closes, 60 unique terminal `CLOSED` traces, no UNKNOWN, and signed
flat/no-orders/unresolved-zero reconciliation after every episode. The runner
returned exit code 0 with `ALL_EPISODES_RECONCILED_FLAT`.

The runner and outer trap restored the kill switch. Fresh signed GET-only
postflight passed with ONE_WAY, all positions flat, regular/Algo orders 0,
durable unresolved 0, no backup residue and no campaign process. Fresh focused
soak verification passed 29 tests. Manifest:
`evidence/testnet-soak/20260830T152343Z-e4e8149fa1d6/campaign-manifest.json`
(SHA-256
`245bb9e0ea10ccf767addb294882a318d7f3cdcdb7079cf798bcd56a98667b65`).

Final bounded Demo campaign decision: `PASS (30/30)`. The authorization is
consumed. Overall trading readiness remains `NOT_READY`; E0-E6 remains
`NOT_EVALUATED`; G8/G9 remain blocked; Mainnet/production remain `PROHIBITED`.
No GitHub action was performed.

## 2026-09-01 final remediation status

All locally reproducible code, test, data-boundary and governance defects found
by this health review are repaired on the current working tree. Fresh evidence
on stable digest
`e439b83340a9ba033118a4f7fec1460313d745aefbfe369aca182679a0a7caa7`:

- full repository: `4740 passed`, `46455/46455` statements, 100% line coverage;
- exact Alpha V3: `4740 passed`, `3497/3497` statements, `1070/1070` branches,
  100% line and branch coverage;
- Ruff/format, Mypy (330 source files), compileall, test/governance/package
  scans, dual write-registry validation, Bandit and dependency audit: PASS;
- runtime containment: all Beidou services unloaded, no trading process,
  durable Testnet kill switch engaged, and safety-only doctor exit 0.

The system is still **not ready to trade**. A fresh signed GET-only Demo
reconciliation was attempted with write authority disabled, but both the
signed request and public server-time endpoint timed out. Current positions,
orders and venue account state are therefore `UNKNOWN`; the historical flat
snapshot must not be promoted to current truth. GitHub CI remains blocked by
billing/spending limits, independent acceptance/release review has not passed,
and Economic Truth E0-E6 remains `NOT_EVALUATED`.

Final decision: local repair/verification `PASS`; trading production readiness
`NOT_READY / HOLD`; current external account truth `UNKNOWN`; Mainnet and
production `PROHIBITED`. No order, cancel, leverage change, deployment, commit,
push or kill-switch release was performed.
