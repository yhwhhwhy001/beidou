# V3 研究与 Alpha 证据门

## 第一性原理

持续盈利不是代码属性，而是一个必须在未知数据、可执行成本和多重试验下仍可证伪的统计命题。可靠运行只能证明系统按规则执行，不能证明规则有正期望。因此研究门与 SRE/执行门并行，任何一门失败都不能晋级。

## 本轮已经接入的门禁

### 时间因果

- REST K 线按 `close_time <= now` 过滤；实时生成器默认只返回已闭合 bar。
- Miner 缺少显式 `is_closed` 元数据时记为 `NOT_VERIFIABLE`，不能把缺失当作 `True`。
- 数据集必须提供外部 `dataset_manifest_hash`；运行器不再把首/中/尾采样哈希当成完整来源证明。
- 预测样本与标签结束时间、symbol 和可用时间绑定；缺失因果字段会阻断 bundle。

### 样本外与多重检验

- 候选通过真实 `PurgedWFO`，记录 train/test IC、Sharpe、fold 数和失败原因。
- 候选通过 `CPCVEvaluator`，记录 path 数和组合指标。
- 候选 p 值进入统一多重检验校正，记录 DSR、PBO 和 BH-FDR 结果。
- WFO、CPCV、multiple-testing、数据 manifest 任一为 UNKNOWN/FAIL，bundle 不是 PASS。

这些代码路径和测试证明“门禁会阻断无证据结果”，不证明当前仓库已有可晋级 Alpha；当前数据 manifest、sealed OOS、成本/容量和跨 regime 结果仍不足。

## 晋级阈值（冻结后不得为适应结果改阈值）

| 阶段 | 必须证明 | 失败动作 |
|---|---|---|
| Research | 100% PIT key、闭合 bar、无重复/乱序；至少 5 个 Purged WFO folds、≥20 条 CPCV paths；DSR≥0.95、PBO≤0.20、BH-FDR q≤0.05 | 所有下游证据作废，回到 Research |
| Net-cost/Capacity | fee、spread、slippage、funding、impact 全计入；sealed OOS 净期望 95% 下界 >0；压力成本后仍为正；容量和参与率满足预注册上限 | 禁止扩容/晋级 |
| Parity | Backtest/Paper/Shadow/Testnet 的 feature→proposal→risk→target 事件链 100% 可解释、未知差异为 0 | 停止运行并重建 kernel |
| Paper/Shadow | 真实撮合、真实拒绝/部分成交/延迟、独立预测与实际成本；≥7 日 shadow、≥50 有效事件后才可申请 Testnet | 重置窗口，不继承历史收益 |
| Testnet | 当前 commit 的 16 项 G5 全部真实执行；再从零开始 ≥30 日、≥200 周期、无 P0/孤儿单/裸仓/对账差异 | G7 窗口归零，证书标记 NOT_VERIFIABLE |

任何回测、Paper 或 Testnet PnL 都不能单独证明未来盈利；Mainnet 不在本轮授权范围内。

## 仍需实施的研究任务

1. 建立含版本、可用时间、修订号、交易所事件序列和完整内容哈希的数据 manifest。
2. 消除在线预测/标签全局数组尾部对齐，改用 `(venue, symbol, interval, observation_time, horizon, revision)` 复合键持久化。
3. 将生产 StrategyKernel 接入 Backtest/Paper/Shadow/Testnet，hash 覆盖数据、特征、因子、风险、策略版本和配置，而不只覆盖方向/强度。
4. 用可重放订单事件校准 fill/cost/funding/容量模型；禁止同源 spread 同时作为 estimated 与 actual。
5. 将 FactorLifecycle、promotion 和 challenger 证据持久化并 fail-closed；缺失 performance 不得自动 ACTIVE。
