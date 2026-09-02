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

## 2026-08-30 authorized local attempt and follow-up fix

- The user explicitly granted a one-attempt management waiver for GitHub CI
  and independent acceptance. The waiver authorized only one Demo Testnet
  `--once` process and did not convert G8/G9 to PASS or authorize Mainnet.
- Run `20260830T055549Z-cbb95f8fe939` used `--once`,
  `--confirm-testnet`, `--close-after-verify`, `max_notional=25`,
  `max_leverage=3`, and `max_instruments=1`. It ended
  `NOT_VERIFIABLE`: four restored symbols had no current-run market
  observation and BTC produced `STRATEGY_NO_ACTION`.
- Manifest facts: `real_testnet_write=false`,
  `real_testnet_write_attempted=false`,
  `real_testnet_write_outcome_unknown=false`, and unresolved trace count `0`.
  The durable kill switch was restored by the command trap. A signed GET-only
  post-check returned one-way mode, no nonzero positions, no regular/algo
  orders, and durable unresolved `0`.
- The attempt exposed a restore-path defect: a persisted five-symbol ACTIVE
  pool bypassed a new `max_instruments=1` runtime limit. A regression test
  failed with five ACTIVE symbols before the fix and passed after startup was
  changed to quarantine restored ACTIVE symbols outside the current bounded
  exchangeInfo universe.
- Fresh working-tree verification after the fix: `4613 passed`, `146 warnings`,
  `45891/45891` full-repository statements at `100%`; Alpha V3 `3497/3497`
  statements and `1070/1070` branches at `100%`. Ruff, mypy, compileall,
  governance scans, package validation, write-registry oracle, Bandit and
  dependency audit passed. The code-only diff SHA-256 is
  `deb09786fc3b7014994126be1b90a4c1f1e537bbda0f52cbe7697fe00b8120ff`.

## 2026-08-30 reauthorized episode after completion-status fix

- A second aggregation defect was reproduced before external execution: four
  flat quarantined symbols returned `CLOSED` and incorrectly promoted an
  ACTIVE `STRATEGY_NO_ACTION` result to `EPISODE_COMPLETED`.
- The regression failed with `EPISODE_COMPLETED` before the fix. Campaign
  completion is now derived only from ACTIVE verification episodes;
  quarantined reduce-only recovery remains recorded but cannot prove a
  verification episode.
- Fresh local gates after this fix: `4614 passed`, `146 warnings`,
  `45891/45891` repository statements at `100%`; Alpha V3 `3497/3497`
  statements and `1070/1070` branches at `100%`. Ruff, mypy, compileall,
  governance scans, package validation, write-registry oracle, Bandit and
  dependency audit passed.
- The newly authorized single Demo run
  `20260830T064800Z-cbb95f8fe939` correctly ended `NOT_VERIFIABLE`: four
  `QUARANTINED_ALREADY_FLAT` safety results plus one ACTIVE
  `STRATEGY_NO_ACTION`. It attempted no exchange write and was not retried.
- Signed post-reconciliation returned ONE_WAY, flat, regular/algo open orders
  `0`, durable unresolved `0`; the kill switch was restored.

## Bounded execution-probe soak local acceptance

- TDD focused gate: `81 passed` across soak configuration/kernel/runner,
  Testnet guard/CLI, actual verifier fake-adapter integration, and architecture
  boundaries.
- Architecture gate: `295 passed`; registry rebuild and independent oracle
  returned `PASS` with no issues.
- Full repository line-coverage gate: `4648 passed`, `2595 warnings`,
  `45895/45895` statements, `100.00%`.
- Alpha V3 line/branch gate: `4648 passed`, `3497/3497` statements and
  `1070/1070` branches, `100.00%`. This remains Alpha-code regression
  evidence only; the execution probe sets `alpha_evidence=false`.
- Ruff lint/format, Mypy (`330` source files), compileall, test-quality,
  hardcoded/forbidden-pattern scans, package validator, write-registry oracle,
  Bandit, and `pip-audit -r requirements.lock` passed. `beidou` remains a
  local non-PyPI distribution and is reported unauditable, not vulnerability
  free.
- A local offline 30-episode acceptance run used the actual campaign runner,
  actual `VerificationRuntime`, and `FakeTestnetAdapter`: `30` completed
  episodes, `30` opening calls, `30` reduce-only close calls, `60` unique
  traces, final position `0`, simulated elapsed `3480` seconds, final kill
  switch engaged, and `network_calls=0`.
- No current Testnet credential value was read or printed. No real Binance
  Testnet, Mainnet, GitHub, commit, push, or merge action was performed.

## 100/500 USDT reauthorization validation and campaign

- TDD red evidence: the revised default-bound test failed because the runner
  still returned `10/25`; after changing only the authorized caps, focused
  soak/architecture tests passed `28`, affected Testnet tests passed `84`, and
  write-registry architecture/oracle tests passed `234`.
- Full local gate: `4648 passed`, `45895/45895` statements, 100% coverage.
  Ruff, format, Mypy and compileall passed.
- Offline actual-composition campaign at 100/500: 30 opens, 30 reduce-only
  closes, 60 unique traces, final position 0, simulated elapsed 3480 seconds,
  kill switch engaged, network calls 0.
- Read-only Demo preflight: BTCUSDT TRADING; venue minNotional 50; at price
  78120.80 and step 0.0001, minimum compliant order was 0.0007 BTC / 54.684560
  USDT; leverage readback 2x; ONE_WAY, flat, no regular/algo orders and zero
  unresolved traces.
- Real campaign `20260830T105901Z-eb1a3ebe4be7` completed 28 fully reconciled
  episodes and 56 order traces. Every episode opened 0.0012 BTC and closed the
  same quantity through the owned reduce-only path; every between-episode
  reconciliation was flat/order-free/unresolved-zero.
- The runner stopped with `TIME_BUDGET_EXHAUSTED` after 3511.43 seconds because
  29 fixed 120-second post-episode sleeps leave only 120 seconds of the
  3600-second budget for all 30 real execution/reconciliation cycles. No
  UNKNOWN occurred and episodes 29-30 were not started.
- Fresh signed post-check at `2026-08-30T11:58:28Z`: writes disabled,
  kill switch engaged, ONE_WAY, flat, regular/algo orders 0, unresolved 0.

## Fixed-cadence remediation

- Red regression: with each episode consuming 8 seconds, the post-episode
  sleep implementation returned `STOPPED` instead of completing 30 rounds.
- Green behavior: the runner now schedules absolute start-to-start deadlines;
  each simulated 8-second cycle sleeps 112 seconds, starts at 120-second
  cadence, and completes 30 episodes in 3488 seconds.
- Actual-composition offline verification used `VerificationRuntime` and
  `FakeTestnetAdapter`: 30 opens, 30 reduce-only closes, 60 unique traces,
  final position 0, 29 sleeps of 112 seconds, network calls 0, and final kill
  switch engaged.
- Fresh gates: affected tests `85 passed`; full repository `4649 passed`,
  `45895/45895` statements, 100%; Ruff, format, Mypy, compileall and write
  registry independent oracle PASS.

## 2026-09-01 final health-remediation verification

- The first full-repository rerun passed all `4740` tests but correctly failed
  the 100% coverage gate at `46450/46455` statements. The five uncovered
  lines were optional argument-forwarding branches in
  `beidou_launcher.alpha_first_adapter`; the test now verifies port, startup
  timeout, monitor interval, self-heal and restart-limit forwarding without
  starting a runtime.
- Fresh final full-repository gate: `4740 passed`, `46455/46455` statements,
  `100.00%` line coverage. Fresh Alpha V3 exact-module gate: `4740 passed`,
  `3497/3497` statements and `1070/1070` branches, `100.00%`.
- Ruff lint/format checked 656 files; Mypy passed 330 source files; compileall,
  test-quality, hardcoded/forbidden-pattern scans, package validator, primary
  write-registry validation, independent registry oracle, Bandit and
  `pip-audit -r requirements.lock` passed. The local `beidou` distribution is
  not on PyPI and remains explicitly unaudited by pip-audit.
- The source/test/registry digest was stable at
  `e439b83340a9ba033118a4f7fec1460313d745aefbfe369aca182679a0a7caa7`
  before and after both long coverage gates. Pytest emitted 146 test-process
  resource/deprecation warnings; they did not fail a gate and are not runtime
  or economic evidence.
- `beidou doctor --mode safety_only` returned 0. All Beidou LaunchAgents were
  unloaded, no verifier/soak/runtime process was active, and the durable kill
  switch remained engaged. `beidou status` remained `STALE` because it reads a
  stopped 2026-08-28 snapshot; its old mismatch is not current account truth.
- A fresh signed GET-only Demo reconciliation could not complete: credentials
  were present, writes were disabled, the first signed request timed out, and
  the public Demo server-time endpoint also timed out. Current account state is
  therefore `UNKNOWN`, not flat. No order, cancellation, leverage change,
  deployment, commit or push occurred.
