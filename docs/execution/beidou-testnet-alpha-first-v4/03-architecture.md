# Bounded Testnet soak campaign architecture

Decision: **APPROVED_WITH_CONDITIONS for local implementation**. This is a
self-review decision, not independent acceptance and not real-campaign
authorization.

## Design

`apps.testnet_soak` is a finite foreground orchestrator around the existing
`VerificationRuntime`. It supplies a deterministic `ExecutionProbeKernel` and
a unique episode namespace, then requires a completed open/fill/reduce-only
close plus a fresh signed flat-account reconciliation before the next episode.
The 120-second cadence is scheduled start-to-start: after each reconciliation,
the runner sleeps only until the next absolute campaign deadline, so execution
time consumes the interval instead of extending it.

```text
operator CLI (explicit --confirm-testnet for a future real run)
  -> bounded campaign runner (30 episodes / 3600 s / one symbol)
     -> VerificationRuntime (canonical market, pool, sizing, trace, recovery)
        -> TestnetEnvironmentGuard (100 USDT runtime cap / <=3x / scoped identity)
           -> BinanceUsdmAdapter -> Binance Demo REST
     -> signed flat reconciliation -> continue or immediate stop
  -> finally engage durable kill switch and write campaign manifest
```

The runner imports the verifier; it does not call exchange HTTP methods or
`create_order` directly. Existing `apps.testnet_verify --confirm-testnet`
continues to require `--once`. The soak process is finite and foreground-only;
there is no daemon, scheduler, KeepAlive, restart, or launcher integration.

## Identity and evidence separation

- The guard's task/entrypoint identity is configurable internally but defaults
  to `testnet-verification` / `apps.testnet_verify`; soak uses
  `testnet-soak-campaign` / `apps.testnet_soak`.
- An episode namespace is included in stable trace/intent/client-order IDs, so
  two episodes on the same closed bar cannot alias.
- Probe traces carry `evidence_class=EXECUTION_PROBE`,
  `alpha_evidence=false`, and the actual entrypoint.
- The probe kernel is deterministic and alternates BUY/SELL only to exercise
  both execution directions. It makes no forecast or economic claim.

## Fail-closed state machine

`PREPARED -> RUNNING -> EPISODE_COMPLETED -> RECONCILED_FLAT -> next episode`

Any other transition becomes `STOPPED`. UNKNOWN is never retried by the soak
orchestrator. The verifier's own query-before-retry recovery remains the sole
order-identity recovery mechanism. On every exit path the runner first engages
the durable kill switch, then records the final manifest.

## Conditions

1. Full local gates must pass on the resulting working tree.
2. The global durable kill switch must remain engaged during local testing.
3. No real campaign is executed in this authorization.
4. Independent acceptance and GitHub CI remain outside this local-development
   authorization and must not be claimed as passed.
