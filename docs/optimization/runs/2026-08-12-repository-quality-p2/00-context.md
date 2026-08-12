# Context

- Run ID: `2026-08-12-repository-quality-p2`
- Repository/version: `/Users/maguannan/beidou` at `9c03be4fcd07fb5b5b0e6001ceb10752899ed911`, branch `main`
- Applicable instructions: no repository `AGENTS.md`; fail-closed quantitative-trading safety contract; preserve user work and evidence
- User authority: local source/test/document changes and Testnet exchange connectivity validation
- External authority boundary: read-only Testnet requests are authorized; order submit/cancel/replace/close, leverage, margin, balance, credential, permission, risk-limit and protection mutations are not authorized; Mainnet and real funds remain prohibited
- Git/deployment authority: no new commit, push, restart or deployment authorization in this turn
- Technical stack: Python 3.14 local venv, pytest, coverage.py, Ruff, mypy, SQLite/PostgreSQL contracts, Binance USD-M adapter
- Dirty work / protected areas: clean worktree at start; preserve append-only trading evidence and current `UNKNOWN` semantics
- Risks and unknowns: configured repository coverage target is 85%; Testnet credentials/endpoint/permissions are not yet proven; real PostgreSQL and live WebSocket state may be unavailable
- Selected specialists: test strategy/coverage, TDD, incremental implementation, code review, security hardening, exchange execution validation, completion verification
- Omitted specialists: deployment/production validation because restart/deploy/Mainnet are not authorized; performance optimization because no measured performance regression is in scope
- Stop conditions: Mainnet endpoint or account identity, secret exposure risk, ambiguous environment, need for order/account mutation, user worktree conflict, or inability to preserve fail-closed controls
- G0 decision: READY for local remediation and read-only Testnet validation
