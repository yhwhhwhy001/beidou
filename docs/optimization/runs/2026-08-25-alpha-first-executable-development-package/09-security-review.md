# Security review — candidate T00 activation

## Decision

`PASS_WITH_CONDITIONS` for generating reviewable public trust and approval artifacts. T00 remains `HOLD_PENDING_HUMAN_SIGNATURE`.

## Assets and trust boundaries

- Protected dirty source checkout: `/Users/maguannan/beidou`; must remain unchanged.
- Candidate clean implementation checkout: `/Users/maguannan/beidou-isolated/BD-AF-P0P3-impl`.
- Public trust root: `/Users/maguannan/beidou-authorization/BD-AF-P0P3-V2/approval-trust-root.allowed_signers`.
- Human approval document: `/Users/maguannan/beidou-authorization/BD-AF-P0P3-V2/BD-AF-P0-T00.approval.json`.
- Human private key: outside package/repository/result/Agent custody and never read or copied.

## Controls applied

- Reused only the existing public key `/Users/maguannan/.ssh/id_ed25519.pub`; its public fingerprint is `SHA256:l2fe96Mv2K8iVBPt6+wwNVSE1X1c+SWivXOlTXVmjiI`.
- Bound allowed-signers identity `yhwhhwhy001@gmail.com` to the public key and SHA-256 `9b38ce274603493d94fbe29fdcf3636ca4a2808d0e6c13168cdd965dd48a31c0`.
- Bound approval scope to `LOCAL_IMPLEMENTATION_ONLY`, exact task T00, exact baseline/worktrees, exact package fingerprint, empty dependencies, H0/H1 only, and a finite expiry.
- Kept approval and trust root outside both source and implementation checkouts.
- Left signature absent; no Agent self-approval or private-key operation occurred.
- Confirmed unsigned preflight fails closed solely with `HUMAN_APPROVAL_SIGNATURE_MISSING`.

## Remaining condition

The human must confirm the candidate facts and sign the exact approval bytes under namespace `beidou-alpha-first-task-approval-v1`. Any approval edit after signing invalidates the signature and requires a new signature. A valid signature authorizes only local T00 work; it does not authorize Git push, runtime activation, Paper/Testnet, exchange access, Mainnet, or real funds.
