# BD-T04 — ClosedBar 与点时特征链

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T03
- 目标：建立可重放、无前视、可追溯的数据事实链，保证任何未闭合、陈旧、乱序或缺口数据不能增加风险。

## 2. 问题证据

- 运行时 feed 未使用 ClosedBar 合同。
- freshness 被无条件标记 PASS。
- K 线闭合状态、sequence、revision 与 available_at 未进入策略主链。

## 3. 实施范围

- 涉及模块：beidou_data, beidou_exchange, beidou_strategy
- 预计修改文件：beidou_data/market.py, beidou_data/quality.py, beidou_data/feature_store.py, beidou_data/klines.py, beidou_core/feed.py, tests/data/*, tests/replay/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得用当前时间替代交易所可用时间；不得用 0/默认指标填充缺失特征继续交易。

## 4. 必须实现

1. 统一 RawMarketEvent、ClosedBar、DataQualityAssessment、FeatureVector 合同。
2. ClosedBar 键至少含 venue/symbol/interval/open_time/revision，记录 close_time、available_at、source、sequence、payload_hash、is_closed。
3. 实现 duplicate/out-of-order/gap/revision/stale 检测与 REST 回补；修订记录不可覆盖原始版本。
4. PointInTimeFeatureStore 按 available_at 提供 as-of 查询，输出 input_hash、feature_version、lookback_start/end、dq_tier。
5. 策略输入只接受 DQ PASS；DEGRADED 仅允许策略明确声明降级，BLOCK/UNKNOWN 一律 NO_NEW_RISK。
6. 建立 deterministic replay，冻结数据集多次回放生成相同 event/feature/proposal hash。

## 5. 接口与合同

- `ClosedBarNormalizer.normalize(raw)->ClosedBarResult`
- `DataQualityGate.assess(events)->DQAssessment`
- `FeatureStore.get_as_of(key, ts)->FeatureVector`

## 6. 数据迁移

新增 raw_market_events、closed_bars、data_quality_events、feature_vectors；按时间分区，forward-only migration。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/data tests/replay -q
```
```bash
pytest -q -k 'closed_bar or point_in_time or out_of_order or gap or revision'
```
```bash
python scripts/run_replay_fixture.py --fixture tests/fixtures/market/replay_v1.json
```

## 9. 验收标准

AC-BD-T04-01. 未闭合 bar 永不进入 StrategyKernel。
AC-BD-T04-02. 重复、乱序、缺口、陈旧、revision 均有明确状态和审计。
AC-BD-T04-03. 同一冻结输入 replay hash 100% 一致。
AC-BD-T04-04. DQ BLOCK/UNKNOWN 时风险增加 Proposal 数为 0。

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
- 生成 `artifacts/evidence/BD-T04/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
