# BD-T07 — 持久化 Risk Snapshot 与 Approval

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T01, BD-T05
- 目标：让 R0-R10 在唯一交易链上形成可复现、可过期、可签名的风险快照与审批。

## 2. 问题证据

- 当前 full_evaluate 只覆盖 leverage 与 concentration 等少数规则。
- Approval 主要依赖内存集合。

## 3. 实施范围

- 涉及模块：beidou_safety/risk, beidou_policy, beidou_control
- 预计修改文件：beidou_safety/risk/rules.py, beidou_safety/risk/engine.py, beidou_safety/risk/approval.py, migrations/*risk*, tests/risk/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得由 PostRisk 批准风险；不得在 UNKNOWN 输入时 fail-open。

## 4. 必须实现

1. 定义 RiskSnapshot，绑定账户、仓位、挂单、行情、DQ、exchange health、reconciliation、portfolio、policy、timestamp。
2. R0-R10 每条规则输出 RuleResult(decision, reason_code, observed, limit, source_hash)。
3. RiskApproval 绑定 proposal_hash、risk_snapshot_hash、account/position versions、policy_version、expires_at、signature。
4. 审批在 Intent 创建前必须持久化；发送前再次验证未过期且事实版本未漂移。
5. UNKNOWN、STALE、对账非 MATCHED、交易所健康不安全、控制面阻断均拒绝 INCREASE。
6. PostRisk API 只允许 monitor/degrade/exit/lock，类型上不提供 approve。

## 5. 接口与合同

- `RiskEngine.evaluate(proposal, snapshot)->RiskEvaluation`
- `ApprovalService.issue(evaluation)->SignedApproval`
- `ApprovalService.revalidate(approval, current_versions)->Result`

## 6. 数据迁移

新增 risk_snapshots、risk_rule_results、risk_approvals；所有记录 append-only。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/risk -q
```
```bash
pytest -q -k 'risk and (unknown or stale or reconciliation or expiry or postrisk)'
```

## 9. 验收标准

AC-BD-T07-01. R0-R10 均有至少一个通过、拒绝和边界测试。
AC-BD-T07-02. 未知账户/DQ/交易所健康/对账差异均拒绝增加风险。
AC-BD-T07-03. Approval 过期或事实版本漂移时发送前拒绝。
AC-BD-T07-04. PostRisk 无 approve/sign 接口。

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
- 生成 `artifacts/evidence/BD-T07/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
