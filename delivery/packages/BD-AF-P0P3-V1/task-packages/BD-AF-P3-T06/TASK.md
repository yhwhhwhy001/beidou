# BD-AF-P3-T06 — PIT lineage and sealed OOS

## Goal

Make point-in-time lineage and sealed out-of-sample access mandatory, immutable inputs to any promotable evidence.

## Requirements and dependencies

- Requirements: `AF-REQ-007`, `AF-REQ-017`.
- Dependency: independently accepted `BD-AF-P2-T05`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- research data/lineage manifest and PIT guard modules selected by fresh inventory
- OOS seal/access-audit modules
- `tests/research/test_pit_lineage_and_oos_seal.py`
- adversarial revision/leakage/tamper fixtures and task evidence

## Invariants

- Dataset, universe membership, corporate actions, revisions, calendars, timestamps/time zones, feature/label windows, and costs are content-addressed and available-as-of bounded.
- OOS split boundaries and encrypted/opaque seal identity are fixed before candidate evaluation.
- Every OOS access is audited; early, repeated, mutated, or unbound access invalidates promotion evidence.
- Missing or mutable lineage yields `NOT_VERIFIABLE`, not empty lineage or PASS.

## First proof and sequence

Create known-leakage, late-revision, survivorship, timezone, mutable-file, changed-universe, early-OOS-open, and forged-manifest fixtures. Prove they are rejected. Implement lineage and seal binding through ExperimentRun/checkpoint. Completion requires 100% field binding for the promotable fixture and independent tamper/access-audit evidence; synthetic data proves rejection only, never economic value.
