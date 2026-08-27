# Acceptance report

## Decision

- Development-package Gate: `PASS_WITH_CONDITIONS`.
- G3 executable task package: `READY`.
- Product implementation: `HOLD / NOT_STARTED`.
- Paper/Testnet/deploy/exchange action: `NOT_AUTHORIZED`.
- Mainnet and real funds: `PROHIBITED`.
- Economic value, candidate promotion, and runtime safety: `NOT_VERIFIABLE` by this package.

The package is executable in the narrow sense that a custodian can verify it, run fail-closed preflight, dispatch nine dependency-ordered task contracts, execute each task's declared acceptance and rollback oracles, and assemble signed evidence. It is not executable authority to change product code or start a runtime.

## Frozen artifact

| Item | Value |
|---|---|
| Package | `BD-AF-P0P3-V1` |
| Directory | `/Users/maguannan/beidou/delivery/packages/BD-AF-P0P3-V1` |
| ZIP | `/Users/maguannan/beidou/delivery/dist/BD-AF-P0P3-V1.zip` |
| Exact package files | 75 |
| Package fingerprint (`SHA-256(CHECKSUMS.sha256)`) | `004a2311bb37ac870d2c5bee10c54d960a666e0139f1b834d147894d1baf296e` |
| ZIP SHA-256 | `de568f6cc72f3e10fa785a57f5d3b6619d896a2ffabed3d219c7054ebfcba072` |
| Executable contracts | 9 (`P0`–`P3`) |
| Requirements | 11 in scope, 7 deferred |
| Acceptance / rollback | 18 criteria / 9 mandatory rollback rehearsals |

## Fresh verification evidence

| Verification | Result |
|---|---|
| Exact-file checksum verification | `CHECKSUMS_OK`, 75 files |
| Structural + semantic validation | `PACKAGE_VALID`, 9 tasks, 11 requirements, 18 criteria, 9 rollback rehearsals |
| Negative fixtures | 8/8 rejected |
| Tooling self-test against source checkout | `SELF_TEST_PASS`, 17/17 |
| Synthetic signed chain | T00 `READY → PASS → rollback PASS → TASK_RESULT_FINALIZED`; T01 dependency preflight `READY` |
| Forged dependency result | `HOLD` |
| Raw evidence tamper | `HOLD` |
| macOS offline oracle control | OS network denial enforced |
| Deterministic build | two independent builds were byte-identical |
| ZIP verification | `ARCHIVE_VERIFIED`, 76 archive members |
| Extracted package verification | checksums, validator, negative fixtures, and 17/17 self-tests all passed |
| Python style/static checks | Ruff format check and Ruff check passed for all package scripts |

The current repository preflight correctly exits `3` with `HOLD`: the implementation baseline and approval trust-root hash are unset, signed human approval is absent, and the worktree is not clean. The acceptance runner was also proven not to create a result directory before preflight becomes `READY`.

## Multi-Agent review findings and closure

The decomposition Agent independently checked the phase graph, candidate modules, test targets, evidence targets, forbidden actions, and rollback expectations. The package-conventions review found two blocking custody gaps in an earlier candidate:

1. rollback was declared but not actually executed by the acceptance runner;
2. a dependency could be made to look accepted by forged JSON status fields.

Both are closed in the frozen artifact. Every task now has a validator-enforced rollback contract that the runner executes, and downstream preflight recursively verifies Git ancestry and changed paths, signed approvals and independent reviews, complete acceptance-plus-rollback evidence, dependency hashes, and raw artifact hashes. Private signing keys are never package or Agent inputs.

The final independent review also found three scope-contract mismatches, all closed before the final freeze:

- T05 now authorizes the exact existing `runner.py` and `orchestrator.py` adapters its end-to-end resume contract requires;
- T08 now authorizes the exact existing compare CLI and marginal-contribution file named by its contract;
- T00 custody now covers tracked and non-ignored untracked changes, binds three exact generated-output exclusions, and includes a regression proving an adjacent near-prefix remains protected.

The same Agent then re-ran the official checks against the final `004a…296e` / `de56…a072` hashes and issued `ACCEPTED_WITH_CONDITIONS`, with no remaining package-integrity blocker. The remaining conditions are the intentionally external human activation inputs listed below.

## Source custody

The package run added only untracked delivery and documentation artifacts. It did not modify the 67 pre-existing tracked changes (`63 M`, `4 D`; `333` insertions and `3764` deletions). Their content/status manifest remains bound by entries digest `adffd268828d42ffc0b4542e8a5e6038dd359f1b41bb052ce58f8f974c33c902`; there were zero non-ignored untracked source paths outside the three exact generated-output prefixes. Git-ignored local files were neither read nor changed and remain outside hashed custody.

No branch, tag, commit, push, stash, reset, runtime process, credential, account, exchange, or deployment action occurred.

## Remaining activation conditions

Before T00 can start, a human custodian must create a new immutable package version that binds:

1. a clean, isolated, recoverable implementation baseline commit;
2. the SHA-256 of an external public OpenSSH `allowed_signers` trust root;
3. a correctly scoped, unexpired, human-signed T00 approval envelope.

That future package must be re-hashed and independently verified. Satisfying these conditions authorizes only the named local implementation task; it does not authorize Paper, Testnet, deployment, Mainnet, exchange access, or real funds.
