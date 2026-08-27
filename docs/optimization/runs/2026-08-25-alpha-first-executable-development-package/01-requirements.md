# Alpha-First executable development package requirements

## Scope and authority

This run produces a deterministic, inspectable development package. It converts the approved `Vertical-Slice Strangler` pivot into dependency-ordered Agent tasks with contracts, tests, evidence, failure actions and rollback. It does not implement those tasks. The first executable archive covers `AF-REQ-001..009`, `AF-REQ-017` and `AF-REQ-018` through G0–G3; `AF-REQ-010..016` remain an explicitly deferred P4–P8 roadmap and require new packages.

## Requirements

| Requirement ID | Priority | Requirement | Observable package acceptance |
|---|---|---|---|
| `AF-REQ-001` | P0 | Preserve and isolate the current user-owned dirty worktree before implementation. | `AF-T00` cannot PASS without a clean immutable baseline or an explicitly documented isolated worktree that leaves all 67 starting paths recoverable. |
| `AF-REQ-002` | P0 | Replace concrete Alpha→Execution coupling with neutral contracts and composition roots. | Architecture oracle rejects Alpha imports of concrete execution/safety/launcher/certification/production packages and fails on an empty scan. |
| `AF-REQ-003` | P0 | Make the root CLI safe and explicit. | Bare `beidou` performs no trading/runtime start or external write; only an explicit `beidou execution start` path may request runtime construction. |
| `AF-REQ-004` | P0 | Prove offline Alpha research is independent from safety, launcher and network availability. | Mining/replay runs from bound local data while network is denied and forbidden packages are made unimportable. |
| `AF-REQ-005` | P0 | Add an append-only, versioned `ExperimentRun` ledger. | Run identity binds code, policy, dataset, feature, label, cost, seed, universe and stage transitions; corrupt/duplicate transitions fail closed. |
| `AF-REQ-006` | P0 | Define complete checkpoint and deterministic resume semantics. | Fault injection at every stage resumes without skipped/duplicated candidates and preserves the declared candidate family/multiple-testing universe. |
| `AF-REQ-007` | P0 | Bind all promotable results to PIT lineage and sealed OOS evidence. | Missing, mutable, leaked or unsealed lineage yields `NOT_VERIFIABLE`, never promotion. |
| `AF-REQ-008` | P0 | Make scientific validation semantics canonical and adversarially testable. | WFO/CPCV/PBO/DSR/FDR/cost/capacity gates reject semantic negative fixtures and cannot be bypassed by raw PASS fields. |
| `AF-REQ-009` | P0 | Compare Candidate and Champion by incremental portfolio value, not standalone Sharpe. | Decision binds confidence/uncertainty, correlation, turnover, cost, capacity, tail and regime contribution; missing inputs are `NOT_VERIFIABLE`. |
| `AF-REQ-010` | P0 | Use one canonical Alpha kernel across research and Paper/Testnet composition. | Same-data/same-cost parity and factor hash tests detect any second implementation or semantic drift. |
| `AF-REQ-011` | P0 | Extract Execution Truth without weakening uniqueness, accounting or recovery. | Intent→order→fill lineage is explainable; fee/funding/partial fills are unique; position/PnL replay and reconciliation converge or writes pause. |
| `AF-REQ-012` | P0 | Keep UNKNOWN resolution fail closed. | Ambiguous submission is queried through venue facts before retry; no blind resubmit or risk increase occurs while unresolved. |
| `AF-REQ-013` | P0 | Apply an environment-specific Testnet hard-safety matrix. | Demo host, credential scope, idempotency, exposure/concentration, margin/liquidation, ACKed protection/equivalent exit, kill switch and reconciliation are hard for risk increase; R2-R5 only affect the relevant Alpha lifecycle. |
| `AF-REQ-014` | P0 | Make Mainnet absence an artifact property. | Alpha-first distribution contains no production default/endpoint/constructor/call path and rejects arbitrary hosts; independent negative oracle passes. |
| `AF-REQ-015` | P1 | Cut over CLI/wheel and remove legacy only after dual-run evidence and rollback proof. | No old callers/imports/fallback hits, isolated install boots, rollback rehearsal passes, and signed/hash-bound prior artifact is recoverable before deletion. |
| `AF-REQ-016` | P1 | Treat 92% Alpha allocation as a measured North Star, not a PR-diff gate. | Resource policy records staff time, compute cost, valid-candidate yield, time-to-evidence and P0 interrupt cost; file ratios are diagnostic only. |
| `AF-REQ-017` | P0 | Maintain full task/evidence traceability and independent high-risk review. | Every P0/P1 requirement maps to task, test, acceptance evidence, failure action and future specialist Gate; orphan IDs fail package validation. |
| `AF-REQ-018` | P0 | Keep runtime authority separate from package readiness. | Package status can be READY while implementation/Testnet/Mainnet remain NOT_AUTHORIZED; no task prompt grants authority beyond the current user turn. |

## Non-goals for this run

- No refactor implementation or test changes.
- No Git worktree/branch/tag/commit/push operation.
- No cleanup, staging or adoption of the existing 67 dirty paths.
- No dependency installation or lockfile change.
- No Paper/Testnet launch, account query, order, cancel, leverage change or cleanup.
- No economic, profitability, runtime-readiness, production-readiness or Mainnet claim.

## G1 decision

`APPROVED` for package generation. Implementation remains outside this run's authority and is dependency-gated from `AF-T00` onward.
