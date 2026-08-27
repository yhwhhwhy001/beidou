# Package architecture and G2 decision

## Source decision

The frozen big-bang proposal was rejected in `../2026-08-25-alpha-first-architecture-review/03-architecture.md`. The user has now authorized packaging its optimized replacement, `O2 Vertical-Slice Strangler`.

## Approved package model

```text
safe CLI facade / composition roots
├── alpha_app   -> alpha + neutral contracts + platform abstractions
├── paper_app   -> alpha + simulator + neutral contracts
└── testnet_app -> alpha + execution + safety_min + neutral contracts

alpha, execution, safety_min and simulator do not import each other's concrete implementations.
```

The package implements the architecture as a sequence of proofs, not directory moves:

1. immutable clean baseline and dependency oracle;
2. safe facade and neutral contracts;
3. resumable ExperimentRun vertical slice;
4. sealed economic evidence and portfolio comparison;
5. canonical Paper parity and attribution;
6. shadow extraction of Execution Truth;
7. independent Testnet safety and Mainnet-absent artifact gates;
8. cutover and legacy deletion only after zero-caller and rollback proof.

## Conditions carried into every task

- Offline Research must remain available when launcher/safety/exchange are unavailable.
- Candidate failure is local; runtime-integrity UNKNOWN pauses risk increase but does not stop pure Research.
- R2-R5 move to Alpha/portfolio lifecycle. Margin, liquidation, protection/equivalent exit, account capability, duplicate/UNKNOWN and reconciliation remain hard Testnet integrity boundaries.
- Mainnet absence is proven on the built artifact, not inferred from configuration intent.
- Checkpoint/resume preserves the tested candidate family and multiple-testing denominator; restart cannot become a selection mechanism.
- No task can lower tests/coverage, convert UNKNOWN to empty/PASS, use synthetic results as economic evidence, or claim runtime authority.

## G2 decision

`APPROVED_WITH_CONDITIONS` for development-package task planning. Conditions are encoded as `AF-T00`, negative acceptance criteria and specialist gates. It is not approval to implement, activate Testnet or delete legacy code.
