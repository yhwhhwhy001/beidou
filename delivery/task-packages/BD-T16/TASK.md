# BD-T16 — 真实 Paper 撮合与成本模型

## 1. 基本信息

- 优先级：**P1**
- 阶段：Phase 2
- 前置依赖：BD-T05, BD-T09, BD-T10
- 目标：Paper 不再直接 ACKED/FILLED，而是通过可重放的撮合、延迟、成本和容量模型生成订单/成交/仓位/账本事件。

## 2. 问题证据

- 当前零写模式可直接本地 ACKED/FILLED。
- 这无法验证排队、部分成交、拒绝和真实成本。

## 3. 实施范围

- 涉及模块：beidou_paper, beidou_strategy/cost, execution contracts
- 预计修改文件：beidou_paper/*, beidou_strategy/state/*cost*, tests/paper/*, tests/parity/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得使用随机合成价格作为 Alpha 证据；随机故障必须固定 seed 并仅用于测试。

## 4. 必须实现

1. PaperExchangeAdapter 实现与 Testnet 相同 Exchange contracts，不含写网络能力。
2. 撮合模型考虑 bid/ask、订单类型、队列/可成交量、延迟、部分成交、拒绝、取消竞争。
3. 成本包括 maker/taker fee、spread、slippage、funding、market impact、容量。
4. 所有模型参数版本化并可校准；输出 model_version 与 uncertainty。
5. 成交事件进入同一 Order/Position/Protection/Ledger/Reconciliation contracts。
6. 同一事件序列可 deterministic replay；Paper 与 Testnet 差异报告可量化。

## 5. 接口与合同

- `PaperExchangeAdapter.submit(command)->Events`
- `CostModel.estimate(context)->CostEstimate`

## 6. 数据迁移

新增 paper_sessions、paper_orders、paper_model_versions、paper_calibration_results。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/paper tests/parity -q
```
```bash
pytest -q -k 'paper and (partial_fill or latency or cost or funding or replay)'
```

## 9. 验收标准

AC-BD-T16-01. Paper 不直接把所有订单标记 FILLED。
AC-BD-T16-02. 手续费、spread、slippage、funding 全部进入账本。
AC-BD-T16-03. 冻结事件 replay 结果一致。
AC-BD-T16-04. 成本压力与容量下降会降低而不是提高净收益。

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
- 生成 `artifacts/evidence/BD-T16/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
