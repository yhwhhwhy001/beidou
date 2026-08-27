# BD-AF-P2-T04 — Complete checkpoint protocol

## Goal

Define a versioned atomic checkpoint that contains enough state to resume the exact declared experiment rather than start a statistically different search.

## Requirements and dependencies

- Requirements: `AF-REQ-006`, `AF-REQ-017`.
- Dependency: independently accepted `BD-AF-P2-T03`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- `beidou_research/experiments/checkpoint.py`
- minimal ExperimentRun contract/store extensions
- `tests/research/test_checkpoint_protocol.py`
- task-owned schemas/fixtures/evidence

## Complete checkpoint contract

Bind run/event sequence, previous/event hash, code/policy/data/universe/timeframe/feature/label/cost digests, RNG/search/optimizer state, candidate queues and completed IDs, multiple-testing family/denominator, stage cursor, worker-count policy, idempotency keys, schema version, writer/fencing token, and atomic-storage digest.

Initial resume scope is deliberately `Template Grid × one Dataset × one Universe × one Timeframe × jobs=1`. Concurrency expansion is a later requirement.

## First failing proof and sequence

1. Generate missing-field, corrupt-hash, stale-writer, partial-write, incompatible-version, and denominator-drift fixtures.
2. Define canonical serialization and compatibility policy.
3. Implement atomic write/read and fencing.
4. Prove corrupted or incomplete checkpoints never downgrade to a fresh run.
5. Rehearse recovery using a copied store.

Completion requires field-coverage, mutation, atomicity, version, fencing, and recovery evidence. Missing state is `NOT_VERIFIABLE`, never a default.
