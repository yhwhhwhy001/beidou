# 北斗 V5 架构

```
mainnet public data ──► beidou_data ──► beidou_alpha (features → signals → ensemble → portfolio → overlays) ──► target weights
        │                    │                                                                                       │
        │              pool (daily refresh / point-in-time membership)                                                │
demo venue (Binance USDⓈ-M) ◄── beidou_exchange ◄── beidou_live (scheduler → throttle → exits → guards → rebalancer → margin → execution → reconciler → reports)
```

| 包 | 职责 | 允许依赖 |
| --- | --- | --- |
| `beidou_shared` | 值类型（Side、InstrumentRules、Position、OrderRequest/Ack）、YAML/env 配置加载 | stdlib、pyyaml |
| `beidou_data` | 官方月度归档下载与校验、REST 补齐、parquet 存储、universe 排名滞回、**交易池**（`pool.py`：日线同步、时点成员表、实盘每日刷新）、实盘闭合 K 线 | shared |
| `beidou_alpha` | 特征、信号、集成、组合构建、**overlays**（退出层、回撤节流）、回测、验证、报告。**纯函数，零 I/O** | numpy、pandas |
| `beidou_exchange` | Binance USDⓈ-M REST：签名、限频、熔断、规则量化、下单/查单/仓位、杠杆档位；host allowlist + kill-switch | shared、httpx |
| `beidou_live` | bar 驱动循环：目标权重 → 节流 → 退出层 → 护栏 → 与真实仓位求差 → 参与率/保证金缩放 → 幂等下单 → 对账 → 归因/心跳/日报；每日池刷新与显式平仓 | alpha、data、exchange、shared |
| `beidou_cli` | `beidou data sync|pool|status`、`research backtest|validate|diagnose|correlate|overlay`、`live run|status|flatten|kill-switch`、`report daily` | 全部 |

唯一的架构测试：`tests/architecture/test_import_rules.py`（依赖方向 + import 时无副作用）。

## 关键设计决策

- **D-002** 研究与信号只用 mainnet 公共数据；执行在 demo。demo 成交量是合成的、价格有偏差。
- **D-003** 目标仓位架构：每根闭合 bar 计算目标权重，与交易所真实仓位求差后再平衡；`clientOrderId = bd-<bar_open_ms>-<symbol>` 保证同一 bar 幂等。
- **D-004** 最小护栏只保护实验有效性：波动率目标、gross ≤ 2x、单币 ≤ 15%、日亏 −5% 软停、数据过期禁交易、kill-switch 文件、`beidou live flatten`。
- **D-005** `NO_ACTION` 保持上一仓位（不是归零，更不是按 raw score 建仓）。
- **D-006** 循环重启幂等，交易所仓位是唯一真值；允许 launchd KeepAlive + 退避。
- **D-011** 显著性检验对重叠标签做非重叠采样或 Newey-West 修正；随机信号阴性对照必须不显著。
- **D-012** 退出层（`beidou_alpha/overlays/exits.py`）在 bar 收盘评估，阈值以入场时日波动率为单位（止损 / 移动止损 / 止盈 + 冷却期），回测循环与实盘调用同一个 `exit_step`；实盘以交易所的 `entryPrice` 为参考并把状态持久化到 `state.json`。交易所原生条件单明确不做（见 `docs/analysis/2026-09-03-exits-pool-sizing.md`）。
- **D-013** 研究用时点成员表：`beidou data pool history` 用 2021 年以来所有有归档的 USDT 永续的日线成交量按月重建"当时的 top-15"（`--universe pit`），消除幸存者偏差。
- **D-014** 实盘每日刷新交易池（同一滞回：进入 ≤15 名、离开 >20 名）；被移出的币目标置 0 并 reduce-only 平仓，直到平掉前仍在受管集合里——退出侧永远遍历仓位，不遍历池子。新进入的币沿用 `min_history_bars` 等待历史。
- **D-015** 只有一个风险预算（波动率目标 + gross/单币上限）；自适应项只做乘在整本书上的标量（回撤节流）或下单层的约束（保证金、参与率），绝不叠加多个"比例"。
- **D-016** 交易所杠杆 = `min(max_leverage, 档位上限, ceil(max_gross / margin_cap))`（默认 5x），只改变保证金效率，不改变敞口；下单前按可用保证金按比例缩小加仓单。
- **D-017** 退出层与回撤节流的启用由预先登记的验收规则决定（`beidou research overlay`：OOS MDD 改善且 OOS Sharpe 损失 ≤ 0.10），不满足则默认关闭。
