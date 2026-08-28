# Release readiness

Decision: `BLOCKED`

Candidate branch: `codex/v4-audit-remediation-campaign`.
Mainnet remains `PROHIBITED`.

Fresh local evidence on 2026-08-29:

- Compile, Ruff lint, full configured mypy, package validation, repository
  governance scans, independent write-registry oracle, and Bandit: `PASS`.
- Full regression under coverage: `4251 passed`.
- Configured full-repository coverage: `98.04%`; required `100%`: `FAIL`.
- Real Binance Testnet campaign: `BLOCKED`; API key, API secret, and dedicated
  account identifier are absent from the execution environment.
- GitHub required jobs for the candidate commit: not yet available.
- Rollback: do not merge or enable campaign writes while required jobs or
  account reconciliation are UNKNOWN; use the durable kill switch to deny new
  risk and retain only owned cancel/reduce-only recovery.

This is not `GREEN_LIGHT_TO_SHIP`. A real Testnet episode cannot repair a
failed code gate, and a green code gate cannot substitute for account custody,
venue ACK/fill/position evidence, or zero unresolved reconciliation.
