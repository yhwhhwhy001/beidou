# V4 execution context

## Scope and authority

- User request: implement the uploaded Beidou Testnet Alpha-First V4 package
  in `/Users/maguannan/beidou`.
- Current authorized scope: repository implementation and local validation;
  no Mainnet activity, no production deployment, and no unapproved external
  messages or money movement.
- Testnet write activity: not executed in this run. Real Testnet evidence is
  unavailable until credentials, explicit CLI confirmation, and a deliberate
  local campaign are supplied.

## Instruction precedence

1. System/developer instructions and repository rules.
2. The user's direct request.
3. Uploaded V4 package documents, treated as project requirements and
   acceptance contracts.
4. Historical run state and memory, used only as context and never as current
   pass evidence.

## Baseline evidence

- Repository: `yhwhhwhy001/beidou`
- Branch: `main`
- HEAD: `ea68bcdc65ed6e2fdb5d749363d9b377e25e7cc2`
- Working tree at implementation entry: dirty; pre-existing/local implementation
  changes were preserved and are listed by `git status --short`.
- Uploaded package: `/Users/maguannan/Downloads/Beidou_Testnet_AlphaFirst_Execution_Package_V4.0_2026-08-28.zip`
- Uploaded ZIP SHA-256: `eedf23295f1980789d5a7a016483671b2d7103946b01fcd36e1ac4c4b3088028`
- ZIP integrity: `unzip -t` passed

## Baseline and instruction handling

- The uploaded prompt asks for fetch/checkout/pull before implementation. That
  operation was not used because this checkout contained local work and the
  user request authorizes local development, not overwriting or synchronizing
  the worktree. The current HEAD already equals the package baseline.
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
  durable DecisionTrace, and Binance adapter. A dry run cannot substitute for
  the required real Testnet evidence.
