# Quick start

Run package commands from the extracted package root with Python 3.12 or newer. These commands validate a development package; they do not authorize implementation or runtime activity.

## 1. Verify exact files and checksums

```bash
python scripts/verify_checksums.py
```

Expected: exit `0`, `CHECKSUMS_OK`, and a package fingerprint. Missing, added, changed, duplicate, absolute, escaping, cached, or symlinked paths fail.

## 2. Validate contracts and negative fixtures

```bash
python scripts/validate_package.py --negative-fixtures
```

Expected: exit `0`, `PACKAGE_VALID`, nine tasks, eleven in-scope requirements, eighteen acceptance criteria, nine mandatory rollback rehearsals, and eight rejected invalid fixtures.

## 3. Rebuild and verify a deterministic archive

```bash
python scripts/build_package.py --output /tmp/BD-AF-P0P3-V1.zip
python scripts/build_package.py --verify /tmp/BD-AF-P0P3-V1.zip
```

Expected: both commands exit `0`; unchanged inputs produce byte-identical ZIP files.

## 4. Confirm that implementation remains fail-closed

From the observed repository checkout:

```bash
python delivery/packages/BD-AF-P0P3-V1/scripts/preflight_execution.py \
  --repo . \
  --task BD-AF-P0-T00
```

Expected for this V1 package: exit `3` and `HOLD`. The manifest intentionally has no human-selected clean implementation baseline, no approval trust-root hash, and no signed task approval. The observed source worktree is user-owned and dirty. Do not edit V1 in place or bypass these reasons.

## 5. Optional complete tooling self-test

```bash
python scripts/self_test.py
```

Add `--repo /absolute/path/to/beidou` to also recapture protected-source custody (67 tracked changes, zero unexpected non-ignored untracked files outside the three named generated-output prefixes), prove that an adjacent near-prefix cannot escape custody, prove that preflight and the acceptance runner make no result directory while the package is on HOLD, and run a disposable signed READY path through acceptance, mandatory rollback, independent review, finalization, and downstream dependency verification. The self-test also rejects checksum tampering, unlisted and escaping files, symlinks, malicious ZIP members, custody tampering, forged accepted dependency metadata, and changed raw evidence.

## Human activation prerequisites for a later package version

A human package custodian must create and validate a new immutable package version before any Agent starts:

1. Select a clean, recoverable, isolated worktree and immutable baseline commit without altering the protected dirty checkout.
2. Create an external OpenSSH `allowed_signers` public-key file for authorized human approvers/reviewers. Never store or disclose private keys in the package, repository, task prompt, result directory, or Agent context.
3. Record the baseline commit and SHA-256 of that exact public `allowed_signers` file in the revised manifest; regenerate checksums and the deterministic archive.
4. Fill `templates/human-approval-envelope.template.json` with the revised package fingerprint, task baseline, exact approval IDs, Owner bindings, and direct dependency-result hashes.
5. The authorized human signs the exact approval bytes under the fixed namespace:

```bash
ssh-keygen -Y sign \
  -f /human-controlled/path/to/private_key \
  -n beidou-alpha-first-task-approval-v1 \
  /external/path/BD-AF-P0-T00.approval.json
```

The human retains the private key. Only the approval JSON, detached `.sig`, and public `allowed_signers` path are supplied to the workflow.

Preflight then uses all three external trust inputs:

```bash
python scripts/preflight_execution.py \
  --repo /absolute/path/to/approved/clean/worktree \
  --task BD-AF-P0-T00 \
  --approval /external/path/BD-AF-P0-T00.approval.json \
  --approval-signature /external/path/BD-AF-P0-T00.approval.json.sig \
  --approval-trust-root /external/path/approval-trust-root.allowed_signers \
  --mode start
```

For a task with dependencies, also pass `--dependency-results-dir /external/path/to/results`. Preflight recursively verifies every ancestor result, Git ancestry and allowed paths, signed approvals/reviews, the complete acceptance-plus-rollback evidence set, and raw artifact hashes. A JSON field named `ACCEPTED` is never sufficient.

## Post-implementation acceptance and independent review

After an explicitly authorized human/Git workflow records the task changes as an immutable commit and restores the approved worktree to clean state, run the exact task oracles and rollback rehearsal. The V1 package itself grants no Agent permission to commit:

```bash
python scripts/run_acceptance.py \
  --repo /absolute/path/to/approved/clean/worktree \
  --task BD-AF-P0-T00 \
  --approval /external/path/BD-AF-P0-T00.approval.json \
  --approval-signature /external/path/BD-AF-P0-T00.approval.json.sig \
  --approval-trust-root /external/path/approval-trust-root.allowed_signers \
  --results-dir /external/path/to/results \
  --implementer-identity implementer-identity
```

Add `--dependency-results-dir /external/path/to/results` after T00; it must be the same absolute directory as `--results-dir`, so one task chain has one evidence root. The runner first requires acceptance-mode preflight `READY`, creates a task-scoped evidence bundle, executes exact argv without a shell under OS-enforced denied networking, verifies JUnit counts/artifacts, runs the mandatory rollback oracle, and emits task-result and reviewer-record drafts only on full `PASS`.

An independent human reviewer copies the reviewer-record draft outside the result directory, replaces the two explicit identity/time placeholders, records only P2/P3 residual risks if any, validates it, and signs the exact bytes:

```bash
python scripts/validate_package.py \
  --validate-reviewer-record /external/path/BD-AF-P0-T00.reviewer-record.json

ssh-keygen -Y sign \
  -f /human-controlled/path/to/private_key \
  -n beidou-alpha-first-task-review-v1 \
  /external/path/BD-AF-P0-T00.reviewer-record.json
```

The finalizer verifies that signature and exact draft binding, then assembles the downstream-verifiable result without reading a private key:

```bash
python scripts/finalize_task_result.py \
  --results-dir /external/path/to/results \
  --task BD-AF-P0-T00 \
  --reviewer-record /external/path/BD-AF-P0-T00.reviewer-record.json \
  --reviewer-signature /external/path/BD-AF-P0-T00.reviewer-record.json.sig \
  --approval-trust-root /external/path/approval-trust-root.allowed_signers
```

`PASS` is not `ACCEPTED`; `ACCEPTED` is not runtime authorization. Paper, Testnet, deployment, exchange/account access, Mainnet, and real funds remain outside this package.
