# V4 remediation traceability

| Package scope | Implementation / evidence | Tests | Status |
|---|---|---|---|
| PKG-00 architecture boundary | verifier import boundary; daemon launcher prohibition | `tests/architecture/test_testnet_verification_boundaries.py` | PASS locally |
| PKG-01 write authority | Testnet guard, durable kill switch, confirmed-write `--once` requirement | guard + CLI tests | PASS locally |
| PKG-02 sole runtime | `apps.testnet_verify`; launchd wrapper/plist removed | CLI/runtime/architecture tests | PASS locally |
| PKG-03 durable truth | DecisionTrace append/fsync; current-run manifest write facts | DecisionTrace + runtime integration | PASS locally |
| PKG-04 pool | live historical pool facts and active-only gate | pool/runtime integration | PASS_WITH_INCIDENT_CAVEAT |
| PKG-05 sizing/leverage | canonical sizing, request identity, leverage readback | sizing/guard/adapter contracts | PASS_WITH_INCIDENT_CAVEAT |
| PKG-06 kernel/components | component output/VETO/proposal traces | kernel/runtime integration | PASS_WITH_INCIDENT_CAVEAT |
| PKG-07 lifecycle/reconciliation | same-id recovery; later close linkage; 2026-08-30 signed flat account readback | runtime integration + signed GET: one-way, flat, no regular/algo orders, unresolved `0` | PASS for current account risk |
| PKG-08 freeze/isolation | verifier bypasses frozen modules; legacy G5 gate restored | architecture + legacy preflight tests | PASS locally |
| PKG-09 CI/economic truth | E0-E6 remains separate; clean local G7 passed at PR head `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`; PR #12 run `33252017084` failed before all steps because no runner was allocated; independent reviewer produced no decision | two detached full-suite coverage runs; static/security evidence; GitHub annotations | BLOCKED |
| PKG-10 docs/default entry | README/runbook/acceptance/release reports are incident-aware and identify the validated code SHA | doc/CLI/architecture tests; package validator | PASS locally / G8 dependent |

All AC-TN requirement-level decisions are recorded in
`12-acceptance-report.md`. Historical Testnet facts are not promoted to a
new bounded-campaign claim.
