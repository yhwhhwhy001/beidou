# Code review

- Reviewer independence: self-review only; no independent reviewer was requested or available.
- Version/diff: working tree based on `818518e8d6a839d014689f30ad66f44d794e4b60`; review scope is limited to the cleanup files listed below.

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P1 | `beidou_bootstrap/dev.py` | unused helper could overwrite an authoritative opening projection directly in PostgreSQL and swallowed all failures | zero callers; architecture prohibited startup use | delete helper and add absence guard | Resolved |
| P1 | `beidou_safety/recovery/exit_recovery.py` | parallel in-memory manager claimed durable exit recovery but had no persistence or consumers | repository-wide reference scan; canonical recovery is wired in supervisor | delete module and guard single authority | Resolved |
| P3 | `beidou_control/api.py` and tests | CORS import and fake module were never used | Vulture 90% unused plus reference scan | remove import/test scaffolding | Resolved |
| P3 | `scripts/certify_72h.py` | retired prerequisite function had no caller | reference scan and Vulture | delete function; retain fail-closed entrypoints | Resolved |
| P1 | concurrent dirty tree | four write-registry governance/digest failures remain in concurrently edited Alpha research files | final full pytest evidence | do not claim repository PASS or repair another run's governance decisions | Open |

## Decision

`PASS_WITH_CONDITIONS`: cleanup diff removes 212 net lines, behavior-focused tests pass, and the removed paths have zero callers. Repository-wide completion remains blocked by four concurrent write-registry failures.
