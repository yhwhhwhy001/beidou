# 北斗 V5 架构

```
mainnet public data ──► beidou_data ──► beidou_alpha (features → signals → ensemble → portfolio) ──► target weights
                                                                                                        │
demo venue (Binance USDⓈ-M) ◄── beidou_exchange ◄── beidou_live (scheduler → rebalancer → execution → reconciler → reports)
```

| 包 | 职责 | 允许依赖 |
| --- | --- | --- |
| `beidou_shared` | 值类型（Side、InstrumentRules、Position、OrderRequest/Ack）、YAML/env 配置加载 | stdlib、pyyaml |
| `beidou_data` | 官方月度归档下载与校验、REST 补齐、parquet 存储、universe 选择、实盘闭合 K 线 | shared |
| `beidou_alpha` | 特征、信号、集成、组合构建、回测、验证、报告。**纯函数，零 I/O** | numpy、pandas |
| `beidou_exchange` | Binance USDⓈ-M REST：签名、限频、熔断、规则量化、下单/查单/仓位；host allowlist + kill-switch | shared、httpx |
| `beidou_live` | bar 驱动循环：目标权重 → 与真实仓位求差 → 幂等下单 → 对账 → 归因/心跳/日报 | alpha、data、exchange、shared |
| `beidou_cli` | `beidou data|research|live|report` | 全部 |

唯一的架构测试：`tests/architecture/test_import_rules.py`（依赖方向 + import 时无副作用）。

## 关键设计决策

- **D-002** 研究与信号只用 mainnet 公共数据；执行在 demo。demo 成交量是合成的、价格有偏差。
- **D-003** 目标仓位架构：每根闭合 bar 计算目标权重，与交易所真实仓位求差后再平衡；`clientOrderId = bd-<strategy>-<symbol>-<bar_open_ms>` 保证同一 bar 幂等。
- **D-004** 最小护栏只保护实验有效性：波动率目标、gross ≤ 2x、单币 ≤ 15%、日亏 −5% 软停、数据过期禁交易、kill-switch 文件、`beidou live flatten`。
- **D-005** `NO_ACTION` 保持上一仓位（不是归零，更不是按 raw score 建仓）。
- **D-006** 循环重启幂等，交易所仓位是唯一真值；允许 launchd KeepAlive + 退避。
- **D-011** 显著性检验对重叠标签做非重叠采样或 Newey-West 修正；随机信号阴性对照必须不显著。
