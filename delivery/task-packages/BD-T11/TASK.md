# BD-T11 — 交易所原生保护与安全监督

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T03, BD-T10
- 目标：风险仓位在严格 SLO 内获得交易所确认的原生止损/止盈保护，本地逻辑仅作监督和应急。

## 2. 问题证据

- 当前保护主要靠本地轮询触发市场单。
- 缺失 swing 数据回退固定 5%；保护参数含代码默认值。

## 3. 实施范围

- 涉及模块：beidou_safety/protection, beidou_exchange, beidou_policy, beidou_control
- 预计修改文件：beidou_safety/protection/*, beidou_exchange/binance_usdm/adapter.py, migrations/*protection*, tests/protection/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在保护失败后继续允许新增风险；不得以固定 5% 默认值声称保护完整。

## 4. 必须实现

1. ProtectionPolicy 从版本化 Policy 获取，缺失关键输入时拒绝开仓。
2. 开仓成交后在 protection_ack_slo 内提交 STOP_MARKET/TAKE_PROFIT 等交易所原生条件单并验证 ACK。
3. 绑定 position_id、position_side、quantity、reduceOnly/closePosition、workingType、priceProtect 和交易规则。
4. 保护状态持久化；重启后查询交易所恢复，避免重复条件单。
5. 保护被拒、取消、失联或数量不匹配时立即 NO_NEW_RISK/EXIT_ONLY；超过严重阈值 LOCK。
6. 本地价格监督只能触发应急退出，不能替代原生保护证书。

## 5. 接口与合同

- `ProtectionService.ensure_for_position(position, policy)->ProtectionCertificate`
- `ProtectionSupervisor.reconcile()->ProtectionHealth`

## 6. 数据迁移

新增 protection_plans、protection_orders、protection_events、protection_certificates。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/protection -q
```
```bash
pytest -q -k 'protection and (native or reject or restart or duplicate or quantity or slo)'
```

## 9. 验收标准

AC-BD-T11-01. 每个风险仓位在 SLO 内有 ACKED 原生保护证书。
AC-BD-T11-02. 保护缺失/拒绝/失联自动进入 EXIT_ONLY 或 LOCK。
AC-BD-T11-03. 重启后不会重复创建保护单。
AC-BD-T11-04. 不存在固定 5% 或 2x ATR 等不可追溯业务默认值。

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
- 生成 `artifacts/evidence/BD-T11/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
