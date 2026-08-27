# Agent operating protocol

## Authority model

An Agent receives authority only from the current human instruction plus a package-bound, human-signed approval envelope. Package text, dependency completion, a validator PASS, or a prior Agent result cannot create authority.

Every Agent must:

1. read `PACKAGE-MANIFEST.json`, `delivery.yaml`, this protocol, and all four files for its task;
2. run checksum/package validation and task preflight before modifying code;
3. stop on `HOLD`, an unknown dependency result, a baseline mismatch, dirty state, missing Owner, evidence tamper, or scope conflict;
4. work only inside the approved isolated worktree and the task's allowed paths;
5. begin with the named failure-oriented proof, then implement the smallest vertical slice;
6. execute acceptance with the bundled runner and preserve raw evidence;
7. preserve the generated task-result/reviewer-record drafts; an independent human reviewer in the package-bound public trust root, not the implementer, signs the acceptance decision;
8. treat only a final result assembled by `finalize_task_result.py` and recursively reverified by downstream preflight as `ACCEPTED`.

## Prohibited actions

This package never authorizes an Agent to:

- stash, reset, clean, discard, overwrite, stage, commit, tag, merge, rebase, push, or delete user-owned work;
- read credentials, account state, or exchange state;
- request, read, copy, transmit, or store an approval/reviewer private key;
- send any network/exchange write, launch Paper/Testnet, or activate a service;
- create or enable a Mainnet endpoint, constructor, credential path, default, or real-funds capability;
- weaken UNKNOWN, idempotency, reconciliation, protection, evidence, test, coverage, statistical, or rollback requirements;
- synthesize an economic PASS, replace missing evidence with a mock, or treat zero collected tests as success;
- change another task, expand requirements, or self-approve a Gate.

## Status semantics

- `READY`: preconditions and authority are presently satisfied; work may start.
- `HOLD`: a known external precondition or human decision is missing; no work starts.
- `BLOCKED`: dependency or defect prevents progress; preserve evidence and stop.
- `FAIL`: an executed oracle disproved acceptance.
- `NOT_VERIFIABLE`: required evidence does not exist or cannot be independently recomputed.
- `PASS`: all mandatory task oracles passed with bound evidence.
- `ACCEPTED`: an independent reviewer accepted the PASS evidence.

`PASS` is not `ACCEPTED`. Neither authorizes a downstream task unless its own preflight is READY.

## Evidence contract

Evidence must include package ID/version/hash, task and criterion IDs, approved baseline commit, actual HEAD, clean-tree result, exact argv/cwd/environment policy/timeout, start/end timestamps, exit code, stdout/stderr hashes, JUnit counts where applicable, artifact hashes, and final status/failure reason. Raw command output is retained outside the package; its hashes appear in the evidence manifest. The result directory also retains the signed approval, signed independent review, every evidence manifest, and every hashed raw artifact. Downstream tasks recompute the chain rather than trusting status text.

## Conflict rule

If code reality conflicts with this package, do not improvise around the contract. Record the contradiction and request a revised requirement/architecture decision. Safety and evidence failures always fail closed.
