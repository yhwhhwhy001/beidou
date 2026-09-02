# Architecture and migration review

## Removal inventory

| Candidate | Consumer evidence | Replacement or retained boundary | Decision |
|---|---|---|---|
| `beidou_bootstrap.dev._sync_opening_balance` | only its definition remained; architecture already prohibited it in startup | governed opening projection and reconciliation remain in the canonical store/runtime path | Remove |
| `beidou_safety/recovery/exit_recovery.py` | no source, test, entrypoint, or documentation consumer; registry digest only | wired and tested `beidou_safety.execution.recovery.RecoveryEngine` | Remove file |
| FastAPI `CORSMiddleware` import and matching fake modules | Vulture 90% unused; no middleware registration | FastAPI routes remain unchanged | Remove |
| `scripts.certify_72h.verify_prerequisites` | definition only | retired script keeps `start`/`finalize` fail-closed and `status` read-only | Remove |

## Compatibility, data, and rollback

- The removed Python helper was private, the removed recovery module had no internal consumers or package export, and all public fail-closed entrypoints remain.
- No schema, migration, persisted record, network call, deployment, or trading action is performed. Removing `_sync_opening_balance` eliminates an unused direct PostgreSQL mutation path.
- Rollback is source-only: restore the deleted definitions/file and rebuild `config/write-capability-registry.json`. Restoring them is not recommended without a new consumer and behavior contract.
- The registry is mechanically rebuilt from the live tree and must pass both the primary and independent architecture oracles.

## Retained legacy candidates

- Historical certification/production governance, retired research validation, CLI compatibility wrappers, protocol methods, and dynamic framework hooks remain because they carry fail-closed/audit behavior or public signatures.
- `beidou_research/features/canonical_feature_engine.py` remains because local planning identifies it as a future convergence target, not a proven obsolete replacement.
- High-confidence Vulture parameter findings are not deleted when they are required by protocol, callback, signal-handler, or compatibility signatures. Behavioral defects in those APIs require separate implementation authority and tests.

## Decision

`APPROVED_WITH_CONDITIONS`: removals are zero-caller and reversible; final approval depends on focused tests, registry oracles, and a full-suite comparison against the dirty baseline.
