# Exchange execution validation report

- Explicit authorization and environment: real Binance USD-M Testnet order lifecycle is authorized; Mainnet and real funds remain prohibited.
- Symbols/cases/exposure bounds/stop conditions: intended BTCUSDT minimum-rule lifecycle; stop on non-Testnet host, UNKNOWN ownership, pre-existing unprotected exposure, timeout without client-ID recovery or reconciliation mismatch.
- Client intent and idempotency: no client intent created yet because the pre-write safety gate failed.
- Durable journal/exchange/position reconciliation: live exchange readback found six non-zero positions, zero ordinary open orders and zero open Algo orders. Ownership and protection could not be proven.
- Partial fill/retry/timeout/rate/clock/precision: Testnet time and BTCUSDT rule schemas passed; write behavior not yet exercised.
- Leverage/margin/mode exchange readback: ONE_WAY mode read back; no leverage or margin mutation performed.
- Protection and recovery: no observed ordinary or Algo protection orders for the account-level existing exposure set.
- UNKNOWN/ownership status: existing positions are UNKNOWN to this validation run. They are not assumed to belong to this system or safe to modify.
- Decision: `BLOCKED_PRE_WRITE` for the real order lifecycle; no exchange mutation occurred. Continue code/coverage work and retry only after a safe attributable starting state is proven.

## Fresh recheck — 2026-08-12

- Testnet host remained `demo-fapi.binance.com`; account remained trade-enabled and in ONE_WAY mode.
- Six non-zero positions remained. BTCUSDT itself was flat, with zero ordinary orders and zero Algo orders account-wide.
- Account-level unknown exposure still prevents a new risk-bearing lifecycle because shared margin and ownership/protection cannot be attributed to this validation run.
- No order, cancellation, position, leverage, margin or protection mutation was performed.

## Historical permission-gate state — 2026-08-12

The following records the pre-correction behavior and is superseded by the explicit Testnet-plan correction in the final section below.

- The Testnet account reports `canWithdraw=true`. The previous implementation incorrectly exempted Testnet from the withdrawal-permission hard stop.
- The exemption has been removed: withdrawal permission now blocks writable execution in every environment, including Testnet.
- The lifecycle remains `BLOCKED_PRE_WRITE` on two independent grounds: withdrawal permission is enabled, and six existing positions remain unattributable/unprotected.
- No permission change was attempted because transfers/permission mutation is outside the current authorization.

## Authorized final Testnet lifecycle — 2026-08-22

- The later explicit user authorization permitted a governed Testnet restart and one bounded order lifecycle; Mainnet, real funds, transfers and permission mutation remained out of scope.
- The G5 S2 implementation now honors the execution package's explicit `allow_withdraw_permission: true` demo-fapi exemption. Unknown permission facts and non-exempt withdrawal permission remain fail-closed.
- The final pushed commit was `154b53a808b2ed8744d01c130eef6c5687ad1a09`. The launchd service was restarted with `launchctl kickstart -k`; `/health` returned HTTP 200/`HEALTHY`, and the running supervisor reported that commit.
- The official runner executed the bounded `BTCUSDT` `create_query_cancel` Testnet scenario with `--max-notional 100`. Create, query, cancel and terminal query all passed. A subsequent read-only check found zero positions and zero open orders.
- This is a single-scenario order-flow result, not full G5 certification: the certificate remains `NOT_VERIFIABLE` until all 16 plan scenarios are run and independently verified. G7 remains `HARD_HOLD` because its durable 30-day/200-cycle evidence is unavailable.
- After the service resumed monitoring, its durable local state diverged from the live Testnet account (15 stale non-zero positions and a balance mismatch). The service correctly entered `DEGRADED`/`NO_NEW_RISK`; no attempt was made to erase history or bypass reconciliation.
