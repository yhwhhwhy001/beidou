# Traceability

Statuses in this file describe package planning only. No requirement is implemented or accepted in this run. Conceptual task aliases `AF-T00..AF-T08` are materialized in the archive as `BD-AF-P0-T00`, `BD-AF-P1-T01..T02`, `BD-AF-P2-T03..T05`, and `BD-AF-P3-T06..T08`. Later aliases are not executable in this archive.

| Requirement ID | Acceptance criterion | Implementation task(s) | Test target | Evidence target | Status |
|---|---|---|---|---|---|
| `AF-REQ-001` | Clean immutable or isolated recoverable baseline before code work | `AF-T00` | dirty-tree negative preflight; manifest/hash replay | `artifacts/evidence/AF-T00/` | `PLANNED / CURRENTLY_BLOCKED` |
| `AF-REQ-002` | Concrete cross-domain imports rejected, empty scan fails | `AF-T01` | AST/import-graph architecture oracle + mutation fixtures | `AF-T01` architecture report | `PLANNED` |
| `AF-REQ-003` | Bare CLI has zero start/write side effects | `AF-T02`, `AF-T14` | isolated console tests; write-registry oracle | CLI behavior and registry evidence | `PLANNED` |
| `AF-REQ-004` | Local-data research works with network/launcher/safety unavailable | `AF-T01`, `AF-T02` | subprocess import-deny/network-deny integration | offline isolation transcript | `PLANNED` |
| `AF-REQ-005` | Append-only versioned ExperimentRun identity and transitions | `AF-T03` | schema/transition/property/concurrency tests | run-ledger replay manifest | `PLANNED` |
| `AF-REQ-006` | Resume has no skipped/duplicate candidate or denominator drift | `AF-T04`, `AF-T05` | state-machine crash matrix, uninterrupted-vs-resumed oracle | checkpoint/replay hash report | `PLANNED` |
| `AF-REQ-007` | PIT lineage and sealed OOS are mandatory for promotion | `AF-T06` | leakage/mutation/revision/manifest negative fixtures | lineage and sealed-split manifest | `PLANNED` |
| `AF-REQ-008` | Scientific gates recompute semantics and reject fake PASS | `AF-T07` | null experiments, known leakage, bad purge/embargo, evidence tamper | validation oracle report | `PLANNED` |
| `AF-REQ-009` | Candidate decision uses incremental portfolio contribution | `AF-T08` | correlation/cost/capacity/tail/regime fixtures + uncertainty | decision bundle and comparison report | `PLANNED` |
| `AF-REQ-010` | One canonical Alpha kernel across research/Paper/Testnet | `AF-T09` | same-data/same-cost/hash parity and second-kernel scan | parity/attribution report | `DEFERRED_OUT_OF_PACKAGE / P4` |
| `AF-REQ-011` | Intent/order/fill/accounting/replay truth remains exact | `AF-T10`, `AF-T11` | property/state-machine, crash/restart, partial-fill, fee/funding/recon | dual-journal parity bundle | `DEFERRED_OUT_OF_PACKAGE / P5` |
| `AF-REQ-012` | UNKNOWN queries facts before retry and blocks new risk | `AF-T11` | timeout/503/stream-gap/query-recovery fixtures; no-resubmit oracle | UNKNOWN convergence report | `DEFERRED_OUT_OF_PACKAGE / P5` |
| `AF-REQ-013` | Testnet hard-safety matrix applies only to risk-increasing execution | `AF-T12`, `AF-T13` | environment parity, permission/host/exposure/protection/reconciliation negatives | safety matrix and authorized Testnet evidence | `DEFERRED_OUT_OF_PACKAGE / P6 / RUNTIME_AUTH_REQUIRED` |
| `AF-REQ-014` | Alpha-first artifact cannot construct Mainnet | `AF-T12`, `AF-T14` | wheel allowlist, string/AST/call graph, arbitrary-host runtime denial | independent artifact oracle | `DEFERRED_OUT_OF_PACKAGE / P6` |
| `AF-REQ-015` | Cutover/deletion requires zero callers and rollback proof | `AF-T14`, `AF-T15` | caller telemetry/import scan/isolated install/rollback rehearsal | cutover and removal evidence | `DEFERRED_OUT_OF_PACKAGE / P7-P8` |
| `AF-REQ-016` | Resource policy uses outcome/cost measures, not PR ratio as gate | `AF-T14`, `AF-T15` | policy schema and anti-Goodhart negative tests | rolling resource report | `DEFERRED_OUT_OF_PACKAGE / P7-P8` |
| `AF-REQ-017` | All requirements map bidirectionally to tasks/tests/evidence | all tasks; package validator | orphan/duplicate/cycle/unknown-ID mutations | package validation report | `PLANNED` |
| `AF-REQ-018` | Package readiness never implies implementation/runtime authority | all task prompts; `AF-T13` | authority-schema and forbidden-action scan | package status and prompt scan | `PLANNED` |

## Orphan and status rules

- Every requirement must appear in at least one task and every task must reference at least one requirement.
- Every mandatory acceptance criterion must name an evidence target and failure action.
- `PLANNED`, `BLOCKED`, `NOT_VERIFIABLE`, `PASS` and `ACCEPTED` are distinct. Package validation can make G3 READY; it cannot advance G4-G10.
- Historical BD-T or T24 evidence may be linked as context only. It cannot populate current PASS/FAIL fields or satisfy a current acceptance criterion.
