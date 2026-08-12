# Task plan

| Task | Requirement IDs | Dependencies | Files | Test | Evidence | Status |
|---|---|---|---|---|---|---|
| Establish fresh quality/coverage baseline | P2-Q-001..005 | G0 | repository | full scanners and coverage JSON | baseline outputs | COMPLETE |
| Prove Testnet endpoint and credential presence safely | P2-X-001..003 | G0 | config/runtime only | read-only endpoint probes | redacted execution report | COMPLETE |
| Remove static, test-quality and swallowed-exception debt | P2-Q-001..003 | baseline | production/tests | focused plus full gates | implementation log | COMPLETE |
| Add risk-based behavior tests for uncovered code | P2-Q-004..005 | coverage ranking | tests | red-green-focused/full coverage | coverage report | PARTIAL: material gain, 85% unmet |
| Review changes and run final verification | all | implementation complete | changed files | full gates and read-only exchange replay | review/final summary | COMPLETE |
