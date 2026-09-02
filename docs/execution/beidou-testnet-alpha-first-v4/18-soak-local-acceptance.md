# Bounded execution-probe soak local acceptance

Date: 2026-08-30

## Decision

`PASS_LOCAL_ONLY`

The real campaign was not run. No Testnet credential was consumed, no network
request was made, and the repository-global durable kill switch remained
engaged throughout this development phase.

## Exact accepted bounds

- mode: `EXECUTION_PROBE`; `alpha_evidence=false`
- episodes: `30`
- wall-clock budget: `3600` seconds
- interval: `120` seconds; 29 sleeps, none after the final episode
- target/max opening order notional: `10` USDT
- absolute account-exposure ceiling: `25` USDT
- leverage: at most `3x`
- instruments: exactly one canonical symbol
- close: reduce-only after each verified fill
- continue gate: signed ONE_WAY, all positions flat, regular/algo orders zero,
  durable unresolved traces zero
- stop gate: any UNKNOWN, non-CLOSED child fact, duplicate identity, failed
  close/reconciliation, timeout, cancellation, or exception

## Evidence

- focused soak and affected verifier/security tests: `81 passed`
- architecture: `295 passed`
- full repository: `4648 passed`; `45895/45895` statements; 100%
- Alpha V3: `4648 passed`; `3497/3497` statements and `1070/1070` branches;
  100% line and branch coverage
- offline actual-composition campaign: `COMPLETED`, 30 episodes, 60 unique
  traces, 30 opening calls, 30 reduce-only close calls, final fake position
  `0.0`, simulated elapsed time `3480.0`, temporary kill switch engaged,
  network calls `0`
- Ruff, Mypy, compileall, registry oracle, test-quality, hardcoded/forbidden
  scans, package validation, Bandit and dependency audit: PASS

## Authorization boundary

Local completion does not clear `HARD_HOLD` or authorize runtime activation.
The next action is a separate explicit user decision for the real Binance Demo
Testnet campaign against the validated local working tree.

## Reauthorized 100/500 local acceptance

Before the later real campaign, the immutable caps were revised under explicit
authorization to 100 USDT per order and 500 USDT total account exposure. The
focused/affected/registry gates passed 28/84/234 tests respectively; the full
repository passed 4648 tests and 45895/45895 statements at 100%. A fresh
offline actual-composition run completed 30 opens and 30 reduce-only closes,
60 unique traces, final position 0 and network calls 0 under the new caps.

This local PASS enabled only the separately authorized Demo attempt. That
attempt is documented in the execution report and stopped at 28/30 because of
the real wall-clock budget; it does not change this local test result into
release or Mainnet readiness.

## Fixed-cadence acceptance

The current runner defines the 120-second interval as start-to-start. With a
simulated eight-second real verifier cycle, it sleeps 112 seconds and completes
all 30 episodes in 3488 seconds. The actual-composition fake-adapter run
produced 30 opens, 30 reduce-only closes, 60 traces, final position 0 and zero
network calls. Decision: `PASS_LOCAL_ONLY`; a new real campaign still requires
explicit authorization.
