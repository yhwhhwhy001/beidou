# Rollback — safe CLI facade

Before implementation, capture the installed entrypoint mapping and behavior. The rollback artifact is a human-approved prior wheel or exact baseline checkout, never an unverified local source import.

1. Disable the new console mapping in the isolated test environment only.
2. Reinstall the hash-bound prior artifact in a fresh environment.
3. Prove the prior entrypoint imports from that artifact, not the source checkout.
4. Re-run characterization and record behavioral differences.

Rollback is a development recovery proof, not permission to restore an unsafe runtime default in production. Any source-checkout import, unbound wheel, or lost user change fails the rehearsal.
