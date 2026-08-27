# Task plan

## Dependency graph

```text
AF-T00 Baseline & quarantine
  └─ AF-T01 Neutral contracts + architecture oracle
       └─ AF-T02 Safe CLI facade + offline isolation
            └─ AF-T03 ExperimentRun ledger
                 └─ AF-T04 Checkpoint protocol
                      └─ AF-T05 Resume + crash matrix
                           └─ AF-T06 PIT lineage + sealed OOS
                                └─ AF-T07 Scientific validation semantics
                                     └─ AF-T08 Candidate vs Champion
                                          └─ AF-T09 Paper parity + attribution
                                               └─ AF-T10 Execution identity shadow
                                                    └─ AF-T11 Accounting/UNKNOWN/reconciliation
                                                         └─ AF-T12 Safety-min + Mainnet-absent wheel
                                                              └─ AF-T13 Authorized Testnet evidence window
                                                                   └─ AF-T14 CLI/wheel cutover
                                                                        └─ AF-T15 Legacy removal
```

## Approved roadmap tasks

Only `AF-T00..AF-T08` are materialized as executable contracts in `BD-AF-P0P3-V1`. `AF-T09..AF-T15` are dependency-gated roadmap entries: they have no task directory, prompt, runnable acceptance contract, or inferred authority in this archive.

| Task | Requirement IDs | Dependencies | Expected files / modules | First failing proof and tests | Evidence / completion condition | Initial status |
|---|---|---|---|---|---|---|
| `AF-T00` Baseline & quarantine | 001,017,018 | none | run baseline manifests; isolated worktree metadata; no product code | preflight must fail on unexplained dirty tree; compile/collect/lint/type/test baseline after human-selected isolation | exact HEAD, protected-path manifest, command hashes, clean isolated baseline; no user diff lost | `BLOCKED_HUMAN_BASELINE_DECISION` |
| `AF-T01` Neutral contracts + architecture oracle | 002,004,017 | T00 | `beidou_shared/contracts/{experiment,alpha_execution}.py`, architecture tests | oracle initially detects current forbidden/concrete paths and empty-scan mutation | neutral contracts contain data/protocols only; Alpha concrete-import count zero | `BLOCKED_DEPENDENCY` |
| `AF-T02` Safe CLI facade + offline isolation | 003,004,018 | T01 | `beidou_cli/*`, `pyproject.toml`, launcher adapter, CLI/architecture tests | bare console currently starts launcher; local run must survive denied network/imports | bare CLI zero side effects; explicit execution command only; compatibility help documented | `BLOCKED_DEPENDENCY` |
| `AF-T03` ExperimentRun append-only ledger | 005,017 | T02 | `beidou_research/experiments/{contracts,store,migrations}.py`, tests | duplicate/out-of-order/corrupt transition fixtures fail | identity binds all lineage/config/code inputs; replay yields identical run state | `BLOCKED_DEPENDENCY` |
| `AF-T04` Complete checkpoint protocol | 006,017 | T03 | `beidou_research/experiments/checkpoint.py`, schema/migration tests | checkpoints missing queues/RNG/denominator are rejected | atomic sequence, integrity hash, compatibility/version policy, stale writer fencing | `BLOCKED_DEPENDENCY` |
| `AF-T05` Resume command + crash matrix | 006 | T04 | `apps/factor_miner/__main__.py`, worker/runner adapters, resume tests | current command exits `NOT_IMPLEMENTED`; inject crash at every stage | resumed candidate family/evidence equals declared uninterrupted oracle; no skip/duplicate | `BLOCKED_DEPENDENCY` |
| `AF-T06` PIT lineage + sealed OOS | 007 | T05 | data manifests, PIT guards, evidence/seal modules, negative fixtures | crafted mutable/revised/leaked/unsealed inputs are negative fixtures and must be rejected | 100% promotable lineage bound; OOS access audit; missing evidence `NOT_VERIFIABLE` | `BLOCKED_DEPENDENCY` |
| `AF-T07` Scientific validation semantic oracle | 008 | T06 | WFO/CPCV/multiple-testing/cost-capacity modules and independent verifier tests | fake PASS, bad purge/embargo, NaN, null-alpha and tampered evidence rejected | independent recomputation agrees; semantic negative suite passes; no threshold weakening | `BLOCKED_DEPENDENCY` |
| `AF-T08` Candidate vs Champion portfolio decision | 009 | T07 | marginal contribution, portfolio contracts, compare CLI/store, tests | current compare exits `NOT_IMPLEMENTED`; standalone-Sharpe counterexample | decision covers uncertainty/correlation/turnover/cost/capacity/tail/regime and is immutable | `BLOCKED_DEPENDENCY` |
| `AF-T09` Canonical Paper parity + attribution | 010 | T08 | alpha kernel adapters, paper simulator/shadow, attribution, parity tests | deliberately divergent formula/cost/frequency must fail parity | one implementation/hash; full PnL lineage; residual within signed tolerance | `BLOCKED_DEPENDENCY` |
| `AF-T10` Execution intent/order/fill identity shadow | 011 | T09 | new `beidou_execution` contracts/journal/adapters; old-path shadow bridge | duplicate intent/fill, partial-fill race, restart fixtures | new shadow journal matches old facts without owning terminal writes | `BLOCKED_DEPENDENCY` |
| `AF-T11` Accounting, UNKNOWN and reconciliation extraction | 011,012 | T10 | fee/funding/position/PnL projections, UNKNOWN resolver, reconciliation, tests | timeout/503 cannot blind retry; corrupted accounting cannot converge | exact replay, unique economic facts, bounded query recovery, unresolved state pauses writes | `BLOCKED_DEPENDENCY` |
| `AF-T12` Safety-min + Mainnet-absent distribution | 013,014,018 | T11 | `beidou_safety_min/*`, packaging allowlist, endpoint/credential guards, negative oracle | production/default/arbitrary host, weak permission, missing protection/recon must fail | independent wheel verifier proves Demo-only constructors and hard Testnet matrix; no runtime activation | `BLOCKED_DEPENDENCY` |
| `AF-T13` Authorized Testnet evidence window | 013,018 | T12 + separate human approval | explicit run contract/evidence only; no source scope unless defect found | preflight must HOLD without account/host/cap/owner/window/cleanup approval | authorized window proves execution truth and attribution; any UNKNOWN stops new risk; cleanup evidence complete | `BLOCKED_SEPARATE_RUNTIME_AUTHORITY` |
| `AF-T14` CLI/wheel cutover + measured resource policy | 003,014,015,016 | T13 | `pyproject.toml`, facade, wheel config, telemetry, resource policy, compatibility docs | old callers/production package/default-start counterfixtures | isolated install boots, old caller telemetry zero for approved window, rollback artifact verified | `BLOCKED_DEPENDENCY` |
| `AF-T15` Legacy removal | 015,016,017 | T14 + human deletion approval | legacy imports/packages/tests/docs selected by fresh caller proof | removal attempted while caller/fallback exists must fail | zero callers/imports/fallbacks across two RCs, full regression, recovery drill and immutable archive | `BLOCKED_SEPARATE_DELETION_AUTHORITY` |

## Execution rules

1. One Agent owns one task worktree and one atomic change set; independent reviewer owns the task Gate.
2. No task starts until all dependencies have current PASS evidence and its preflight is READY.
3. Every task begins with a failure-oriented test/evidence capture and ends with targeted, affected regression, architecture, security/migration and rollback evidence selected by risk.
4. A task may discover extra work but cannot silently expand scope. Create a new requirement/decision or stop for human direction.
5. `AF-T13` and `AF-T15` are intentionally non-runnable without separate authority; package readiness does not satisfy that condition.

## G3 decision target

`READY` only when the machine-readable package validates unique IDs, known dependencies, acyclic graph, complete acceptance/evidence/failure/rollback fields, full requirement coverage, authority boundaries and deterministic archive hashes.

## V5 execution continuation — 2026-08-25

The immutable V5 package materializes nine executable tasks with IDs
`BD-AF-P0-T00`, `BD-AF-P1-T01`, `BD-AF-P1-T02`, `BD-AF-P2-T03`,
`BD-AF-P2-T04`, `BD-AF-P2-T05`, `BD-AF-P3-T06`, `BD-AF-P3-T07`, and
`BD-AF-P3-T08`. The dependency graph is strictly serial. T00 is independently
accepted with conditions; T01 is the next authorized implementation slice.

The T01 approval envelope is externally staged at
`/Users/maguannan/beidou-authorization/BD-AF-P1-T01/BD-AF-P1-T01.approval.json`,
bound to V5 fingerprint `8be70e608f5d79d374dcfcb486ca486a7c8909bf50165716e4d064cf41c737c7`,
baseline `b9daa21dc6628dd394ffae0ed1f344bd35d5de6d`, the original approved
worktree, and the finalized T00 result hash. Its schema validates; implementation
remains held until the human creates the detached signature under the fixed
`beidou-alpha-first-task-approval-v1` namespace. No product files are edited
before T01 preflight reports `READY`.
