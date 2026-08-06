# BD-T19 — 30 天无人值守认证

## 1. 基本信息

- 优先级：**P2**
- 阶段：Phase 5
- 前置依赖：BD-T18
- 目标：以真实经过时间证明 24/7 运行、自愈有界、事实一致和事故闭环；不以短期收益替代可靠性证据。

## 2. 问题证据

- 当前无 30 天无人值守经过时间证据。

## 3. 实施范围

- 涉及模块：certification, autonomy, observability, reporting, operations
- 预计修改文件：beidou_certification/*, beidou_autonomy/*, beidou_reporting/*, runbooks/*, config/g7-unattended-plan.yaml

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得补写、伪造或缩短经过时间；不得自动晋级 Mainnet。

## 4. 必须实现

1. 定义 30 天窗口、允许维护窗口、SLO/SLI、事故等级、暂停和重置规则。
2. 持续采集订单重复率、未保护仓位时长、对账差异、恢复次数、数据质量、延迟、成本和策略健康。
3. 所有自动恢复有最大次数和恢复验证；不可恢复问题升级 LOCK。
4. 日报/周报由事实库生成，签名绑定 evidence；任何缺口标记 NOT_VERIFIABLE。
5. 窗口内重大 P0、重复订单、未保护仓位、未解释账本差异触发认证失败并重新计时。
6. 完成后只签发 G7 Unattended 证书；G8/Mainnet 仍需独立人工批准与资金阶梯计划。

## 5. 接口与合同

- `UnattendedCertification.evaluate(window)->CertificateResult`

## 6. 数据迁移

新增 certification_windows、sli_samples、incident_closures、unattended_certificates。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
python scripts/certification/start_g7.py --plan config/g7-unattended-plan.yaml
```
```bash
python scripts/certification/evaluate_g7.py --window-id <id>
```

## 9. 验收标准

AC-BD-T19-01. 连续 30 天真实经过时间完整，证据无缺口。
AC-BD-T19-02. 无重复订单、无超 SLO 未保护仓位、无未解释关键账本差异。
AC-BD-T19-03. 自愈全部有界且每次恢复后完成事实重建和对账。
AC-BD-T19-04. 最终结论仅为 G7 PASS/FAIL/NOT_VERIFIABLE，不自动 Mainnet。

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
- 生成 `artifacts/evidence/BD-T19/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
