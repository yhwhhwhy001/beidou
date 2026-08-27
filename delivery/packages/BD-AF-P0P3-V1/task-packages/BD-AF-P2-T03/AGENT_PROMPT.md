# Agent prompt — ExperimentRun ledger implementer

After READY preflight, write the state machine and adversarial fixtures before the store. Keep contracts I/O-free and inject clocks/identities explicitly.

Implement append-only events with hash chaining, version handling, idempotency conflict detection, and stale-writer fencing. Make replay deterministic. Do not silently repair corrupt history, infer missing identity fields, or coerce unknown versions.

Use a real supported local store for integration proof; an in-memory fake alone is insufficient. Do not add network dependencies. Preserve raw ledger/replay manifests and migration rollback evidence for an independent state-machine reviewer.
