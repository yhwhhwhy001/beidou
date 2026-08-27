# First-wave Agent dispatch

No implementation Agent may start from the package as generated. `BD-AF-P0-T00` is intentionally on human-baseline HOLD.

After a human approves an immutable clean isolated baseline, dispatch in this order:

1. `BD-AF-P0-T00` — baseline custodian. It records and proves isolation; it does not refactor product code.
2. `BD-AF-P1-T01` — contracts/architecture Agent. It starts only after T00 is independently accepted.
3. `BD-AF-P1-T02` — CLI/isolation Agent. It starts only after T01 is independently accepted.
4. `BD-AF-P2-T03` through `BD-AF-P3-T08` — strictly serial unless a revised dependency proof demonstrates safe parallelism.

For each dispatch, supply exactly:

- absolute clean worktree path;
- package archive SHA-256 and extracted package path;
- package-bound public `allowed_signers` file plus the human-signed approval envelope for that task and baseline; never provide a private key;
- the external result directory containing dependencies' signed and independently accepted task-result custody chains;
- task-specific four-file contract.

Do not dispatch downstream work speculatively. The single initial worker limit is deliberate: checkpoint/resume and statistical-family semantics must be proven before introducing scheduling concurrency.
