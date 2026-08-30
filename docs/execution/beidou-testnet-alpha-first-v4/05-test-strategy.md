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

## Superseded pre-remediation evidence

- The earlier `4251 passed` / `98.04%` full-coverage run is retained as a
  historical failure. It is superseded by the clean detached candidate run
  below and is not used as current release evidence.

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

## Clean detached candidate evidence

Validated implementation SHA:
`490ee9f697a791520a74a801331ad78f7ff81e24`.
Validated PR head:
`a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`.
The latter adds evidence documentation only. It was checked from detached
worktree `/tmp/beidou-pr12-a55881e`, using the repository development
environment through `python -m` so imports resolve to that exact checkout.

- Full repository suite under coverage on the PR head: `4612 passed`,
  `146 warnings`, `152.81s`; coverage report `45891/45891` statements,
  `0` missed, `100.00%` -> `PASS`.
- Alpha V3 line/branch gate on the PR head: `4612 passed`, `146 warnings`,
  `139.52s`; `3497/3497` statements and `1070/1070` branches -> `PASS`.
- Ruff format: `673 files already formatted` -> `PASS`.
- Ruff lint -> `PASS`.
- Configured package mypy command -> `PASS`.
- `compileall` for project packages, apps, scripts, tools and delivery scripts
  -> `PASS`.
- Write-capability registry rebuild plus independent oracle -> `PASS`, no
  issues.
- Test-quality, hardcoded-value and forbidden-pattern scans -> `PASS`.
- Package validator -> `PASS`.
- Bandit with project configuration -> `PASS`, no findings.
- `pip_audit -r requirements.lock` -> no known vulnerabilities, exit `0`;
  the editable `beidou` distribution is not a PyPI package and is explicitly
  reported as unauditable rather than vulnerability-free.

## GitHub and independent-verification evidence

- Draft PR: `https://github.com/yhwhhwhy001/beidou/pull/12`, reviewed head
  `a55881efe87c3ac44ab7ed2c5d644d6e2caed02a`.
- Actions run `33252017084`: `verify (3.12)` and `security` both ended in
  `FAILURE` after about two seconds with zero steps and no runner. Both check
  annotations state that recent account payments failed or the spending limit
  must be increased. This is not a test failure and is not a CI pass.
- A no-context independent reviewer was requested, but the reviewer service
  stopped at its usage limit before producing findings or a decision. No
  independent acceptance status is inferred.

## Omissions and residual risk

Historical venue evidence exists, but the post-campaign KeepAlive incident
invalidates any claim that the 100 episodes were one deliberately bounded
campaign. Local G7 is green for the exact PR head above; required GitHub jobs
and independent acceptance are still absent and therefore cannot be inferred
from local evidence. E0-E6 remains `NOT_EVALUATED`; no Testnet fact is
profitability evidence.
