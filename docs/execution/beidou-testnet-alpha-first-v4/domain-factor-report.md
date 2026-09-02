# Factor lifecycle report

- Factor ID/hypothesis/owner: V4 Alpha DAG and its eight registered factor
  nodes; research ownership remains required. This incident-remediation scope
  did not introduce or promote a new economic hypothesis.
- Data and computation: the safe CLI exposes only caller-bound local close
  data through `beidou alpha evaluate`. Import/help/status paths do not build
  the trading runtime or read credentials. Existing factor and Alpha contracts
  retain provenance, immutable forecast, ensemble and exposure-target checks.
- Robustness/IC/IR/correlation: implementation and regression coverage are
  testable locally, but no current out-of-sample, multi-regime IC/IR,
  correlation or transaction-cost evidence was produced by this remediation.
- Portfolio and regime tests: code-path tests cover the Alpha DAG, factor
  contracts, portfolio target and risk boundaries. The bounded 30/30 Demo
  campaign was explicitly an `EXECUTION_PROBE` with `alpha_evidence=false`;
  it cannot be used as portfolio or strategy-performance evidence.
- Promotion/retirement thresholds: no factor promotion is authorized. Economic
  Truth E0-E6 remains `NOT_EVALUATED`; promotion must stay fail-closed until
  independently reviewed thresholds and fresh evidence exist.
- Monitoring/drift: runtime health, input provenance and fail-closed contracts
  are covered by the repository test/health gates. Economic degradation and
  live factor drift are not established because no production Alpha operation
  is authorized.
- Status: `PASS_WITH_CONDITIONS` for local Alpha code integrity;
  `NOT_EVALUATED` for economic validity and `NOT_AUTHORIZED` for promotion or
  live use.

## 2026-09-01 Alpha/data revalidation

- The exact 22-module Alpha V3 gate passed all 4740 repository tests with
  3497/3497 statements and 1070/1070 branches covered.
- Research-data provenance now rejects non-USD-M archive paths, duplicate JSON
  keys, non-integer/out-of-range/future timestamps, oversized cross-source
  samples and manifest/content mismatch resealing. Focused boundary tests
  passed 62 cases; both changed data modules have 100% line coverage.
- These results establish code-path and fail-closed data-boundary integrity
  only. Current stored data/economic evidence was not promoted, no factor was
  activated, and Economic Truth E0-E6 remains `NOT_EVALUATED`.
- Status remains `PASS_WITH_CONDITIONS` for local Alpha integrity,
  `NOT_EVALUATED` for economic validity and `NOT_AUTHORIZED` for live use.
