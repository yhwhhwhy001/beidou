# Rollback — resume vertical slice

The old non-resumable path may remain available for fresh research runs only; it must never consume a resume identifier.

1. Disable `--resume` routing without changing stored ledger/checkpoints.
2. Prove resume requests fail explicitly and cannot fall through to a fresh run.
3. Run the prior fresh-run characterization on frozen inputs.
4. Preserve all crash-matrix artifacts for diagnosis and future replay.

Rollback fails if a resume request starts new work, candidate history is altered, checkpoint state is deleted, or statistical-family membership changes.
