# Requirements

## Goal

Implement the actionable P0 order-flow remediations from the 2026-08-12 review without enabling trading or claiming runtime readiness.

## Requirements and acceptance criteria

| ID | Requirement | Observable acceptance criterion |
|---|---|---|
| OFR-001 | All environments share fail-closed safety semantics | Testnet cannot bypass NO_NEW_RISK, reconciliation, protection, user-stream, execution-fact, or supervisor blockers |
| OFR-002 | An approved intent cannot be changed at the send boundary | Testnet uses the normal execution plan and slice validation; no direct MARKET substitution |
| OFR-003 | Approval nonce is consumed exactly at final send verification | Final verification uses the signer contract and a replay is rejected |
| OFR-004 | REST method and ambiguous-write behavior are correct | PUT reaches the transport as PUT; an ambiguous order 5xx is UNKNOWN and is not blindly retried |
| OFR-005 | Venue-rule quantization cannot increase approved risk | Decimal step/tick quantization is side-aware, never increases approved quantity, and is revalidated before send |
| OFR-006 | Execution algorithms preserve typed contracts and reduction intent | MarketableLimit emits Money; emergency execution cannot silently convert risk-increasing orders into reduce-only behavior |
| OFR-007 | Protection/recovery facts remain conservative | Pending/unowned protection is not counted as active coverage and UNKNOWN facts are not aged or deleted into safety |
| OFR-008 | Regression gates detect semantic bypasses | Tests inspect behavior/AST patterns used by current code and fail on environment-specific safety downgrades |

## Non-goals

- No exchange/Testnet execution, cancellation, leverage mutation, credential use, deployment, restart, or production verification.
- No profitability claims or strategy parameter tuning.
- No automatic deletion of historical execution evidence.
- Long-horizon BD-T09/BD-T10/BD-T11 database migrations are not declared complete without real PostgreSQL integration evidence.

## Gate decisions before development

- G1: APPROVED by the user's request to execute the review remediation.
- G2: APPROVED_WITH_CONDITIONS: retain the existing single-fact-chain architecture; make bounded fail-closed changes first; database consolidation remains a later gated slice.
- G3: READY: implement OFR-001 through OFR-008 in dependency order with red-green regression evidence.
