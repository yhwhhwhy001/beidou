# Bounded Testnet soak campaign task plan

| Task | Depends on | Deliverable | Status |
|---|---|---|---|
| SOAK-T01 | authorization | Requirements, traceability and architecture with explicit local-only boundary | COMPLETE |
| SOAK-T02 | SOAK-T01 | Red tests for configuration, CLI, kernel, campaign state machine, reconciliation and identity | COMPLETE |
| SOAK-T03 | SOAK-T02 | Configurable verifier authority/identity hooks with unchanged defaults | COMPLETE |
| SOAK-T04 | SOAK-T03 | `apps.testnet_soak` config, probe kernel, foreground runner and CLI | COMPLETE |
| SOAK-T05 | SOAK-T04 | Offline fake-adapter integration proving open/fill/reduce-only-close behavior | COMPLETE |
| SOAK-T06 | SOAK-T05 | Write registry rebuild, architecture/governance/security gates | COMPLETE |
| SOAK-T07 | SOAK-T06 | Full local test and coverage verification; evidence and readiness update | COMPLETE |
| SOAK-T08 | SOAK-T07 | Stop with kill switch engaged and request separate real-campaign authorization | COMPLETE |
