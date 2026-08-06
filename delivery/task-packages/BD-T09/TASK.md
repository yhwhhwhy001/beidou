# BD-T09 — 统一订单聚合与 UNKNOWN 恢复

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T03, BD-T08
- 目标：合并两套订单状态机，建立事件追加、幂等用户流和可验证 UNKNOWN 恢复。

## 2. 问题证据

- OrderStateTracker 与 OrderStateMachine 两套状态模型并存。
- 运行链使用较弱的 Tracker，且部分状态迁移顺序异常。

## 3. 实施范围

- 涉及模块：beidou_safety/execution/order_machine, beidou_exchange/user_stream, PostgreSQL
- 预计修改文件：beidou_safety/execution/order_machine.py, beidou_safety/execution/order_state.py, beidou_exchange/binance_usdm/*, migrations/*order*, tests/state_machine/order/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在超时后生成新 clientOrderId 重发；不得以本地猜测关闭 UNKNOWN。

## 4. 必须实现

1. 唯一状态机覆盖 CREATED/PERSISTED/SENT/ACKNOWLEDGED/PARTIALLY_FILLED/FILLED/CANCEL_PENDING/CANCELED/REJECTED/EXPIRED/UNKNOWN。
2. 每次事件 append order_events，带 exchange_event_id/trade_id 幂等键。
3. 支持重复、乱序、部分成交、cancel/fill race；非法转换记录 Incident 并 fail-closed。
4. UNKNOWN 恢复先 query_order_by_client_id，再结合用户流与 open orders/account trades 重建。
5. 同 symbol 存在未闭合 UNKNOWN 风险增加订单时阻断新风险。
6. 删除旧状态机运行路径和重复枚举。

## 5. 接口与合同

- `OrderAggregate.apply(event)->TransitionResult`
- `OrderRecoveryService.recover(order_id)->RecoveryResult`
- `UserEventConsumer.consume(event)->IdempotentResult`

## 6. 数据迁移

新增 orders、order_events、fills；迁移旧 order_states 为 LEGACY_IMPORT，不能伪造缺失事件。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/state_machine/order -q
```
```bash
pytest -q -k 'order and (property or replay or duplicate or out_of_order or cancel_fill or unknown)'
```

## 9. 验收标准

AC-BD-T09-01. 状态机模型测试覆盖所有合法/非法转换。
AC-BD-T09-02. 重复用户流事件不改变累计成交。
AC-BD-T09-03. cancel/fill race 回放得到唯一终态。
AC-BD-T09-04. UNKNOWN 未闭合时同标的新风险确定性阻断。

验收状态仅允许：`PASS / CONDITIONAL_PASS / FAIL / NOT_VERIFIABLE`。

## 10. 交付证据

- 修改文件清单与 Git diff --stat
- 执行命令、退出码、开始/结束时间、stdout/stderr 原文及 SHA-256
- 测试报告与覆盖率报告
- 数据库/API/运行证据（如适用）
- 本任务验收矩阵逐项结果
- 遗留问题、风险与回滚命令
- commit SHA、branch、config hash、policy version、agent ID

## 11. 完成后动作

- 更新 `docs/optimization/06_ACCEPTANCE_MATRIX.md`；
- 生成 `artifacts/evidence/BD-T09/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
