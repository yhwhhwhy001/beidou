# Context

- Goal: complete all remaining repairs, reach genuine repository-wide 100% line coverage, and validate a real Binance USD-M Testnet order lifecycle.
- Repository: `/Users/maguannan/beidou`, `main` at `68e8937f520c5959c591554d72bd7f065d3dc7ab` when this run started.
- Authority: local code/tests/docs and real Testnet order lifecycle are authorized. Git commit/push for this scoped repair batch was separately authorized on 2026-08-12. Mainnet, real funds, transfers, credential/permission changes, deployment and restart are not authorized.
- Safety invariant: no Testnet write while ownership, existing exposure, protection or resulting state is UNKNOWN.

## Fresh external preflight

- Endpoint allowlist resolved to `demo-fapi.binance.com`; BTCUSDT is a TRADING perpetual contract.
- Account readback: ONE_WAY mode, `canTrade=true`, `canWithdraw=true`.
- Existing state: six non-zero positions, zero ordinary open orders and zero open Algo orders.
- Decision: order write is temporarily stopped because existing position ownership/protection is not proven. No order was submitted, cancelled or modified.
