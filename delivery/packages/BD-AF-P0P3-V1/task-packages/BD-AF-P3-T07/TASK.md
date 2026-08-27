# BD-AF-P3-T07 — Scientific validation semantic oracle

## Goal

Make WFO, CPCV/PBO, DSR/FDR, cost, capacity, stability, and sample sufficiency canonical and independently recomputable rather than trusting implementation PASS flags.

## Requirements and dependencies

- Requirements: `AF-REQ-008`, `AF-REQ-017`, `AF-REQ-018`.
- Dependency: independently accepted `BD-AF-P3-T06`.
- Required approvals: `H1_TASK_START_PER_TASK`, `H2_METRIC_OWNER_BEFORE_BD_AF_P3_T07`.

## Allowed paths

- Alpha validation/statistics policy and result contracts
- independent verifier module that does not call implementation decision code
- `tests/research/test_scientific_validation_semantics.py`
- null, leakage, purge/embargo, NaN/Inf, multiple-testing, cost/capacity, and tamper fixtures

## Required semantics

The human Metric Owner must freeze formulas, units, sampling clock, fold construction, purge/embargo, family definition/denominator, thresholds, uncertainty policy, cost/capacity model, minimum samples, failure semantics, and change control before coding.

## First proof and sequence

Demonstrate that fake raw PASS fields, bad purge/embargo, null alpha, denominator shrinkage, NaN/Inf, cost omission, low sample size, and evidence tampering are rejected by an independent oracle. Implement canonical result contracts and recomputation. Mutation tests must show every hard gate affects the decision. No synthetic/null experiment can establish profitability. Completion requires Owner policy digest, independent agreement, semantic negative suite, and zero threshold weakening.
