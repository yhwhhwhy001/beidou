# BD-T13 — 独立对账与差异门禁

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T03, BD-T10, BD-T12
- 目标：系统事实与交易所事实独立获取，对所有非 MATCHED 结果实施可解释的风险门禁。

## 2. 问题证据

- 当前运行时把交易所余额同时写入 system facts 与 exchange facts。
- 余额差异被过滤，不触发事故。
- repair_strategy 可能返回 SYSTEM_IS_AUTHORITATIVE。

## 3. 实施范围

- 涉及模块：beidou_safety/reconciliation, beidou_control, beidou_observability
- 预计修改文件：beidou_safety/execution/reconciliation.py, beidou_safety/reconciliation/*, beidou_core/engine.py, migrations/*reconciliation*, tests/reconciliation/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得自动选择系统或交易所为权威并覆盖另一方；不得忽略余额差异。

## 4. 必须实现

1. SystemFactSnapshot 从账本/订单/仓位投影生成；ExchangeFactSnapshot 仅由 Adapter 查询。
2. 比较余额、保证金、仓位数量/方向、订单、保护、杠杆、模式、fee/funding；支持容差 policy。
3. 状态 MATCHED/MISMATCHED/ONE_SIDE_MISSING/BOTH_SIDES_MISSING/STALE/ERROR。
4. 除 MATCHED 外全部阻断 INCREASE；严重差异触发 EXIT_ONLY/LOCK。
5. 差异创建 Incident，记录两侧 hash、来源时间、证据链接；修复必须是显式 reconciliation action。
6. 对账恢复完成前生命周期不得 ACTIVE。

## 5. 接口与合同

- `ReconciliationService.compare(system, exchange)->Result`
- `ReconciliationGate.apply(result)->ControlAction`

## 6. 数据迁移

新增 reconciliation_runs、reconciliation_differences、reconciliation_actions。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/reconciliation -q
```
```bash
pytest -q -k 'reconciliation and (balance or position or order or stale or one_side or lock)'
```

## 9. 验收标准

AC-BD-T13-01. 注入余额/仓位/订单/保护差异均被发现。
AC-BD-T13-02. 所有非 MATCHED 状态阻断新风险。
AC-BD-T13-03. 严重差异触发 LOCK，且 P0 告警不可抑制。
AC-BD-T13-04. 系统和交易所事实来源 hash 不同且可追溯。

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
- 生成 `artifacts/evidence/BD-T13/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
