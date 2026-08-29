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
| PKG-07 lifecycle/reconciliation | same-id recovery; later close linkage; signed flat account readback | runtime integration + fresh signed GET | PASS for current account risk |
| PKG-08 freeze/isolation | verifier bypasses frozen modules; legacy G5 gate restored | architecture + legacy preflight tests | PASS locally |
| PKG-09 CI/economic truth | E0-E6 remains separate; GitHub runner cannot allocate | economic truth tests; CI run evidence | BLOCKED |
| PKG-10 docs/default entry | README/runbook/acceptance/release reports incident-aware | doc/CLI/architecture tests | IN_PROGRESS until final candidate SHA/results |

All AC-TN requirement-level decisions are recorded in
`12-acceptance-report.md`. Historical Testnet facts are not promoted to a
new bounded-campaign claim.
