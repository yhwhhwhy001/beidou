# BD-AF-P1-T01 — Neutral contracts and architecture oracle

## Goal

Break concrete Alpha-to-Execution coupling with neutral data/protocol contracts and independently executable composition roots. Prove the boundary with a non-vacuous architecture oracle.

## Requirements and dependencies

- Requirements: `AF-REQ-002`, `AF-REQ-004`, `AF-REQ-017`.
- Dependency: independently accepted `BD-AF-P0-T00`.
- Required approval: `H1_TASK_START_PER_TASK`.

## Allowed paths

- `beidou_shared/contracts/experiment.py`
- `beidou_shared/contracts/alpha_execution.py`
- minimal Alpha/application composition modules approved by the architecture decision
- `tests/architecture/test_alpha_first_import_boundaries.py`
- `tests/integration/test_alpha_offline_isolation.py`
- task-owned docs/evidence

All other paths require a revised task contract.

## Invariants

- `alpha`, `execution`, `safety_min`, and `simulator` never import another domain's concrete implementation.
- Neutral contracts contain data, enums, errors, and protocols only; no client construction, I/O, global registry, or environment lookup.
- The oracle inventories expected Alpha modules and fails if discovery is empty or a known mutation survives.
- Offline Alpha import and local-data work do not require launcher, safety, exchange SDK, DNS, sockets, or credentials.

## First failing proof and sequence

1. Add the boundary inventory and mutation/empty-scan tests; record current violations.
2. Introduce the smallest neutral contracts needed by one vertical slice.
3. Move construction to explicit composition roots without compatibility fallbacks into concrete packages.
4. Prove forbidden-import count is zero and offline isolation works in a subprocess.
5. Run affected regression and independent architecture review.

Completion requires bound JUnit, inventory, mutation, import-graph, and offline transcripts. A reduced scan, ignored violation, or compatibility import is FAIL.
