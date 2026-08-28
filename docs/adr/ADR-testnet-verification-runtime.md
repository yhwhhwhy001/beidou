# ADR: Testnet Verification Runtime

- Status: accepted for implementation, Testnet admission remains HOLD
- Date: 2026-08-28
- Baseline: `ea68bcdc65ed6e2fdb5d749363d9b377e25e7cc2`
- Scope: local single-operator Binance USD-M Testnet/Demo only

## Decision

Create `apps.testnet_verify` as the only new Testnet verification composition
root. It owns the closed-bar decision cycle and composes neutral strategy/data
contracts with the Binance adapter. It must not import the legacy launcher,
Engine, Supervisor, certification, production, chaos, HA, policy-signing, or
external signed-capability paths.

The verifier may use the existing Binance REST transport only through the
adapter boundary. Binance HMAC remains part of the exchange protocol. The
verifier adds an explicit local Testnet guard with hard Mainnet denial,
absolute caps, stable intent identity, query-before-retry, and a kill switch.

## Consequences

- The legacy modules remain present and importable for historical audit and
  explicitly invoked future paths; they are not deleted or silently changed.
- A Testnet write is not evidence of economic alpha. Testnet execution and
  E0-E6 Economic Truth are separate decisions.
- Mainnet is prohibited. No release or production activation follows from
  local tests or package validation.

## Rejected alternatives

- Extending `beidou_core.engine` would retain the God Object as the Testnet
  owner and create a second decision path.
- Disabling `BEIDOU_TERMINAL_WRITE_HOLD` globally would broaden legacy runtime
  authority and cannot establish a bounded Testnet write capability.
- Removing production modules would destroy audit history and is outside the
  Testnet scope.

## Verification

The architecture tests in
`tests/architecture/test_testnet_verification_boundaries.py` are non-vacuous:
they inventory the verifier source and reject forbidden imports, direct
transport writers, and deletion of frozen modules.
