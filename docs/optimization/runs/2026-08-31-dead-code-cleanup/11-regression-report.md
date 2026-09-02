# Regression report

| Surface | Before | After cleanup | Decision |
|---|---|---|---|
| Full pytest | `4669 passed, 6 failed` | final observed tree: `4690 passed, 4 failed` | FAIL at repository scope; concurrent changes altered the failure set |
| Cleanup-focused tests | not applicable | final observed tree: `39 passed` | PASS |
| Recovery plus write-registry architecture | four registry failures in baseline | `233 passed` | PASS |
| Ruff lint | not recorded before | full tree PASS; changed files PASS | PASS |
| Ruff format | two unrelated existing files fail full-tree check | all changed files formatted | PASS_WITH_CONDITIONS |
| Mypy | touched script initially exposed missing annotations | touched packages/script PASS after annotations | PASS |

## Final full-suite failures

1. `tests/architecture/test_write_capability_registry.py::test_write_capability_registry_is_complete_and_valid`
2. `tests/architecture/test_write_capability_registry.py::test_registry_read_only_dry_run_is_executable`
3. `tests/architecture/test_write_registry_independent_oracle.py::test_independent_oracle_accepts_current_registry`
4. `tests/architecture/test_write_registry_independent_oracle.py::test_independent_oracle_cli_is_a_separate_executable_gate`

The final failures identify unreviewed terminal-call governance and source-digest drift in concurrently edited `beidou_research/data/*` and `beidou_research/economic_truth.py`. They do not reference the cleanup files. The two earlier baseline failures were changed by concurrent work and are not attributed to this cleanup. Full repository PASS is not claimed.

## Evidence

- Baseline: `artifacts/evidence/SYS-CLEANUP-20260831/20260830T205451061392Z.manifest.json`
- Focused cleanup: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T040438314889Z.manifest.json`
- Recovery and registry: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T040737954456Z.manifest.json`
- First full rerun: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T040919393384Z.manifest.json`
- Final cleanup-focused rerun: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T041814073786Z.manifest.json`
- Final full observed-tree rerun: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T041820954547Z.manifest.json`
- Ruff/mypy changed-scope checks: `artifacts/evidence/SYS-CLEANUP-20260831/20260831T041404879192Z.manifest.json`, `20260831T041405086550Z.manifest.json`, and `20260831T041405187651Z.manifest.json`
