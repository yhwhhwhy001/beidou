# Task plan

1. [COMPLETE] Define immutable parent/child command and aggregate transition contracts.
2. [COMPLETE] Add SQLite and PostgreSQL durable command persistence with a forward-only migration.
3. [COMPLETE] Persist the complete plan before sending, update each child around the adapter boundary and ACK the parent only after all child ACKs.
4. [COMPLETE] Add exact signed target-delta planning that accounts for current and in-flight exposure.
5. [COMPLETE] Project user-stream order updates into the same aggregate contract and verify restart replay/idempotency.
6. [COMPLETE] Run focused and full controlled verification; record remaining environment evidence as NOT_VERIFIABLE.
7. [PENDING] Commit and push the verified P1 slice to `origin/main`, then verify the remote SHA.
