# Risk review

| Risk | Control | Residual status |
|---|---|---|
| Process dies after venue write | Persist child first; `SENDING -> UNKNOWN` on restart; query by deterministic client ID | Controlled in code; real venue recovery NOT_VERIFIABLE |
| Parent ACK hides unsent slices | Parent ACK derives from the complete child aggregate only | PASS in controlled tests |
| Partial fill is double counted | Cumulative fill must be monotonic and duplicate event IDs are idempotent | PASS in controlled tests |
| New signal resubmits existing exposure | Target delta subtracts durable in-flight signed remainder, including UNKNOWN | PASS in controlled tests |
| Reversal submits only target magnitude | Signed target-current-inflight equation | PASS in controlled tests |
| External/protection event corrupts aggregate | Unknown client ID is not projected into an aggregate | PASS in controlled tests |
| Testnet weakens research/lifecycle gates | `DEV_BYPASS` and development universe fast promotion are limited to research/Paper | PASS in architecture tests |
| PostgreSQL schema/runtime differs from fake adapter | Forward migration and SQL contract tests exist | Real PostgreSQL crash/double-worker run NOT_VERIFIABLE |
| Stale worker persists or advances a child | Plan requires the current parent lease/token; send transitions require the same live fence; recovery writes the new fence | PASS in SQL contract tests; real double-worker run NOT_VERIFIABLE |
| Venue normalizes quantity/price after plan persistence | Venue-exact equality is required at the final adapter boundary; no silent economic mutation | PASS in architecture and full regression tests |
| Venue returns a terminal/unknown status with an order ID | REJECTED/CANCELED/EXPIRED are projected explicitly; unknown or invalid fill becomes UNKNOWN | PASS in engine behavior tests |

## Fail-closed decision

- Local P1 implementation: eligible for commit after final controlled verification.
- Testnet operation: HOLD until separately authorized migration, restart, WebSocket replay and
  identity-bound recovery evidence exist.
- Mainnet/real funds: PROHIBITED by current authority and missing release evidence.
