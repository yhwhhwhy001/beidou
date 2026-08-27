# Agent prompt — baseline custodian

Read the package manifest, operating protocol, and all files in this task directory. Do not modify anything until `preflight_execution.py` returns `READY` for an approval envelope supplied by a human.

Your only objective is to prove a clean isolated baseline without losing, hiding, adopting, or changing the original dirty worktree. Treat the recorded 67 paths as user-owned. You may inspect Git and write evidence only to the approved evidence directory.

Start by reproducing the expected HOLD on the source checkout. Verify the current tracked-change facts against `baseline/source-custody-manifest.json`; it must still report 67 entries and the package-bound digest. After the human provides an isolation choice and revised package, verify exact commit equality, cleanliness, path containment, package checksum, approval binding, and recoverability. Stop on any mismatch.

Do not implement architecture changes, run exchange/runtime processes, create Git history, or approve your own result. Return `TASK_RESULT.md` with status and evidence hashes to an independent reviewer.
