# Test strategy

This is the risk-selected portfolio for future task execution. Only package validation is run in the current G0-G3 packaging turn.

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| Dirty baseline destroys attribution (`001`) | preflight/characterization | real Git index/worktree, no mutation | isolated worktree | required before every task | HEAD/path/hash manifest and restore proof |
| Cross-domain dependency regression (`002`,`004`) | architecture/import/subprocess | real package imports; mutation fixtures | network denied | per PR + wheel | AST graph, empty-scan and forbidden-import results |
| Bare CLI starts runtime (`003`) | CLI contract + side-effect oracle | real built console script; fake OS boundary only for unit | isolated install | per PR + release smoke | process/network/file/write inventory must remain zero |
| ExperimentRun transition corruption (`005`) | unit/property/state-machine/database integration | in-memory only for unit; real SQLite/PostgreSQL contract before cutover | local/CI DB | per PR + migration gate | append/replay/concurrency/corruption report |
| Resume skips/duplicates candidates or changes denominator (`006`) | deterministic differential + crash matrix | real generator/runner on bound fixture dataset | local multiprocess | per PR + nightly stress | uninterrupted/resumed candidate/evidence hashes |
| Leakage, revisions or OOS peeking (`007`) | semantic negative/property/data-quality | real manifest/PIT code; crafted adversarial datasets | offline sealed workspace | per PR + promotion gate | lineage graph, access audit, seal verification |
| Statistical fake green (`008`) | independent oracle, null experiments, mutation | independent recomputation; controlled synthetic nulls only as negative fixtures | offline | per PR + promotion gate | rejection rates, mutation score, threshold/policy digest |
| Standalone Sharpe defeats portfolio value (`009`) | unit/property/scenario | deterministic portfolio fixtures and real candidate artifacts later | offline/Paper | per PR + promotion gate | comparison bundle with uncertainty and guardrails |
| Research/Paper formula drift (`010`) | differential/parity/architecture | same real implementation and frozen inputs | offline/Paper | per PR + release | output/hash/cost/frequency/attribution diff |
| Duplicate/partial/fee/funding/accounting drift (`011`) | state-machine, integration, replay, chaos | real stores/adapters in shadow; mocked transport cannot prove venue behavior | local DB/Paper/Testnet | per PR + T13 | intent/order/fill/journal/reconciliation bundle |
| UNKNOWN blind retry (`012`) | timeout/503/race/restart negative tests | deterministic fault transport + real authorized Testnet confirmation in T13 | local then Testnet | per PR + runtime gate | terminal-call count, query evidence, convergence/hold state |
| Weak Testnet safety (`013`) | permission/host/cap/protection/recon negative matrix | real package/config; authorized Testnet only in T13 | offline then Demo | per PR + T13 | independent hard-matrix report |
| Mainnet remains constructible (`014`) | package allowlist, string/AST/call graph, runtime deny | real wheel/extracted package | clean isolated environment | protected CI + release | zero forbidden capabilities and arbitrary-host rejection |
| Cutover breaks consumers/rollback (`015`) | compatibility, telemetry, isolated install, rollback drill | real scripts/entrypoints/artifacts | Paper then authorized Testnet | RC + release | zero unknown callers, verified previous artifact and restore transcript |
| Resource metric Goodhart (`016`) | schema/property/scenario | real time/compute records; PR diff only diagnostic | reporting | weekly/phase review | yield/cost/time-to-evidence report and P0 exception audit |
| Orphan task/authority escalation (`017`,`018`) | package schema/graph/prompt scan | real extracted package | any Python >=3.12 | package build + CI | deterministic manifest and validator output |

## Mandatory sequencing

```text
package/schema
→ compile/import/architecture
→ focused unit/property/state-machine
→ database/component integration
→ offline end-to-end and parity
→ full affected regression/coverage/security/migration
→ isolated wheel negative oracle
→ Paper evidence
→ separately authorized Testnet evidence
→ cutover/rollback rehearsal
```

## Mocks and fixtures

- Deterministic synthetic/null data is allowed to prove rejection, numerical behavior and fault handling. It cannot prove economic value, promotion, Paper parity or Testnet readiness.
- Mock transport is allowed to force exact timeout/race paths. It cannot satisfy venue, account, permission, fill, protection or elapsed-time acceptance.
- A PASS field produced by the implementation is never acceptance evidence unless an independent verifier recomputes it from immutable raw evidence.

## Coverage policy

- Line/branch coverage is a guardrail, not the primary proof. Critical contracts, Execution Truth and safety-min require high coverage plus mutation/semantic negative evidence.
- Do not lower the repository's current enforced threshold inside a functional task. Any future threshold change is its own approved decision with mutation and risk-portfolio evidence.
- Glue code may use risk-based coverage only after the behavior portfolio above is active and no P0/P1 acceptance loses evidence.

## Omissions and residual risk

- No future implementation test, full repository suite, Paper/Testnet run or economic experiment is executed in this package-generation turn.
- Current 67-path dirty worktree is not validated by package tests and remains outside any PASS claim.
- Real Testnet and elapsed-time evidence are `NOT_AUTHORIZED`; they cannot be simulated or compressed.
- Performance/capacity thresholds and economic promotion thresholds require human Metric Owners before `AF-T07`/`AF-T08` can PASS.
