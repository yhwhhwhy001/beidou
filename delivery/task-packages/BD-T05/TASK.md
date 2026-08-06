# BD-T05 — Typed Strategy Kernel 切换

## 1. 基本信息

- 优先级：**P0**
- 阶段：Phase 1
- 前置依赖：BD-T04
- 目标：用唯一 Typed Strategy Kernel 替换旧 AlphaGraph，类型层强制 Entry、Filter、Exit、Sizing、Protection 语义。

## 2. 问题证据

- engine.py 仍实例化旧 AlphaGraph。
- Filter 仍输出 LONG/SHORT。
- TypedAlphaGraph 尚未成为 Autopilot 主链。

## 3. 实施范围

- 涉及模块：beidou_strategy/alpha, beidou_core, beidou_research/backtest
- 预计修改文件：beidou_strategy/alpha/contracts.py, beidou_strategy/alpha/typed_graph.py, beidou_strategy/kernel.py, beidou_core/engine.py, tests/strategy/*, tests/parity/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得在本任务调整策略参数追求收益；不得保留旧 Graph 作为第二运行路径。

## 4. 必须实现

1. 定义 EntryProposal、FilterResult(ACCEPT/VETO/DEGRADE)、ExitProposal、SizingDecision、ProtectionPolicy、StrategyProposal。
2. Filter 类型中不存在 direction；Exit 只允许 REDUCE/FLATTEN/CANCEL。
3. 修复 TypedGraph 所有构造、failure policy、hash 和缺失输入问题；缺失关键特征为 BLOCK。
4. Autopilot、Backtest、Paper、Shadow、Testnet 通过同一 StrategyKernel 入口。
5. Proposal hash 绑定 dataset/feature/factor/model/policy/code version。
6. 迁移后删除旧 AlphaGraph 的运行导入；可保留只读迁移适配器一个版本，但不得执行。

## 5. 接口与合同

- `StrategyKernel.evaluate(StrategyContext)->StrategyProposal`
- `ExitPolicy.evaluate(PositionContext)->ExitProposal`

## 6. 数据迁移

策略定义版本升级；旧策略版本标记 LEGACY_DISABLED，不覆盖历史结果。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/strategy tests/parity -q
```
```bash
pytest -q -k 'filter_invariant or exit_invariant or proposal_hash or strategy_parity'
```
```bash
python scripts/check_forbidden_patterns.py --repo . --strategy
```

## 9. 验收标准

AC-BD-T05-01. Filter contract 无 LONG/SHORT 字段。
AC-BD-T05-02. Exit 不能增加绝对风险；属性测试覆盖所有方向组合。
AC-BD-T05-03. Backtest/Paper/Shadow/Testnet 对相同上下文产生相同 Proposal hash。
AC-BD-T05-04. 运行依赖图中不存在旧 AlphaGraph。

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
- 生成 `artifacts/evidence/BD-T05/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
