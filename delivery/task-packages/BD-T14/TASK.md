# BD-T14 — 生命周期、恢复和控制面

## 1. 基本信息

- 优先级：**P0/P1**
- 阶段：Phase 1
- 前置依赖：BD-T08, BD-T09, BD-T10, BD-T11, BD-T12, BD-T13
- 目标：把进程重启、状态恢复、事实重建、对账和业务恢复拆为五阶段，所有自动恢复有界且可证伪。

## 2. 问题证据

- 模块生命周期骨架存在，但运行状态与对账/恢复证据未形成强制门。
- 历史上存在固定等待自动 RESUME 风险。

## 3. 实施范围

- 涉及模块：beidou_lifecycle, beidou_autonomy, beidou_control, beidou_observability
- 预计修改文件：beidou_lifecycle/*, beidou_autonomy/*, beidou_control/*, beidou_core/engine.py, tests/recovery/*, tests/chaos/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得基于固定 sleep 自动 RESUME；不得在未完成对账时 ACTIVE。

## 4. 必须实现

1. 统一模块状态 CREATED/INITIALIZING/VALIDATING/ACTIVE/DEGRADED/SUSPENDED/RECOVERING/FAILED/STOPPING/STOPPED。
2. Startup workflow：进程启动→schema/migration检查→状态回放→事实重建→交易所对账→保护验证→trading-ready。
3. Control states READ_ONLY/NO_NEW_RISK/EXIT_ONLY/EMERGENCY_FLATTEN/LOCK 持久化、版本化、fencing。
4. 自愈策略包含故障指纹、有界重试、退避、熔断、最大次数、恢复验证、升级告警、rollback point。
5. 不可自动恢复事项：账本差异、未知订单、未保护仓位、凭据权限异常、数据库损坏。
6. 四层健康 liveness/readiness/trading-ready/exit-ready 分离，UNKNOWN 不得报告 healthy。

## 5. 接口与合同

- `RecoveryCoordinator.run()->RecoveryCertificate`
- `ControlPlane.transition(action, reason, expected_version)->State`
- `HealthService.snapshot()->LayeredHealth`

## 6. 数据迁移

新增 module_states、control_state_events、recovery_attempts、recovery_certificates、incidents。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/recovery tests/chaos -q
```
```bash
pytest -q -k 'recovery and (db_restart or network or process or reconcile or bounded)'
```

## 9. 验收标准

AC-BD-T14-01. 未完成事实重建、对账、保护验证时 trading-ready=false。
AC-BD-T14-02. 固定等待不会自动 ACTIVE。
AC-BD-T14-03. 重复恢复超过上限进入 LOCK 并告警。
AC-BD-T14-04. 数据库/网络/进程故障注入后状态转换和证据完整。

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
- 生成 `artifacts/evidence/BD-T14/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
