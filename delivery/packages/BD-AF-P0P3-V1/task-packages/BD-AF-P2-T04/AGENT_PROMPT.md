# Agent prompt — checkpoint protocol implementer

Begin only after T03 acceptance. First enumerate every source of future candidate choice and every denominator used by validation; missing one invalidates resume.

Implement canonical, hash-bound checkpoints with atomic replacement and stale-writer fencing. Restrict this slice to jobs=1 and the declared Template-Grid scope. Reject unknown versions, incomplete fields, policy/data/code mismatches, and partial/corrupt writes.

Never regenerate missing candidates, reseed silently, shrink the multiple-testing family, or treat a rejected checkpoint as permission for a new run. Do not broaden to distributed scheduling. Preserve fixtures and recovery evidence for independent review.
