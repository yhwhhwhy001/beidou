# BD-T06 — 证据驱动因子生命周期

## 1. 基本信息

- 优先级：**P0/P1**
- 阶段：Phase 1
- 前置依赖：BD-T04, BD-T05
- 目标：将因子生命周期从内存枚举迁移为持久化、证据驱动、不可跳级的晋级门禁。

## 2. 问题证据

- engine 初始化时自动将八个因子推进到 Challenger。
- 晋级未绑定 dataset、统计、成本容量、Paper 证据。

## 3. 实施范围

- 涉及模块：beidou_research/factors, beidou_research/mining, beidou_certification, PostgreSQL
- 预计修改文件：beidou_research/factors/*, beidou_research/mining/*, beidou_research/promotion.py, migrations/*factor*, tests/research/lifecycle/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在启动代码自动晋级；不得手工更新 lifecycle 字段绕过服务。

## 4. 必须实现

1. 状态机固定为 IDEA→GENERATED→SANITY_PASSED→RESEARCH_VALIDATED→OOS_VERIFIED→COST_CAPACITY_VERIFIED→PAPER_TRADING→CHALLENGER→ACTIVE。
2. 每次转换创建 immutable PromotionDecision，绑定 factor_version、commit、dataset hash、evidence IDs、policy、falsifier。
3. 数据库约束禁止跳级和覆盖历史；并发晋级使用 optimistic lock/version。
4. SUSPENDED 可创建新 challenger version；RETIRED 原版本不可恢复。
5. 删除 engine 中自动 transition；运行时只加载数据库标记 ACTIVE 且证书有效的版本。

## 5. 接口与合同

- `PromotionService.request_transition(factor_id, target, evidence)->Decision`
- `FactorRegistry.get_runnable(as_of)->list[FactorVersion]`

## 6. 数据迁移

新增 factor_definitions、factor_versions、factor_lifecycle_events、promotion_decisions、evidence_links。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/research/lifecycle -q
```
```bash
pytest -q -k 'factor and (promotion or skip_gate or retire or concurrent)'
```

## 9. 验收标准

AC-BD-T06-01. 缺少任一强制证据时晋级返回 FAIL，状态不变。
AC-BD-T06-02. 重启后生命周期、版本和证据链一致。
AC-BD-T06-03. 不存在直接 UPDATE lifecycle 的生产代码。
AC-BD-T06-04. 退役版本不可重新 ACTIVE；必须创建新 version。

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
- 生成 `artifacts/evidence/BD-T06/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
