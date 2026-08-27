# BD-AF-P2-T03 — Append-only ExperimentRun ledger

## Goal

Introduce the first persistence vertical slice: a versioned append-only `ExperimentRun` identity and transition ledger whose state is reconstructible and tamper-evident.

## Requirements and dependencies

- Requirements: `AF-REQ-005`, `AF-REQ-017`.
- Dependency: independently accepted `BD-AF-P1-T02`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- `beidou_research/experiments/contracts.py`
- `beidou_research/experiments/store.py`
- `beidou_research/experiments/migrations.py`
- `tests/research/test_experiment_run_ledger.py`
- task-owned migration/evidence fixtures

## Required identity and state

Run identity binds code commit/tree state, policy digest, dataset/universe/timeframe/PIT manifest, feature digest, label digest, cost model, seed, and schema version. Events bind monotonic sequence, previous hash, event hash, writer/fencing token, stage, timestamp, payload digest, and idempotency key.

## Invariants and first proof

- No update/delete API for accepted events.
- Duplicate keys are idempotent only for byte-identical facts; conflicts fail.
- Out-of-order, forked-chain, corrupt, unknown-version, and stale-writer events fail closed.
- Replay yields one deterministic state or `NOT_VERIFIABLE`.

Begin with transition-table, corruption, duplicate, concurrency, and replay tests. Implement the smallest store adapter needed by one run. No migration may destroy old data. Completion requires schema digest, transition coverage, replay hash, and concurrency evidence.
