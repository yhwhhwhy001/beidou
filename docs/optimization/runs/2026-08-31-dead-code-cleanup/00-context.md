# Context

- Run ID: `2026-08-31-dead-code-cleanup`
- Repository/version: `/Users/maguannan/beidou`, branch `codex/v4-incident-remediation`, HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`
- Request scope: inspect the local system and remove code that is provably expired, retired, or ineffective; preserve runtime behavior and safety boundaries.
- Applicable instructions: system/developer/user authority; repository `00_EXECUTION_MASTER.md`, `01_AGENT_OPERATING_PROTOCOL.md`, `PACKAGE_README.md`, and `README.md`; no `AGENTS.md` was found.
- User authority: local source, test, and lifecycle-evidence changes. No commit, push, deployment, restart, Testnet/Mainnet write, external message, or production mutation is authorized.
- Technical stack: Python 3.12+, pytest, Ruff, mypy, coverage, Hatch; Binance USD-M Testnet verifier and historical governance modules.
- Commands and baseline: instruction/state inspection completed at `2026-08-30T20:53:54Z`. The fresh pre-change suite recorded `4669 passed, 6 failed`; failures were all in pre-existing dirty work. The final observed tree recorded `4690 passed, 4 failed`; all four failures are registry governance/digest findings in concurrently edited Alpha research files. Cleanup-focused tests recorded `39 passed`.
- Dirty work / protected areas: the checkout starts with 35 modified tracked files, new `apps/testnet_soak`, new tests/docs, and many evidence directories from the V4 incident-remediation run. All pre-existing changes are user-owned and must be preserved. Files overlapping that work require explicit provenance review before editing.
- Concurrent work: additional unrelated edits appeared during this run in `Makefile`, `apps/testnet_verify/cli.py`, `beidou_core/engine.py`, `beidou_launcher/state.py`, `docs/ONE_CLICK_START.md`, and related tests. They are protected and are not attributed to this cleanup.
- Safety boundaries: Mainnet remains prohibited; the global kill switch and all order/UNKNOWN/reconciliation controls are out of cleanup scope. Historical production-governance modules that provide audit or fail-closed compatibility must be isolated, not deleted, unless current consumer and rollback evidence proves safe removal.
- Selected specialists: `complete-development-lifecycle`, `project-onboarding-and-constraints`, `code-simplification`, `deprecation-and-migration`, `regression-impact-analysis`, and `verification-before-completion`.
- Omitted specialists: deployment/production/trading validation because no runtime activation is authorized; performance optimization because no measured performance defect is in scope; security review is deferred unless a candidate touches identity, credentials, write authority, or execution controls.
- Evidence needed: fresh baseline tests and static checks; candidate-by-candidate caller/package/config/test inventory; replacement and rollback proof; focused tests; full repository regression; final diff review.
- Stop conditions: pre-existing edits cannot be separated safely; baseline failure invalidates behavior-preserving cleanup; a candidate has an unknown external consumer; deletion affects persistent state, public compatibility, trading authority, order recovery, or audit evidence without an approved migration path.
- G0 decision: `READY` for read-only inventory and baseline validation with the dirty worktree protected.
