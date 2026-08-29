# Test strategy

| Risk / requirement | Test layer | Real or mock dependency | Environment | CI / release / post-release | Evidence |
|---|---|---|---|---|---|
| Mainnet/HTTP/unsafe host and bounded caps | unit + architecture | deterministic | local | blocking | `tests/unit/test_testnet_guard.py`, `tests/architecture/test_testnet_verification_boundaries.py` |
| HMAC signing and encoded request material | REST contract | captured request fixture | local | blocking | `tests/unit/test_binance_rest_client.py::test_signed_order_hmac_covers_the_exact_encoded_query` |
| exchangeInfo rule ingestion, `MIN_NOTIONAL`/`NOTIONAL`, and closed bars | adapter contract | deterministic transport fixture | local | blocking | `tests/unit/test_testnet_binance_contracts.py` |
| Dynamic pool membership and source hashes | unit + runtime integration | adapter-shaped fixture; no fixed symbol fallback | local | blocking | `tests/unit/test_strategy_data.py`, `tests/integration/test_testnet_verification_runtime.py` |
| StrategyKernel proposal/no-action trace | runtime integration | adapter-shaped market fixture | local | blocking | `tests/integration/test_testnet_verification_runtime.py` |
| Adaptive sizing monotonicity, caps, step/minimum rules | unit/property | deterministic inputs | local | blocking | `tests/unit/test_testnet_adaptive_sizing.py`, `tests/unit/test_testnet_binance_contracts.py` |
| PREPARED-before-write, restart, stable client id, UNKNOWN recovery | unit + integration fault injection | controlled adapter fake | local | blocking | `tests/unit/test_decision_trace.py`, `tests/integration/test_testnet_verification_runtime.py` |
| ACK identity, fill/position reconciliation, reduce-only close | integration contract | controlled adapter fake | local | blocking | `tests/integration/test_testnet_verification_runtime.py`, `tests/unit/test_testnet_binance_contracts.py` |
| Real Binance Testnet order/fill/close evidence | authorized E2E | real Binance Testnet/Demo | historical campaign + fresh read-only reconciliation | admission evidence with incident caveat | historical manifests/DecisionTrace; current account flat, no open orders, `0` unresolved |
| Current HEAD CI and full quality gates | system verification | repository toolchain | local/CI | release blocker | fresh local results below; full CI remains blocked |

## Historical local commands (not current-HEAD completion evidence)

- Full regression under coverage: `4251 passed, 126 warnings`.
- V4 target/architecture contracts: `35 passed, 91 warnings`.
- Full compile: `python -m compileall -q beidou_* apps scripts tools delivery/scripts` -> `PASS`.
- Ruff format: `650 files already formatted` -> `PASS`.
- Ruff lint: `All checks passed!` -> `PASS`.
- Configured package mypy command -> exit `0` -> `PASS`.
- Test-quality and hardcoded-value scans -> `PASS`.
- Package validator -> `PASS`.
- Write-capability registry rebuild plus independent oracle -> `{"issues": [], "status": "PASS"}`.
- Wheel/sdist build and installed-wheel CLI smoke -> `PASS`.
- Alpha V3 line/branch coverage gate -> `4236 passed`, `100.00%` -> `PASS`.
- Full repository coverage gate -> `4251 passed`, `98.04%`, required `100%` -> `FAIL`.
- Forbidden-pattern scan -> `PASS` after a precise allowlist entry for the retained legacy G5 runner's fixed loopback health read; its exchange requests remain adapter-governed.
- Bandit in an isolated security venv -> `0` issues -> `PASS`.
- Clean wheel runtime dependency audit -> `No known vulnerabilities found` -> `PASS`; the already-populated host audit found 26 vulnerabilities in unrelated installed packages and is not treated as project-clean evidence.

## Incident-remediation evidence (2026-08-29)

- Disabled and booted out `com.beidou.testnet-verify`; observed no verifier
  process for more than 70 seconds; durable kill switch remains present.
- Added a config gate requiring `--once` whenever `--confirm-testnet` is used.
- Removed the launchd verifier wrapper/plist and added an architecture
  regression test prohibiting their return.
- Fixed clean-checkout preflight test isolation and reduced the engine runtime
  coverage test from 65 seconds to about 5 seconds by eliminating event-loop
  busy waits.
- Fresh targeted regression: `67 passed`; legacy G5 gate regression:
  `17 passed`.
- Fresh signed GET reconciliation: account query success, one-way mode,
  no nonzero positions, no open orders, no open algo orders. The sole FILLED
  trace was linked to a later quantity-matched reduce-only CLOSED trace;
  unresolved count is `0`.

## Omissions and residual risk

Historical venue evidence exists, but the post-campaign KeepAlive incident
invalidates any claim that the 100 episodes were one deliberately bounded
campaign. Current full regression/100% coverage and clean governance checks
must be rerun after remediation. GitHub Actions remains unavailable because of
account billing/spending limits. E0-E6 remains `NOT_EVALUATED`; no Testnet fact
is profitability evidence.
