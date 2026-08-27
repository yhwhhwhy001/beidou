# BD-AF-P0-T00 — Baseline and quarantine

## Goal

Establish a human-approved, immutable, clean implementation baseline while preserving every pre-existing user-owned tracked or non-ignored untracked change. The three exact generated-output prefixes declared in the source-custody manifest are excluded; ignored local files remain forbidden to touch but are not content-hashed. This task is a custody proof, not a refactor.

## Requirements and dependencies

- Requirements: `AF-REQ-001`, `AF-REQ-017`, `AF-REQ-018`.
- Dependencies: none.
- Initial status: `HOLD_HUMAN_BASELINE_DECISION`.
- Required approvals: `H0_BASELINE_ISOLATION`, `H1_TASK_START_PER_TASK`.

## Allowed scope

- Read-only repository/Git inspection.
- A new evidence directory outside product packages.
- A human-selected isolated worktree or clean clone.
- A revised, revalidated package manifest that records the chosen 40-hex baseline.

## Prohibited scope

- No stash, reset, clean, checkout-overwrite, deletion, staging, commit, push, or adoption of the observed dirty paths.
- No product-code, test, dependency, lockfile, credential, runtime, Paper/Testnet, or account change.

## First failing proof

Capture that this package's source checkout is dirty, the implementation baseline is unset, and preflight returns `HOLD`. Preserve the exact path manifest and hashes without printing secrets.

## Implementation sequence

1. Human chooses a recoverable isolation method and exact commit.
2. Custodian proves the original dirty checkout is untouched and recoverable.
3. Custodian proves the isolated checkout is at the chosen commit and clean.
4. Package maintainer records the baseline in a new package version, regenerates checksums/archive, and obtains H0 approval bound to its SHA-256.
5. Independent reviewer repeats the checks from both checkouts.

## Completion evidence

- original checkout HEAD/status/path-hash manifest;
- bundled source-custody manifest equality (`67` tracked paths, zero non-generated/non-ignored untracked paths, content/status digest and exact generated-output exclusions bound by the package);
- isolated checkout absolute path, HEAD, clean status, and remote-free provenance statement;
- new package/archive SHA-256 and approval-envelope SHA-256;
- independent recovery/read-only comparison transcript.

Any unexplained delta, missing path, mutable baseline, or self-issued approval is `HOLD`/`FAIL`.
