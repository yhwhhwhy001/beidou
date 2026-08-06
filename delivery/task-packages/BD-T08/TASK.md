# BD-T08 — 持久化 Intent 与 PostgreSQL Transactional Outbox

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T07
- 目标：合并两套内存 Outbox，以 PostgreSQL 事务保证业务 Intent 与待发送消息原子持久化。

## 2. 问题证据

- IntentOutbox 与 TransactionalOutbox 均为内存 dict/list。
- 重启会丢失消息与幂等状态。

## 3. 实施范围

- 涉及模块：beidou_safety/execution, beidou_infra/outbox, PostgreSQL
- 预计修改文件：beidou_safety/execution/intent.py, beidou_infra/outbox.py, beidou_infra/db/*, migrations/*outbox*, tests/integration/outbox/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得使用 SQLite 或内存容器作为生产事实源；不得 ACK 未发送消息。

## 4. 必须实现

1. 建立 order_intents 与 outbox_messages，同一数据库事务提交。
2. idempotency_key 与 stable_client_order_id 建立唯一约束；重复业务请求返回原 Intent。
3. 状态固定 PENDING→SENDING→SENT→ACKED；网络不确定进入 UNKNOWN；超过策略进入 DEAD_LETTER。
4. Worker 使用 SELECT ... FOR UPDATE SKIP LOCKED、lease_owner、lease_until 和 fencing token。
5. crash-before-send 可重取；crash-after-send-before-ack 必须先按 clientOrderId 查询，不可直接重发。
6. 每次状态转换 append outbox_events，禁止覆盖审计历史。

## 5. 接口与合同

- `IntentService.create_with_outbox(command, approval)->Intent`
- `OutboxWorker.claim(batch)->Messages`
- `OutboxWorker.resolve_unknown(message)->Resolution`

## 6. 数据迁移

新增 order_intents、outbox_messages、outbox_events、dead_letters；唯一约束和索引必须并发安全。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/integration/outbox -q
```
```bash
pytest -q -k 'outbox and (crash or idempotent or double_worker or unknown or dead_letter)'
```

## 9. 验收标准

AC-BD-T08-01. 业务 Intent 与 Outbox 不会出现单边提交。
AC-BD-T08-02. crash-before-send 不丢消息；crash-after-send 不重复订单。
AC-BD-T08-03. 双 worker 竞争时同一消息只有一个有效发送者。
AC-BD-T08-04. 重启后 pending/unknown/dead-letter 状态完整恢复。

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
- 生成 `artifacts/evidence/BD-T08/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
