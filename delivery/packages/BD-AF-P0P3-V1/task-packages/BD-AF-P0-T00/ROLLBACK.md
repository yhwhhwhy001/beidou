# Rollback — baseline custody

No product mutation is permitted, so rollback means abandoning the proposed isolated checkout/package revision without touching the original checkout.

1. Stop if the original checkout status or hashes differ from the initial custody manifest.
2. Mark the proposed baseline and approval envelope `REVOKED`; do not reuse their hashes.
3. Preserve both before/after read-only manifests for investigation.
4. A human may later remove the isolated checkout through an explicitly authorized recoverable operation; the Agent must not delete it.
5. Re-run the source-checkout comparison. Success means every protected path and HEAD matches the initial custody manifest and no Git refs or index entries were changed by the Agent.

If recoverability cannot be proven, status is `FAIL_P0`; downstream tasks remain on HOLD.
