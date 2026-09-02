# Testnet Verification runbook

## 1. Read-only preflight

Run from the repository root:

```bash
python -m apps.testnet_verify --help
pytest tests/unit/test_testnet_binance_contracts.py tests/integration/test_testnet_verification_runtime.py -q
```

The default invocation omits `--confirm-testnet` and therefore does not
authorize risk-increasing writes. Do not use a legacy safety/certification
runner as a substitute for this entrypoint.

## 2. Credentials and account boundary

Use a dedicated Binance USDⓈ-M Testnet/Demo account. Inject credentials from
an external secret manager or protected process environment; never commit,
paste, or echo them. The endpoint must be exactly an allowed HTTPS Testnet
host. Mainnet is prohibited by code, regardless of CLI or environment values.

## 3. Explicit bounded campaign

Only after a deliberate local Testnet write authorization:

```bash
python -m apps.testnet_verify --once --confirm-testnet --close-after-verify \
  --max-notional 25 --max-leverage 3 --max-instruments 1
```

## 3b. No continuous campaign supervisor

The V4 verification campaign is a single bounded episode. Always retain
`--once`; do not install the verifier as a daemon or configure an automatic
restart policy. A terminal/session interruption is a stop condition followed
by read-only reconciliation, not permission to restart and submit again.

The verifier must produce a startup manifest, a durable PREPARED trace before
the first write, leverage set/readback, ACK or UNKNOWN recovery, position
readback, reconciliation, and (when filled) a reduce-only close. Inspect only
redacted fields in `evidence/testnet-verification/<run_id>/manifest.json` and
the configured DecisionTrace JSONL file.

## 4. Stop conditions

Stop and leave the trace `UNKNOWN`/`NOT_VERIFIABLE` when any of these facts is
missing or ambiguous: exchangeInfo rules, closed bars, account permissions,
leverage readback, order identity, position readback, reconciliation, or
network outcome. Never change the client id after an ambiguous POST. A
deterministic venue rejection may end that intent as `FAILED`; it does not
prove a fill.

## 5. Recovery and rollback

- Restart with the same trace and pool paths; the runtime queries the durable
  client id before any same-id retry.
- Keep UNKNOWN unresolved until a venue query and position reconciliation make
  the outcome known.
- Use the owned reduce-only close path for an open verification position.
- Engage the local kill switch and stop new risk if the campaign is not
  reconcilable. Do not use a Mainnet endpoint or a global write bypass.

Engage the durable switch without starting the runtime:

```bash
python -m apps.testnet_verify --engage-kill-switch
```

The terminal write authority checks the switch file at the final request
boundary. It blocks leverage and risk-increasing orders while retaining owned
cancel/reduce-only recovery. There is deliberately no automatic clear flag;
removal requires a separate operator decision after account reconciliation.

Testnet VERIFIED is an execution-fact decision only. It is not alpha
profitability, E0-E6 Economic Truth, production readiness, or live-money
authorization.

## 6. Bounded execution-probe soak (separate authorization required)

Inspect the finite entrypoint locally without network access:

```bash
python -m apps.testnet_soak --help
pytest tests/unit/test_testnet_soak.py \
  tests/architecture/test_testnet_soak_boundaries.py -q
```

After local gates pass, stop. Do not clear the durable kill switch or invoke
the following shape until a separate real-campaign authorization identifies
the exact working tree and confirms a fresh signed flat-account preflight:

```bash
python -m apps.testnet_soak --confirm-testnet \
  --episodes 30 --max-duration-seconds 3600 \
  --cycle-interval-seconds 120 --target-notional 10 \
  --absolute-notional-ceiling 25 --max-leverage 3 \
  --symbol BTCUSDT
```

The process is foreground-only. It performs no automatic restart and no sleep
after episode 30. Every episode must open/fill, reduce-only close, and pass a
fresh signed flat/no-orders/unresolved-zero reconciliation. Any UNKNOWN or
other non-CLOSED fact stops the campaign. The final campaign manifest must
retain `execution_mode=EXECUTION_PROBE`, `alpha_evidence=false`, and
`economic_truth_e0_e6=NOT_EVALUATED`.
