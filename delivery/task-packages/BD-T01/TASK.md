# BD-T01 — 密钥与审批 Fail-Closed

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 0
- 前置依赖：BD-T00
- 目标：消除默认审批密钥与无签名兼容旁路，将 Paper 与 Testnet/未来实盘的审批端口彻底分离。

## 2. 问题证据

- RiskApprovalSignerImpl 在缺少 BEIDOU_SIGNING_KEY 时回退到 beidou-testnet-default-key。
- verify() 无签名时仅检查内存 approved 集合。

## 3. 实施范围

- 涉及模块：beidou_safety/risk, beidou_security, beidou_policy, apps/autopilot
- 预计修改文件：beidou_safety/risk/engine.py, beidou_safety/risk/approval.py, beidou_security/*, apps/autopilot/__main__.py, tests/security/*, tests/unit/risk/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得改变 R0-R10 风险业务阈值；不得把测试密钥写入仓库或配置模板。

## 4. 必须实现

1. 删除所有默认 HMAC/签名密钥和无签名向后兼容验证路径。
2. 定义 ApprovalSignerPort；生产/Testnet 实现无密钥时返回 SIGNING_UNAVAILABLE 并拒绝风险增加。
3. 定义 PaperApprovalPort，仅生成明确标记 mode=PAPER、non_tradable=true 的纸面决策，不复用真实签名器。
4. 签名内容绑定 approval_id、proposal_hash、account_snapshot_hash、risk_snapshot_hash、policy_version、expires_at、nonce。
5. 使用常量时间比较；签名过期、篡改、重放、版本不一致均拒绝。
6. 密钥仅从环境/秘密提供器注入；日志、异常、evidence 自动脱敏。

## 5. 接口与合同

- `ApprovalSignerPort.sign(payload)->SignedApproval`
- `ApprovalVerifierPort.verify(signed)->VerificationResult`
- `PaperApprovalPort.decide(snapshot)->PaperDecision`

## 6. 数据迁移

如已有 approval 表，新增 mode、payload_hash、expires_at、nonce、signature_key_id；使用 forward-only migration。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
rg -n 'default-key|testnet-default|beidou-default' beidou_* apps config
```
```bash
pytest tests/unit/risk tests/security -q
```
```bash
pytest -q -k 'approval and (tamper or replay or expire or missing_key)'
```

## 9. 验收标准

AC-BD-T01-01. 源码及模板中不存在默认签名密钥。
AC-BD-T01-02. 缺少密钥时 Testnet 风险增加请求确定性拒绝，错误码为 SIGNING_UNAVAILABLE。
AC-BD-T01-03. 无签名、过期、篡改、重放、错误 policy version 均不可通过。
AC-BD-T01-04. Paper 决策不具备交易写能力且无法被 Exchange Executor 接受。

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
- 生成 `artifacts/evidence/BD-T01/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
