# 北斗受控收敛工程 PRD

文档版本：V1.0  
状态：已批准用于开发执行  
基线：`6b0d95dfa67465be421d9a4f5eaa5a406e7c3341`

## 1. 背景与问题

北斗已有大量模块骨架，但真实运行链未收敛。代码存在不等于生产能力；当前目标是先建立唯一交易事实链和证据门，再恢复研究、Paper、Testnet 与无人值守认证。

## 2. 目标

1. 所有风险增加订单经过唯一 Data→Strategy→Risk→Intent→Outbox→Exchange→Order→Position→Protection→Ledger→Reconciliation 链。
2. UNKNOWN/ERROR 输入 fail-closed。
3. Backtest/Paper/Shadow/Testnet 共用同一策略和状态合同。
4. 生产状态使用 PostgreSQL，事件可重放，恢复后必须对账。
5. 所有 Gate 有原始证据、可重现命令和 Falsifier。

## 3. 非目标

- 本包不开放 Mainnet；
- 不承诺持续盈利；
- 不在 G0-G3 前优化收益参数；
- 不引入企业多人审批，本地单操作员仍由机器门禁和不可变审计保护。

## 4. 核心功能需求

| FR | 需求 | 任务 |
|---|---|---|
| FR-001 | 安全启动、配置与密钥 fail-closed | T00-T02 |
| FR-002 | 唯一 Exchange Gateway | T03 |
| FR-003 | ClosedBar/DQ/PIT 数据链 | T04 |
| FR-004 | Typed Strategy Kernel parity | T05 |
| FR-005 | 因子证据生命周期 | T06,T17 |
| FR-006 | 持久化风险快照和审批 | T07 |
| FR-007 | Intent/Outbox/订单幂等与恢复 | T08,T09 |
| FR-008 | Fill/Position 权威投影 | T10 |
| FR-009 | 原生保护与监督 | T11 |
| FR-010 | 复式账本与独立对账 | T12,T13 |
| FR-011 | 生命周期、自愈、控制面 | T14 |
| FR-012 | CI/证据门禁 | T15 |
| FR-013 | 真实 Paper 与研究证据 | T16,T17 |
| FR-014 | Testnet 与无人值守认证 | T18,T19 |

## 5. 非功能指标

- Global line coverage ≥85%；critical line ≥95%；critical branch ≥90%；mutation ≥75%。
- 任何 UNKNOWN/ERROR 不能增加风险。
- 关键事件至少一次持久化、幂等消费、确定性重放。
- 保护 SLO、订单 ACK SLO、对账 SLO 由 Policy 定义并由测试强制。
- P0/LOCK 告警不可抑制。

## 6. 验收

详见 `docs/optimization/06_ACCEPTANCE_MATRIX.md` 与每个任务的 `ACCEPTANCE.yaml`。
