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
