# BD-AF-P2-T05 — Resume command and crash matrix

## Goal

Complete one end-to-end resumable Factor Miner slice and prove that crash/restart cannot skip, duplicate, reorder, or select a different candidate family.

## Requirements and dependencies

- Requirements: `AF-REQ-006`, `AF-REQ-018`.
- Dependency: independently accepted `BD-AF-P2-T04`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- `apps/factor_miner/__main__.py`
- `apps/factor_miner/worker.py`
- `beidou_research/mining/orchestrator.py`
- `beidou_research/mining/runner.py`
- the previously accepted `beidou_research/experiments/` contracts
- `tests/research/test_factor_miner_resume.py`
- `tests/research/test_resume_crash_matrix.py`
- task-owned frozen input/evidence fixtures

## Invariants

- `--resume RUN_ID` resolves exactly one compatible run/checkpoint or fails closed.
- Candidate IDs, order-independent set, attempts, evidence hashes, rejection reasons, multiple-testing family, and final normalized result equal the uninterrupted oracle.
- Fault injection covers every durable-transition boundary, including before/after event and checkpoint writes.
- A crash never turns an already-attempted candidate into a new statistical draw.
- Scope remains jobs=1 and the single bound grid/dataset/universe/timeframe.

## First proof and sequence

Capture the current `NOT_IMPLEMENTED` behavior. Build a frozen uninterrupted oracle, inject one crash at each transition, resume, and compare normalized outputs. Add duplicate/reorder/missing-checkpoint and repeated-crash negatives. Completion requires a complete crash matrix with no skipped/duplicated candidate and no denominator/hash drift.
