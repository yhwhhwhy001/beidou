# V4 execution context

## Scope and authority

- User request: implement the uploaded Beidou Testnet Alpha-First V4 package
  in `/Users/maguannan/beidou`.
- Current authorized scope: repository implementation, local validation, and
  the previously authorized bounded Testnet campaign. Mainnet and production
  remain prohibited.
- Incident boundary: an unbounded verifier was installed with launchd
  `KeepAlive` after the bounded campaign. It was disabled and stopped; the
  durable kill switch remains engaged. No new campaign is authorized while
  current code/CI evidence is incomplete.

## Instruction precedence

1. System/developer instructions and repository rules.
2. The user's direct request.
3. Uploaded V4 package documents, treated as project requirements and
   acceptance contracts.
4. Historical run state and memory, used only as context and never as current
   pass evidence.

## Baseline evidence

- Repository: `yhwhhwhy001/beidou`
- Remediation branch: `codex/v4-incident-remediation`
- Incident baseline: `db2debcfed1a969e627b8a34b4e6bb89d8815184`
- Package baseline: `ea68bcdc65ed6e2fdb5d749363d9b377e25e7cc2`
- Working tree contains preserved untracked Testnet evidence; release
  verification is performed in a clean detached worktree.
- Uploaded package: `/Users/maguannan/Downloads/Beidou_Testnet_AlphaFirst_Execution_Package_V4.0_2026-08-28.zip`
- Uploaded ZIP SHA-256: `eedf23295f1980789d5a7a016483671b2d7103946b01fcd36e1ac4c4b3088028`
- ZIP integrity: `unzip -t` passed

## Baseline and instruction handling

- The package baseline is historical. Current changes are compared against it
  by requirement ID; line numbers are not applied mechanically.
- The uploaded documents are project requirements and acceptance contracts;
  they do not grant Testnet credentials, exchange-write permission, deployment
  permission, or production/Mainnet authority.
- Historical run state under
  `docs/optimization/runs/2026-08-25-alpha-first-executable-development-package/`
  records an earlier implementation HOLD and is not treated as current pass
  evidence.

## Required gates and stop conditions

- PKG-00 through PKG-10 are dependency ordered; do not skip a failed P0
  prerequisite.
- No evidence means NOT_VERIFIABLE, not PASS.
- Mainnet is PROHIBITED.
- A real Testnet order must be routed through the verifier, bounded guard,
  durable DecisionTrace, and Binance adapter. Confirmed writes now require
  `--once`; daemon/KeepAlive execution is prohibited by code and architecture
  test.
- Fresh signed GET reconciliation at `2026-08-29T06:07:46Z` showed no nonzero
  positions, no open orders, and no open algo orders. Startup recovery linked
  the sole FILLED trace to a later quantity-matched reduce-only CLOSED trace,
  producing `0` unresolved traces without an exchange write.
- GitHub Actions runner allocation remains blocked by billing/spending limits;
  therefore AC-TN-017 and release readiness remain blocked.
