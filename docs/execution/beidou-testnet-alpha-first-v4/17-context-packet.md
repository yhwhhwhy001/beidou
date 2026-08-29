# Context packet

## Objective

Complete V4 implementation remediation, compare it against the uploaded
execution package, verify safely, and ship only when all required gates pass.

## Non-goals

Mainnet, production deployment, secret disclosure, synthetic alpha promotion,
or another Testnet campaign while HOLD is active.

## Status

In progress at clean system verification. Testnet account risk is reconciled
to zero; release remains blocked.

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
| Full candidate regression | clean detached `490ee9f697a791520a74a801331ad78f7ff81e24`: `4612 passed`, `45891/45891`, `100.00%` |
| Registry digest drift | rebuilt registry and independent oracle both pass |
| GitHub required jobs unavailable | rerun after billing/spending limit recovery |
| Independent review pending | review final diff and raw package criteria |

## Next actions

1. Commit and push the documentation convergence; open a PR so required CI
   runs on the remediation head.
2. Obtain independent acceptance/release review.
3. Merge only after all required checks pass.
