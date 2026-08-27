# Rollback — Candidate versus Champion decision

Decisions are append-only and superseded, never rewritten or deleted.

1. Disable the new comparison command from issuing new decisions.
2. Preserve and hash all decision/input bundles and supersession links.
3. Mark affected unresolved decisions `NOT_VERIFIABLE`; never convert them to promotion.
4. Run prior diagnostic comparison only on frozen inputs and record differences without changing lifecycle state.

Rollback fails if historical decisions are mutated, missing evidence becomes a default, a promotion side effect remains reachable, or the Champion/input identity cannot be reconstructed.
