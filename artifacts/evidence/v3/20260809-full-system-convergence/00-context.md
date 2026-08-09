# Context

- Run ID: 2026-08-09-full-system-convergence
- Repository/version: `/Users/maguannan/beidou`, `52cd5558fe0336e1553df5bb63f9dadcb1f0cecc`
- Branch: `codex/full-system-convergence-v3`
- Applicable instructions: repository has no `AGENTS.md`; `00_EXECUTION_MASTER.md`; BD-T00/T01/T02/T03/T15 task packages; financial safety contract.
- User authority: scoped local code and evidence changes; no Git push, deployment, restart, exchange write, credential change, order action or Mainnet action.
- Technical stack: Python, asyncio, pytest, SQLite runtime, planned PostgreSQL authority, macOS LaunchAgent.
- Baseline commands: compileall PASS; pytest collect PASS (960); Ruff FAIL (216 baseline errors); mypy FAIL.
- Dirty work / protected areas: pre-existing runtime `.beidou/*` is ignored and preserved; no tracked user changes before this run.
- Current runtime risk: supervisor state reported `trading_ready=false` with heartbeat/order-chain blockers while LaunchAgent remained running; the state is local evidence, not exchange truth.
- G0 decision: PASS_WITH_CONDITIONS. Continue only with fail-closed local slices; do not claim readiness.
