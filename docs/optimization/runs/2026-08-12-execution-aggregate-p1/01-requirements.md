# Requirements

| ID | Requirement | Acceptance criterion |
|---|---|---|
| P1-EXEC-001 | Every planned child order is durable before the first venue write | Parent and all children can be restored after process restart with immutable economic fields and deterministic client IDs |
| P1-EXEC-002 | Parent ACK reflects the complete submit plan | Parent is ACKED only after every child has a venue ACK; any ambiguous/missing child keeps parent UNKNOWN |
| P1-EXEC-003 | Child transitions are monotonic and idempotent | Terminal states cannot regress; duplicate events do not change totals; UNKNOWN requires query-by-client-id |
| P1-EXEC-004 | Orders execute target delta, not target size | `delta = target signed quantity - current signed quantity - acknowledged/pending signed remainder` using exact decimal arithmetic |
| P1-EXEC-005 | Partial fills and restart replay preserve aggregate exposure | Replaying the same persisted child/fill facts produces the same remaining quantity and parent status |
| P1-EXEC-006 | PostgreSQL schema supports the same command contract | Forward-only migration and adapter contract tests cover parent/child commands, events, fencing and replay queries |
| P1-EXEC-007 | User-stream events update child/parent facts without inventing fills | ACK, partial fill, fill, cancel, reject and gap/unknown are mapped conservatively and duplicate event IDs are idempotent |
| P1-EXEC-008 | Quality gates cover the touched execution boundary | Focused tests, full regression, type/critical lint, migration checks and diff review have fresh evidence |

## Non-goals

- No real venue write, Testnet restart, deployment or Mainnet validation.
- No strategy profitability tuning.
- No deletion or rewrite of historic order/fill evidence.
- Repository-wide 85% coverage is a release gate, not permission to weaken or exclude low-coverage safety code.
