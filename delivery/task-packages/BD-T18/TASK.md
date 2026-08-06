# BD-T18 — Binance Testnet 认证

## 1. 基本信息

- 优先级：**P1**
- 阶段：Phase 4
- 前置依赖：BD-T00, BD-T01, BD-T02, BD-T03, BD-T04, BD-T05, BD-T06, BD-T07, BD-T08, BD-T09, BD-T10, BD-T11, BD-T12, BD-T13, BD-T14, BD-T15, BD-T16, BD-T17
- 目标：在受保护 Testnet 环境验证真实订单、用户流、原生保护、恢复和对账的完整异常矩阵，签发 G5 或明确 FAIL。

## 2. 问题证据

- 当前没有与 HEAD 绑定的真实 Testnet 订单链、重启、对账和故障注入证据。

## 3. 实施范围

- 涉及模块：certification, exchange, execution, protection, reconciliation, recovery
- 预计修改文件：beidou_certification/*, tests/testnet/*, scripts/testnet/*, runbooks/testnet/*, artifacts/evidence/testnet/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得使用 Mainnet URL/真实资金；不得把 NOT_VERIFIABLE 作为 PASS；不得自动执行超过安全名义金额的订单。

## 4. 必须实现

1. Gate Runner 校验 commit/config/policy/certificate 与 Testnet URL、API 权限、IP 白名单、无提款权限。
2. 场景覆盖 create/query/cancel、stable clientOrderId、ACK 丢失、超时、重复、部分成交、cancel/fill race。
3. 验证 user data stream 断线重连、listen key 续期、事件幂等和 REST 回补。
4. 验证原生保护 ACK、拒绝、取消、重启恢复、数量匹配和保护 SLO。
5. 验证数据库重启、进程崩溃、双 worker、时钟漂移、限频、凭据失效、对账差异。
6. 任一 P0 场景失败立即停止，控制面 LOCK，生成 Incident 与回滚证据。
7. G5 证书签名绑定 commit/config/policy、所有场景 PASS 和 evidence manifest。

## 5. 接口与合同

- `TestnetGateRunner.run(plan)->GateCertificate`
- `ScenarioRunner.run(id)->ScenarioResult`

## 6. 数据迁移

无生产业务迁移；Testnet 数据独立 schema/database。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
python scripts/testnet/run_g5.py --plan config/g5-testnet-plan.yaml --confirm-testnet
```
```bash
python delivery/scripts/verify_evidence_bundle.py artifacts/evidence/testnet
```

## 9. 验收标准

AC-BD-T18-01. G5 全部强制场景 PASS；无 P0/P1 未闭合。
AC-BD-T18-02. 每个订单可从 Intent 追溯至 ACK/Fill/Position/Protection/Ledger/Reconciliation。
AC-BD-T18-03. 重复、超时和崩溃均未产生重复订单。
AC-BD-T18-04. Mainnet URL、提款权限或证据不完整时 Gate 必须 FAIL。

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
- 生成 `artifacts/evidence/BD-T18/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
