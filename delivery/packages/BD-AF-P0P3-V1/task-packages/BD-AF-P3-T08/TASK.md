# BD-AF-P3-T08 — Candidate versus Champion portfolio decision

## Goal

Decide Candidate disposition from incremental portfolio value and uncertainty rather than standalone Sharpe, while keeping promotion outside this package.

## Requirements and dependencies

- Requirements: `AF-REQ-009`, `AF-REQ-017`, `AF-REQ-018`.
- Dependency: independently accepted `BD-AF-P3-T07`.
- Required approvals: `H1_TASK_START_PER_TASK`, `H3_PORTFOLIO_OWNER_BEFORE_BD_AF_P3_T08`.

## Allowed paths

- `apps/factor_miner/__main__.py` for the existing offline compare command only
- `beidou_research/mining/selection/marginal_contribution.py`
- `beidou_research/portfolio/` comparison contracts/calculators/store
- `tests/research/test_candidate_champion_decision.py`
- deterministic correlation/cost/capacity/tail/regime/uncertainty fixtures
- task-owned evidence

## Decision contract

Bind Candidate/Champion/portfolio IDs and evidence hashes; marginal return/risk and confidence; correlation/diversification; turnover and incremental cost; capacity/impact; drawdown/tail contribution; regime stability; constraints; missing-evidence reasons; policy/Owner digest; immutable decision and supersession chain.

Allowed dispositions in this slice are research lifecycle states such as `REJECT`, `HOLD`, `SHADOW_ELIGIBLE`, or `NOT_VERIFIABLE`. No Paper/Testnet promotion or capital allocation is authorized.

## First proof and sequence

Start with counterexamples: high standalone Sharpe but redundant/costly/tail-worsening Candidate; modest Sharpe with robust diversification; missing uncertainty/capacity/regime input; tampered Champion. Implement independent marginal recomputation and immutable decision storage. Completion requires all required dimensions, uncertainty-aware result, tamper detection, and no promotion side effect.
