# Rollback — scientific validation semantics

Previous validation outputs remain historical evidence only and cannot regain promotable status automatically.

1. Freeze new promotion decisions and mark affected results `NOT_VERIFIABLE`.
2. Preserve policy versions, raw statistics, family membership, and independent recomputation evidence.
3. Select the prior calculator only for diagnostic comparison, never as a promotion fallback.
4. Recompute a frozen fixture under both versions and record the semantic delta.

Rollback fails if thresholds are silently restored, failed candidates leave the family, raw PASS is trusted, or results lose policy/lineage binding.
