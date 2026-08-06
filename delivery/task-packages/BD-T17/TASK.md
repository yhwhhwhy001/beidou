# BD-T17 — 因子统计与组合优化

## 1. 基本信息

- 优先级：**P1**
- 阶段：Phase 2
- 前置依赖：BD-T04, BD-T06, BD-T16
- 目标：以冻结、点时可得数据建立可证伪的因子、策略和组合研究证据，不承诺盈利。

## 2. 问题证据

- 现有因子评估实现较基础，部分边际 Sharpe 为经验近似。
- 晋级需要完整统计、成本、容量和稳健性证据。

## 3. 实施范围

- 涉及模块：beidou_research, beidou_strategy/portfolio, beidou_reporting
- 预计修改文件：beidou_research/evaluation/*, beidou_research/mining/*, beidou_strategy/portfolio/*, tests/research/*

### 禁止修改范围

- 不得启用 Mainnet、CANARY、LIVE 或 PRODUCTION 写交易能力。
- 不得使用 Mock/Fake/Stub/Placeholder 代替本任务要求的生产实现；测试替身只能用于明确的单元测试边界。
- 不得删除测试、降低断言强度、增加无理由 skip/xfail、扩大 lint/type ignore 或降低覆盖率阈值。
- 不得吞异常、把 UNKNOWN/ERROR 转为空余额/空仓位/空订单或健康状态。
- 不得在业务模块新增 Binance URL、原始 /fapi/ 端点、urllib/requests/httpx 直接调用。
- 不得修改与任务无关的策略参数以改善回测结果。
- 不得将提交说明、README 或文档声明作为任务通过证据。
- 不得选择性删除亏损样本、缩短区间、忽略成本、复用测试集调参或声称保证盈利。

## 4. 必须实现

1. 实现 IC/RankIC/ICIR、Newey-West、Bootstrap、稳定性、单调性和时间衰减。
2. 实现 multiple testing correction、Deflated Sharpe、PBO、Purged WFO、embargo、CPCV、参数邻域。
3. 按 regime/symbol/liquidity/funding 分层，评估成本压力、容量和相关性。
4. 组合优化输入含 expected return uncertainty、covariance shrinkage、turnover/cost、concentration、tail risk。
5. 杠杆与仓位输出目标名义敞口、保证金、杠杆上限、清算距离、单笔/组合风险和容量。
6. 所有报告绑定 dataset manifest、commit、policy、seed、环境、命令和 artifact hashes。

## 5. 接口与合同

- `FactorEvaluationPipeline.run(manifest, factor_version)->EvidenceBundle`
- `PortfolioOptimizer.optimize(snapshot)->PortfolioDecision`

## 6. 数据迁移

新增 research_runs、statistical_evidence、capacity_evidence、portfolio_decisions。

## 7. 异常与边界

- 所有外部依赖失败必须产生类型化结果和 correlation_id。
- UNKNOWN/STALE/ERROR 默认阻断风险增加，不得转换为空状态。
- 重复、乱序、超时、崩溃和重启必须有明确行为。
- 并发冲突必须依靠数据库约束、版本或 fencing 解决，不依赖进程内锁作为唯一保障。

## 8. 测试命令

```bash
pytest tests/research tests/portfolio -q
```
```bash
pytest -q -k 'newey or bootstrap or purged or cpcv or pbo or deflated or capacity'
```

## 9. 验收标准

AC-BD-T17-01. 任何晋级报告可由冻结数据和 commit 复现。
AC-BD-T17-02. 训练、验证、测试区间无泄漏且有 embargo。
AC-BD-T17-03. 成本/容量压力下结果符合单调风险预期。
AC-BD-T17-04. 未达到统计与风险门槛时决策为 REJECT/SUSPEND，不包装为成功。

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
- 生成 `artifacts/evidence/BD-T17/manifest.json`；
- 独立 commit；
- 不自动开始下一任务，先验证依赖 Gate。
