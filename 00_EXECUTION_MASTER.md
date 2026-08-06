# 北斗工程重构总执行书

## 1. 最终目标

将北斗收敛为仅服务 Binance USDⓈ-M 永续合约、本地单操作员、可审计、可恢复、24/7 无人值守候选的量化交易系统。最终目标是长期净交易成本后风险调整正收益，但本工程包不允许通过回测挑选、降低风险门槛或伪造证据证明盈利。

优先级固定为：

```text
资金与账户安全 > 事实链正确性 > 数据和订单一致性 > 风险调整后净收益 > 可靠性 > 自动化 > 性能 > 功能数量
```

## 2. 当前决策与强制状态

```text
Decision: PIVOT
Paper: HOLD
Shadow: HOLD
Testnet: HOLD
Mainnet: PROHIBITED
Baseline: 6b0d95dfa67465be421d9a4f5eaa5a406e7c3341
```

## 3. 唯一目标交易链

```text
Exchange Market Data
→ Raw Event Store
→ ClosedBar / DQ / PIT Feature Store
→ Typed Strategy Kernel
→ Portfolio Decision
→ Risk Snapshot + Signed Approval
→ Persisted Intent + PostgreSQL Transactional Outbox
→ BinanceUsdmAdapter
→ Unified Order Aggregate
→ Fill / Position Projection
→ Native Protection
→ Double-entry Ledger
→ Independent Reconciliation
→ Control Plane / Recovery / Observability
```

任何平行的 Exchange、Outbox、Order State、Position、Ledger 或 Reconciliation 路径均必须删除或明确禁用。

## 4. 执行阶段

| 阶段 | 任务 | Gate | 可进入下一阶段条件 |
|---|---|---|---|
| Phase 0 | T00-T02 + T15 基础 | G0 | 可编译、可收集、配置/密钥 fail-closed |
| Phase 1 | T03-T14 | G1-G3 | 唯一事实链、数据/策略/订单/账本/对账收敛 |
| Phase 2 | T16-T17 | G4 | 真实 Paper 成本模型与研究证据 |
| Phase 4 | T18 | G5 | Testnet 异常矩阵全部 PASS |
| Phase 5 | T19 | G6-G7 | 7 天 Shadow + 30 天无人值守 |
| 独立批准 | 非本包自动执行 | G8 | Mainnet Candidate；仍需人工明确批准 |

## 5. 首批执行边界

第一批只执行 `BD-T00 → BD-T01/BD-T02 → BD-T03`，`BD-T15` 同步加固。完成后停止并提交独立验收。不得提前开始策略、因子、仓位、杠杆或收益参数优化。

## 6. 完成定义

任务完成必须同时满足：

1. 所有 `must_implement` 行为实现；
2. 所有验收命令已执行且原始输出归档；
3. 无越界修改；
4. 无新增 P0/P1；
5. 迁移和回滚已演练；
6. evidence manifest 完整并通过 schema；
7. 独立验收状态为 PASS，或不存在 P0 的 CONDITIONAL_PASS；
8. 一个任务一个独立 commit。

## 7. 停止条件

出现以下任一情况立即停止：Mainnet URL、真实资金、提款权限、未解释重复订单、未保护仓位、账本不平、对账严重差异、默认密钥、测试/扫描器被弱化、证据不可重现、任务依赖未完成。
