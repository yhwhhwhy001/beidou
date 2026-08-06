# BD-T10 — Fill 与 Position 权威链

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T09
- 目标：仓位只能由真实成交事件构建，支持部分成交、反向、reduce-only、单向/双向模式和重启重放。

## 2. 问题证据

- 当前保护管理器内存持仓可能被当作系统仓位。
- 仓位与订单/成交事实未形成唯一持久化投影。

## 3. 实施范围

- 涉及模块：beidou_safety/position, beidou_safety/execution, beidou_exchange
- 预计修改文件：beidou_safety/position/*, beidou_safety/execution/*, migrations/*position*, tests/position/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得从保护单对象、策略信号或交易所快照直接覆盖系统仓位。

## 4. 必须实现

1. 定义 FillEvent 为仓位唯一输入；trade_id + venue 唯一。
2. PositionAggregate 支持加仓、减仓、全平、反向、部分成交、手续费与已实现 PnL。
3. 明确 one-way 与 hedge mode 的 position key；Adapter 启动时发现并锁定账户模式。
4. reduce-only/close-position 不得增加绝对风险；违反时拒绝并告警。
5. position_events append-only；position_projection 可从 fills 完整重建。
6. 重启先 replay，再与交易所独立快照对账；对账完成前不可 ACTIVE。

## 5. 接口与合同

- `PositionAggregate.apply_fill(fill)->PositionTransition`
- `PositionProjection.rebuild(account)->Snapshot`

## 6. 数据迁移

新增 fills、position_events、position_projection；旧内存/SQLite 持仓只作为迁移参考并标记 NOT_VERIFIED。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/position -q
```
```bash
pytest -q -k 'position and (partial or reverse or reduce_only or hedge or replay)'
```

## 9. 验收标准

AC-BD-T10-01. 重复 fill 不重复更新仓位/PnL。
AC-BD-T10-02. 任意 fill 序列 replay 结果确定性一致。
AC-BD-T10-03. reduce-only 永不增加绝对敞口。
AC-BD-T10-04. 数据库重建仓位与独立 Testnet 快照一致或明确 MISMATCHED。

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
- 生成 `artifacts/evidence/BD-T10/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
