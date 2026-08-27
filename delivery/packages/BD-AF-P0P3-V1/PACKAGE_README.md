# BD-AF-P0P3-V1 — Alpha-First G0–G3 development package

This archive is an executable **development-plan package**, not an implementation or runtime package. It converts the approved Alpha-First `Vertical-Slice Strangler` decision into nine dependency-ordered Agent contracts covering baseline isolation, neutral boundaries, a safe CLI, resumable experiments, sealed evidence, and Candidate-vs-Champion decisions.

## Decision and authority

- Package specification: `PASS_WITH_CONDITIONS` once the bundled validators pass.
- Implementation start: currently `HOLD`; the package requires an external human envelope and cannot authorize implementation itself. A clean immutable implementation baseline, package-bound public approval trust root, and signed approval for `BD-AF-P0-T00` are absent.
- Paper/Testnet action: `NOT_AUTHORIZED`.
- Mainnet/real funds: `PROHIBITED`.
- Git branch/tag/commit/push and modification of the observed user-owned dirty tree: `NOT_AUTHORIZED` by this package.

Package readiness therefore means only that the task contracts are internally consistent, complete, inspectable, deterministic, and fail closed. It does not mean that any refactor behavior, economic value, runtime safety, or exchange behavior has passed.

## Included and deferred phases

Included:

- `P0`: human baseline selection and quarantine proof;
- `P1`: neutral contracts, architecture oracle, safe CLI, offline isolation;
- `P2`: append-only `ExperimentRun`, complete checkpoint, deterministic resume;
- `P3`: PIT lineage, sealed OOS, scientific semantics, Candidate-vs-Champion portfolio decision.

Deferred from this archive:

- `P4`: canonical Paper parity;
- `P5`: Execution Truth extraction;
- `P6`: safety-min, Mainnet-absent wheel, and separately authorized Testnet evidence;
- `P7`: CLI/wheel cutover;
- `P8`: legacy removal.

Those phases remain in the source roadmap but require fresh packages and specialist gates. No Agent may infer authority for them from this archive.

## Machine-readable format

Files named `*.yaml` in this package intentionally contain strict JSON, which is valid YAML 1.2. This permits validation with Python's standard library and removes an undeclared YAML-parser dependency. Duplicate JSON keys are rejected.

## Package layout

- `PACKAGE-MANIFEST.json`: frozen package identity, authority, baseline observation, and phase bounds.
- `baseline/source-custody-manifest.json`: path/status/content hashes for the 67 pre-existing tracked changes plus a zero-non-ignored-untracked assertion after excluding only this run's three exact generated artifact directories; it stores no file contents and lets T00 reject any unexpected near-prefix or other untracked addition. Git-ignored local files are outside hashed custody and remain forbidden to touch.
- `delivery.yaml`: requirement/task graph.
- `task-packages/*`: one four-file contract per task.
- `schemas/*`: schemas actively applied by the validator.
- `scripts/validate_package.py`: structural, semantic, graph, authority, and negative-fixture validation.
- `scripts/preflight_execution.py`: read-only repository/baseline/signed-approval preflight plus recursive dependency-result custody verification; it is expected to HOLD now.
- `scripts/baseline_custody.py`: capture/verify the protected tracked-change manifest and approved clean baseline without stashing or resetting.
- `scripts/run_acceptance.py`: post-implementation acceptance-plus-rollback runner that emits hash-bound evidence and unsigned result/review drafts; unavailable until preflight is READY.
- `scripts/finalize_task_result.py`: verifies a human-signed independent review and assembles a downstream-verifiable final task result without accessing a private key.
- `scripts/verify_checksums.py`: exact-file-set and SHA-256 verification.
- `scripts/build_package.py`: deterministic ZIP build and archive verification.
- `scripts/self_test.py`: isolated tamper/path-escape/determinism regression suite plus a synthetic signed READY path through acceptance, rollback, independent review, finalization, and downstream dependency verification.
- `scripts/synthetic_ready_path_test.py`: creates disposable keys, trust data, and a clean Git fixture outside the package; it retains no private keys and proves forged result metadata or changed raw evidence fails closed.

See `QUICK_START.md` for commands and expected results.

On macOS, the acceptance runner wraps every oracle in `sandbox-exec` with `deny network*` and records the executed argv/enforcer in evidence. On a platform without a supported operating-system denial mechanism it returns `NOT_VERIFIABLE`; the environment variable is never treated as sufficient proof of offline execution.

Approvals and independent reviews use detached OpenSSH signatures under separate fixed namespaces. The package binds only the SHA-256 of an external public `allowed_signers` file. Private keys are never package inputs and must not be exposed to an Agent. Downstream preflight recomputes Git changes and raw evidence hashes; an unsigned or self-asserted `ACCEPTED` field is rejected.

## Source decision

The package is derived from:

- `docs/optimization/runs/2026-08-25-alpha-first-architecture-review/03-architecture.md`
- `docs/optimization/runs/2026-08-25-alpha-first-executable-development-package/`

The source snapshot is recorded for traceability only. Because the source worktree was dirty, it is not an implementation baseline.
