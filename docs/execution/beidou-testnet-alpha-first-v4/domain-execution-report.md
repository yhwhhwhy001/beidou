# Exchange execution report — 2026-08-30

Current decision: `PASS` for the exact bounded Demo execution path; safety
state reconciled. Overall trading readiness remains `NOT_READY`.

## Authority and bounds

The user explicitly waived GitHub CI and independent acceptance for one local
Binance Demo Testnet attempt. The authorized command retained `--once`,
`--close-after-verify`, 25 USDT maximum notional, 3x maximum leverage and one
configured instrument. Mainnet, daemon execution, GitHub writes and a second
automatic attempt were not authorized.

## Result

- Run: `20260830T055549Z-cbb95f8fe939`.
- Runtime decision: `NOT_VERIFIABLE`, exit code `2`.
- Episodes: four `MARKET_OBSERVATION_UNKNOWN`; one `STRATEGY_NO_ACTION`.
- Exchange writes: attempted `false`; acknowledged `false`; outcome unknown
  `false`; `real_testnet_write=false`.
- Durable unresolved trace count: `0`.
- Manifest SHA-256:
  `e6cc3af3c9dd45f1bdd780fca6d0dd412ea3a59032fe190380deb43877972228`.

The attempt therefore did not validate submit/fill/close behavior. It did
validate fail-closed no-action behavior and exposed a restored-pool limit
defect before any terminal order request.

## Rollback and reconciliation

The shell trap restored `.beidou/testnet_verification/KILL_SWITCH` on process
exit. Fresh signed GET-only reads then returned Demo Testnet, one-way mode,
zero nonzero positions, zero regular orders, zero Algo orders and durable
unresolved `0`. The verifier LaunchAgent remains disabled and no verifier
process remains.

## Remediation

Startup now quarantines restored ACTIVE symbols outside the current bounded
exchangeInfo universe. The regression failed before the fix and passed after
it. The full local suite passed with `4613` tests and 100% repository
coverage; Alpha V3 line/branch coverage also remained 100%.

Another external attempt requires a new explicit authorization. This report
does not imply profitability, production readiness or Mainnet authority.

## Reauthorized follow-up episode

- Run: `20260830T064800Z-cbb95f8fe939`.
- Bounds: Demo host, `--once`, `--close-after-verify`, 25 USDT maximum
  notional, 3x maximum leverage and one ACTIVE instrument.
- Runtime decision: `NOT_VERIFIABLE`, exit code `2`.
- Episodes: four `QUARANTINED_ALREADY_FLAT`; one ACTIVE
  `STRATEGY_NO_ACTION`.
- Exchange writes: attempted `false`; acknowledged `false`; outcome unknown
  `false`; `real_testnet_write=false`.
- Durable unresolved trace count: `0`.
- Manifest SHA-256:
  `7084207764f0699f5b90437c71675d9ddd22f3168239000695a153f288b565d5`.

The fixed aggregation correctly refused to call this campaign completed.
There was no automatic retry. The kill switch was restored; signed GET-only
post-reconciliation returned Demo Testnet, ONE_WAY, flat, zero regular/algo
open orders and durable unresolved `0`. Execution-path acceptance remains
`BLOCKED` because this run produced no submit/fill/close evidence.

## Authorized bounded soak campaign

- Campaign: `20260830T104014Z-ea4fad98f2db`.
- Authorized bounds: Binance Demo Testnet, `BTCUSDT`, execution-probe, 30
  episodes, at most 60 minutes, 120-second interval, 10 USDT target and
  per-order cap, 25 USDT absolute account-exposure cap, 3x maximum leverage,
  one instrument, mandatory reduce-only close after each fill, and immediate
  stop on UNKNOWN or any incomplete episode.
- Runtime result: episode 1 returned `SIZING_BLOCKED`; the campaign stopped in
  3.21 seconds before episode 2. Exit code was `2`.
- Exchange writes: attempted `false`; acknowledged `false`; outcome unknown
  `false`; `real_testnet_write=false`.
- Root cause: DecisionTrace `tn-8c66cd393c1e09b8d042` records exchange
  `min_notional=50`, `min_qty=0.0001`, `step_size=0.0001`, observed price
  `78048.2`, raw quantity `0.0001281259529367749672638190247`, final quantity
  `0`, and reason vector `MIN_NOTIONAL_NOT_SATISFIED`. The authorized 10 USDT
  order ceiling and 25 USDT account ceiling are both below the 50 USDT venue
  minimum. At the observed price and 0.0001 quantity step, the smallest
  venue-compliant quantity was 0.0007 BTC (54.63374 USDT), so no compliant
  BTCUSDT opening order existed.
- Evidence:
  `evidence/testnet-soak/20260830T104014Z-ea4fad98f2db/campaign-manifest.json`,
  `evidence/testnet-soak/episodes/20260830T104017Z-6074e017b9da/manifest.json`,
  and `.beidou/testnet_soak/decision-trace.jsonl`.

There was no retry. The outer safety guard restored
`.beidou/testnet_verification/KILL_SWITCH`. A fresh signed-GET-only
reconciliation at `2026-08-30T10:43:16Z`, with writes disabled, returned the
Demo host, ONE_WAY mode, no nonzero position, zero regular orders, zero Algo
orders, and durable unresolved `0`. No campaign process remains.

Execution safety decision: `PASS` for bounded fail-closed stopping and the
stopped-account safety state; `FAIL` for submit/fill/reduce-only-close path
acceptance because no order request was created. The campaign authorization is
consumed. Any parameter change or new attempt requires separate explicit
authorization. Mainnet and GitHub operations remain prohibited.

## 100/500 USDT bounded campaign

Campaign `20260830T105901Z-eb1a3ebe4be7` used BTCUSDT, execution-probe,
30 requested episodes, 3600 seconds, 120-second post-episode interval,
100 USDT per-order cap, 500 USDT account-exposure cap, 3x maximum leverage,
one symbol and mandatory reduce-only close.

It completed 28 episodes in 3511.43 seconds. Each episode produced an
acknowledged 0.0012 BTC fill, an acknowledged same-quantity reduce-only close,
position-after 0, signed ONE_WAY/flat/no-orders reconciliation, and unresolved
0. There were 56 unique order-linked traces. `real_testnet_write=true`, write
attempted `true`, and write outcome unknown `false`.

The campaign stopped before episode 29 with `TIME_BUDGET_EXHAUSTED`; it did not
fail on execution or reconciliation. The 30/60/120 contract is not feasible
under post-episode sleep semantics unless all 30 real order and reconciliation
cycles together take no more than 120 seconds. No automatic retry occurred.

Fresh signed-GET-only post-reconciliation at `2026-08-30T11:58:28Z` returned
ONE_WAY, flat, zero regular/algo orders and unresolved 0, with writes disabled
and the kill switch restored. Execution-path decision: `PASS` for repeated
submit/fill/reduce-only-close/reconcile behavior; exact campaign acceptance:
`FAIL` because only 28/30 episodes completed. Manifest:
`evidence/testnet-soak/20260830T105901Z-eb1a3ebe4be7/campaign-manifest.json`.

## Local timing remediation

The 28/30 stop was reproduced with a deterministic 8-second episode and fixed
locally. The runner now uses a 120-second start-to-start schedule, sleeping
112 seconds after an 8-second execution/reconciliation cycle. A full offline
actual-composition run completed 30/30 in 3488 seconds with 30 opens,
30 reduce-only closes, 60 traces, final position 0 and no network calls.

Local execution-timing decision: `PASS`. This does not authorize another
external campaign; the prior authorization remains consumed and the global
kill switch remains engaged.

## Fixed-cadence 30/30 Demo campaign

Under a new, exact authorization, source bundle
`983f298ab9ed2fec8fdad23031104def95eba51ce5b68dceab0c6751c66cac44`
ran one foreground Binance Demo campaign with BTCUSDT, execution-probe, 30
episodes, a 3600-second budget, 120-second `START_TO_START` cadence, 100 USDT
per-order cap, 500 USDT absolute account-exposure cap, 3x maximum leverage,
one symbol, mandatory reduce-only close, and immediate stop on UNKNOWN.

Read-only preflight passed while the kill switch remained engaged: exact Demo
host, BTCUSDT `TRADING`, venue minimum notional 50 USDT, current smallest
effective order approximately 55.08 USDT and within the cap, ONE_WAY, flat,
regular/Algo open orders 0, leverage readback 2x and within the 3x cap, and
durable unresolved 0. The outer trap then temporarily moved the switch and
restored it on every exit path.

Campaign `20260830T152343Z-e4e8149fa1d6` completed 30/30 in
3487.4605526250016 seconds with `ALL_EPISODES_RECONCILED_FLAT`. It produced 30
acknowledged opens, 30 acknowledged reduce-only closes and 60 unique durable
traces. All 60 traces are terminal `CLOSED`; every episode ended ONE_WAY,
flat, with regular/Algo open orders 0 and unresolved 0. The maximum observed
opening `cumQuote` was 94.881720 USDT; all leverage readbacks were 2x. Real
Testnet writes were attempted and acknowledged, and no write outcome was
UNKNOWN.

The episode `run_id` is generated after each episode completes, so its
118-124 second completion-time deltas are only a cadence proxy, not exact
start timestamps. The hash-bound manifest declares `START_TO_START`; the
runtime uses absolute deadlines; the fresh fake-clock regression passed; and
the complete real campaign remained within its 3600-second budget. This
measurement limitation is retained rather than misreporting completion
timestamps as start-time evidence.

Fresh signed GET-only post-reconciliation passed: exact Demo environment,
ONE_WAY, all positions flat, regular/Algo open orders 0, durable unresolved 0,
kill switch engaged, backup absent and no campaign process. The campaign
manifest SHA-256 is
`245bb9e0ea10ccf767addb294882a318d7f3cdcdb7079cf798bcd56a98667b65`:
`evidence/testnet-soak/20260830T152343Z-e4e8149fa1d6/campaign-manifest.json`.

Execution-path and exact bounded-campaign decision: `PASS`. The one campaign
authorization is consumed. This is execution-probe evidence only; it is not
Alpha, profitability, E0-E6, Mainnet or production-readiness evidence.

## 2026-09-01 execution-path revalidation

Fresh local execution, recovery, UNKNOWN, idempotency, cancellation,
partial-fill, close and reconciliation tests are included in the `4740/4740`
full-repository pass with 100% line coverage. The guarded launch facade's five
previously uncovered optional forwarding paths are now directly tested without
constructing a real runtime. Primary and independent write-capability registry
validation both returned PASS.

No current external reconciliation claim is possible. The bounded signed
GET-only Demo check had credentials present and write authority disabled, but
the first signed request timed out; the public Demo server-time endpoint also
timed out. Current venue positions/orders are `UNKNOWN`. The durable kill
switch remains engaged, all Beidou services are unloaded, and no trading
process or exchange write occurred. Execution code integrity is `PASS`;
current account reconciliation is `BLOCKED/UNKNOWN`.
