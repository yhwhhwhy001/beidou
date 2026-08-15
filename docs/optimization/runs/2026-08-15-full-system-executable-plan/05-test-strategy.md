# Test strategy

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| TASK-M00-C00 checkout isolation | Git/worktree contract | Real Git metadata and process metadata | Current checkout + isolated worktree | Pre-development hard gate | current-state.yaml、implementation log |
| UNKNOWN terminal write | Unit/property/contract/failure | Fake transport only for negative local tests; no exchange write | Isolated worktree | Required before M00-C exit | focused red/green output |
| Entry/capability bypass | Static architecture + subprocess dry-run | Real entry modules; network disabled | Isolated worktree/CI | Required before M00-E exit | Entry/Write Capability Registry |
| Fake certificate acceptance | Unit/property/mutation + independent verifier | Immutable local fixtures | Isolated worktree + protected CI design | Required before M00-V exit | verifier evidence manifest |
| Environment semantic divergence | Parameterized contract + AST/call graph | Real safety code, synthetic facts | Isolated worktree | Any OPEN diff hard-blocks Testnet | semantic manifest/report |
| Truth read side effect | Property/state machine/clock failure | Deterministic clock and real truth objects | Isolated worktree | Required before M00-T CONTRACT_READY | hash/timestamp evidence |
| Durable intent/order/position chain | Unit→real PostgreSQL integration→fault injection | Real isolated PG only; no running DB | Later authorized isolated environment | Required before funds spine acceptance | replay/reconciliation report |
| Market/research validity | Reference/property/PIT/WFO/CPCV/PBO/DSR | Recorded real data + sealed datasets | Offline | Required before promotion | domain reports |
| Testnet lifecycle | Contract first; real Testnet only under separate authority | Dedicated exclusive account required | NOT AUTHORIZED | CERT-G5 blocked | no evidence yet |

## Omissions and residual risk

- No existing process, exchange, account, running database, deploy, Mainnet, or real-funds test is authorized.
- Mock/fake transports may prove local negative behavior but cannot satisfy real integration or Testnet gates.
- Coverage is secondary evidence: denominator, omit/pragma/skip and thresholds must be frozen before using coverage as a Gate.
- Full-suite green cannot override semantic counterexamples or an OPEN P0.
