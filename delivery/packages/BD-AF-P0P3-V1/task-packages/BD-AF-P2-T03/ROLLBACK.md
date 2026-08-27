# Rollback — ExperimentRun ledger

The migration must be additive and old readers must remain selectable during this phase.

1. Stop new ledger writes through the local feature/composition selection.
2. Export and hash the append-only events; never delete or rewrite them.
3. Select the prior read path and prove pre-task behavior on the same immutable inputs.
4. Re-enable the new reader against a copy and prove replay equality before any retry.

Rollback fails if a destructive migration is required, events cannot be exported/replayed, writer fencing is ambiguous, or the prior path depends on modified source outside the task.
