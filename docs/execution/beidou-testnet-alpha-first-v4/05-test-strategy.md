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
| Real Binance Testnet order/fill/close evidence | authorized E2E | real Binance Testnet/Demo | not run in this turn | admission blocker | no evidence; status `NOT_VERIFIABLE` |
| Current HEAD CI and full quality gates | system verification | repository toolchain | local/CI | release blocker | fresh local results below; full CI remains blocked |

## Executed local commands

- Full regression: `python -m pytest tests/ -q` -> `4236 passed, 2090 warnings`.
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
- Full repository coverage gate -> `4236 passed`, `98.10%`, required `100%` -> `FAIL`.
- Forbidden-pattern scan -> `PASS` after a precise allowlist entry for the retained legacy G5 runner's fixed loopback health read; its exchange requests remain adapter-governed.
- Bandit in an isolated security venv -> `0` issues -> `PASS`.
- Clean wheel runtime dependency audit -> `No known vulnerabilities found` -> `PASS`; the already-populated host audit found 26 vulnerabilities in unrelated installed packages and is not treated as project-clean evidence.

## Omissions and residual risk

The local suite does not prove venue behavior, credentials, order matching,
network timeout semantics at Binance, or economic alpha. No Testnet write was
performed because the user request was handled as local development authority
only and no `--confirm-testnet` campaign was authorized. Testnet admission,
30-episode stability, CI status, and E0-E6 Economic Truth remain
`NOT_VERIFIABLE`/`NOT_EVALUATED` until their required evidence exists. The
full repository 100% coverage is not green, and no GitHub CI run was
established in this turn.
