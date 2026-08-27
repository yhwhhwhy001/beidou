# Rollback — checkpoint protocol

Checkpoints are new immutable artifacts and must never overwrite the last known-good generation.

1. Stop the checkpoint writer through local composition selection.
2. Retain and hash every generation, including corrupt fixtures.
3. Select the prior ExperimentRun path, which must refuse resume rather than fabricate state.
4. On a copied store, load the last compatible checkpoint and prove its chain to the ledger.

Rollback succeeds only when no original checkpoint/event is changed and a rejected checkpoint remains rejected. Automatic fresh-start fallback, generation deletion, or fencing ambiguity is failure.
