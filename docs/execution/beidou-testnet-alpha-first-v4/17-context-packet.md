# Context packet

## Objective

Complete V4 implementation remediation, compare it against the uploaded
execution package, verify safely, and ship only when all required gates pass.

## Non-goals

Mainnet, production deployment, secret disclosure, synthetic alpha promotion,
or another Testnet campaign while HOLD is active.

## Status

Local clean system verification is complete on validated candidate head
`a55881e`. Testnet account risk is reconciled to zero; release remains blocked
at G8/G9.

## Decisions

| Decision | Reason | Reversible? |
|---|---|---|
| Keep kill switch engaged | Prevent new risk during remediation | Yes, only after a separate readiness decision |
| Prohibit confirmed writes without `--once` | Package requires a bounded campaign | Yes, but changing it requires a new approved contract |
| Restore legacy G5 preflight | Freeze/isolate does not authorize weakening legacy governance | Yes |
| Preserve historical evidence | Incident facts must remain auditable | No rewrite; append-only |

## Evidence

| Evidence | Location |
|---|---|
| Package SHA and baseline | `00-context.md` |
| Requirement mapping | `02-traceability.md` |
| Incident/root cause | `07-debugging-log.md` |
| Acceptance and blockers | `12-acceptance-report.md`, `13-release-readiness.md` |
| Durable traces | `.beidou/testnet_verification/decision-trace.jsonl` |

## Files touched

Verifier config/runtime, legacy preflight, incident regression tests, README,
runbook, and V4 execution evidence documents. Untracked historical campaign
manifests are preserved and not staged.

## Open risks

| Risk | Next check |
|---|---|
| Full candidate regression | clean detached candidate `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`: full repository and Alpha line/branch gates both `100%` |
| Registry digest drift | rebuilt registry and independent oracle both pass |
| GitHub required jobs unavailable | PR #12 run `33252017084` failed before steps; restore billing/spending runner eligibility |
| Independent review pending | no-context reviewer reached usage limit without a result; obtain a completed review |

## Next actions

1. Restore GitHub Actions runner eligibility and rerun required PR jobs.
2. Obtain independent acceptance/release review.
3. Merge only after all required checks pass; then perform a separate
   readiness decision before removing the kill switch or starting the
   requested bounded Testnet campaign.
