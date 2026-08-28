# Final summary (updated 2026-08-29 after V4 gap-fix campaign)

## Outcome and validated scope

The V4 package audit found 15 divergence items. All in-repo items are fixed
and the real Testnet campaign was re-run through the sole verification entry:

- **B1 fixed**: demo-fapi `/fapi/v1/ticker/24hr` omits `bidPrice/askPrice`;
  the verifier now derives spread from depth top-of-book (live-verified).
- **B2 fixed**: venue leverage readback must strictly equal the request;
  mismatch now blocks the risk-increasing order (engine + verifier).
- **A3 fixed**: deprecated sizing helpers marked DEPRECATED; architecture
  test forbids the verifier from referencing them.
- **C6 fixed**: `signed_capability` marked `FUTURE_MAINNET` (code marker);
  frozen-modules list published (`16-frozen-modules.md`).
- **C4 implemented**: E0–E6 Economic Truth gates
  (`beidou_research/economic_truth.py`) — fail-closed, prerequisite-chained,
  surfaced in every verifier manifest; all gates `NOT_EVALUATED` (no research
  evidence supplied, by design).
- **C5 implemented**: AC-STR-004/005 parity tests (backtest/paper MATCH,
  frozen-input proposal hash reproducibility).
- **Campaign bug fixed**: write-guard quantity identity is numeric
  (`"0.100" == "0.1"`); canonical request hash normalizes quantity strings.
- **Campaign bug fixed**: demo accounts report `canWithdraw=true`; the
  verifier requires trading capability only and records withdrawal capability
  as an audited fact.
- **Legacy engine retired from the default Testnet path**: its leftover
  positions were closed reduce-only; the verifier is the sole operational
  Testnet entry. The engine remains available for the legacy G5
  certification path only.

## Campaign evidence (live demo-fapi, 2026-08-29)

- 100 completed decision episodes (terminal traces).
- 8 real fill→close chains (BCHUSDT, LTCUSDT) with leverage set/readback,
  ACK, position readback, reconciliation, and complete DecisionTraces.
- 11 stale UNKNOWN traces adjudicated against venue facts (venue confirmed
  absent → FAILED); 0 unresolved traces at campaign end.
- Evidence: `evidence/testnet-verification/20260828T20*Z-*/manifest.json`,
  `.beidou/testnet_verification/decision-trace.jsonl` (redacted facts only).

## Requirement and gate decisions

- Local P0 contracts: PASS (see `12-acceptance-report.md`).
- Local gates: ruff format/lint, compileall, mypy, full pytest, and 100%
  full-repository coverage — all green locally.
- Registry independent oracle: re-run after source changes.
- GitHub Actions: still BLOCKED by account billing/spending limits (runner
  allocation, run `33195803547`); external to this repository.
- Economic Truth E0–E6: gates implemented, `NOT_EVALUATED` — Testnet
  execution facts are never promoted to alpha claims.
- Mainnet/production/real-money authority: prohibited or not authorized.

## Decision: CONDITIONAL PASS (Testnet execution chain)

`Testnet READY` / `Completed` / `Alpha VERIFIED` are NOT claimed. Admission
depends on GitHub runner availability and independent human review.

## Evidence index

- Acceptance matrix: `12-acceptance-report.md`
- Frozen modules: `16-frozen-modules.md`
- Runbook: `10-runbook.md`
- Architecture diagram: `11-architecture-diagram.md`
- New tests: `tests/unit/test_coverage_gap_*.py`,
  `tests/unit/test_testnet_verify_account.py`,
  `tests/unit/test_testnet_verify_features.py`,
  `tests/unit/test_economic_truth_gates.py`, guard/leverage/kernel-parity
  additions.
