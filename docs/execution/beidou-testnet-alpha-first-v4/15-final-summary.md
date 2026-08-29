# Final local-verification summary (2026-08-29 incident remediation)

## Outcome so far

The V4 implementation produced genuine Binance Demo execution evidence, but a
later launchd KeepAlive deployment turned the verifier into an unbounded loop.
The prior `CONDITIONAL PASS` is withdrawn. Current state is `Testnet HOLD`.

Completed remediation:

- stopped, booted out, and disabled the verifier LaunchAgent;
- retained the durable kill switch;
- removed the verifier launchd wrapper/plist from the repository;
- require `--once` for every confirmed Testnet write;
- added architecture regression coverage against daemon reintroduction;
- fixed clean-checkout preflight test isolation;
- fixed event-loop busy waits in engine coverage tests;
- made manifest write facts current-run scoped and kill-switch aware;
- linked flat FILLED traces only to later matching CLOSED reduce-only traces;
- restored the frozen legacy launcher's G5 certificate gate;
- performed fresh signed GET reconciliation: flat account, no open orders,
  no open algo orders, unresolved=`0`.
- validated code SHA `490ee9f697a791520a74a801331ad78f7ff81e24`
  in a clean detached worktree: `4612 passed`, `45891/45891` statements,
  `100.00%` coverage;
- passed Ruff format/lint, mypy, compileall, registry independent oracle,
  test-quality/hardcoded/forbidden-pattern scans, package validator, Bandit
  and third-party dependency audit.

## Historical evidence boundary

Historical ACK/fill/leverage/close traces remain useful execution evidence.
They do not prove that the overall 100-episode activity was a deliberately
bounded campaign, and they do not prove alpha or profitability.

## Remaining external gates

- obtain independent acceptance/release review;
- obtain successful required GitHub Actions after the billing/spending-limit
  blocker is removed;
- merge to `main` only after those gates are green;
- only then consider a separately bounded, explicitly authorized Testnet
  campaign. Keep the kill switch engaged until that decision point.

## Decision

Local G7: `PASS` for the exact validated code SHA.
G8/G9: `BLOCKED` for release, merge and any new Testnet campaign.
`NOT_EVALUATED` for E0-E6 / alpha.
`PROHIBITED` for Mainnet and production.
