# P1 execution aggregate design

## Durable authority chain

1. The approved parent `OrderIntent` is claimed by one outbox owner.
2. The complete immutable child command set is persisted before the first venue write.
3. Each child advances through an append-audited state machine around the adapter boundary.
4. The parent is ACKED only when every child has an identity-bound venue acknowledgement.
5. A crash-ambiguous send becomes `UNKNOWN`; recovery is query-by-client-order-ID, never blind retry.

## Exposure arithmetic

The executable signed quantity is:

`target - current venue position - durable in-flight child remainder`

All three operands use `Decimal`. Reversals therefore submit the full signed difference, while
already planned, acknowledged, partially filled or unknown children reserve their remaining
exposure and prevent duplicate risk.

## Event projection

Normalized account-stream order events update the exact child selected by client-order ID.
Duplicate event IDs are idempotent. Orders not present in the aggregate store (for example,
protective or external orders) remain in the global durable projector but cannot mutate or
invalidate an unrelated parent/child aggregate.

## Persistence parity

- SQLite: local durable restart and replay contract.
- PostgreSQL: forward-only migration, fenced child transitions and append-only child events.
- Both stores project restart-time `SENDING` children to `UNKNOWN`.
