# 北斗 V5 架构

```
mainnet public data ──► beidou_data ──► beidou_alpha (features → signals → ensemble → books → portfolio → overlays) ──► target weights
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
- **D-018** 新策略作为独立小书加入（各书独立构建、按 fraction 求和、总书套主书上限）的验收由预登记规则决定（`beidou research book`）；书级 ACCEPT 不替代信号级 PASS。首个用例 flow 空头小书：书级 ACCEPT / 信号级 FAIL；操作者按 D-019 把它作为探针书上线。
- **D-019** 探针书：registry 的 `books:` 声明独立小书（`fraction` = 主书风险预算的比例），策略用 `book` 归属；模型按书独立构建、求和后套主书上限与带（`combine_books`），只有主书时路径逐位不变。`verdict: ACCEPT` 只在非主书且带显式 `probe` 块时被启动检查放行；`beidou_live/probe.py` 按 `attribution.jsonl` 的 30 天归因 P&L 自动停书并持久化，日报在复审日标 REVIEW_DUE。
- **D-020** 验证判定以样本外为主（2026-09-04 操作者决定）：PASS = 走前 OOS Sharpe ≥ 1.0 且 OOS 净收益的 Newey-West t ≥ 2.0，WEAK_PASS = 0.5 / 1.5；折一致性、CPCV 负路径比例、PBO（网格 ≥ 4）、成本 2× 为硬门；DSR 照常报告但不再一票否决（`beidou_alpha/validation/verdict.py`）。
- **D-022** 实盘请求窗口 = `max(profile.history_bars, min_history_bars + 模型在 registry 参数下的 warmup)`（周级 tsmom 为 1,442），超过 1,500 启动即拒绝；`NO_ACTION` 的 hold 以上一周期的 `state.last_contributions` 为种子跨周期持久（D-005 的完整实现），与全历史回测的 forward-fill 同义（E-042）。
- **D-021** 每周期开始先摄入 income：交易性收入按上一周期的贡献归因（份额按 \|贡献\| 归一化）；TRANSFER 类现金流（入金、demo 重置、抵押品兑换）重置日起点与高水位、写入 `cycles.jsonl.external_flows`，日报 drift 跳过该 bar（E-044）。退出层参考价固定为首次入场价，同向加减仓不重锚（E-047）。
- **D-023** 信号自己声明是否消费资金费率历史（`SignalSpec.needs_funding`）；声明为真时 `AlphaModel.targets` 没拿到历史就报错、`LiveEngine.startup` 在行情端口不提供 `funding_history` 时拒绝启动，实盘面板与研究面板由同一份结算费率构成（KILL-027 的结构性关闭）。`beidou live verify`（M-011）用公共数据 + `state.json` 离线重算上一周期的 contributions 并逐币比对；`cycles.jsonl` 的 `gross_before` 改用 `positionRisk` 的仓位求和（账户报文不带 positions 数组时旧口径恒为 0）。
- **D-024** 证据的可复现与可归因：每份 validate 报告记录网格 / 折数 / min_train / purge / 每个配置的全样本 Sharpe，折向量可从报告自身复现（E-049 的教训）；账本按 (param_key, range, symbols) 去重，精确重放只计一次（与 `research book` 一致）；DSR 同时报告 pooled（账本 + 网格离散度）与 noise-null（单个 Sharpe 估计的抽样方差）两个零假设，均为信息性，判定归 D-020；在集成上做决策的报告（`research overlay`）记录 `registry` 指纹——按启用条目经各信号自己的参数对象规范化后的摘要，外加 ensemble 方法与各书 fraction，所以改注释不会变、改参数一定变（P5 那次「证据比结论早过期 15 分钟」靠时间戳才发现）；`research decompose` 把一本书拆成 full / constant_long / sign_only / long_only / short_only / equal_notional，以区分信号与组合构建的贡献（不计入试验账本）。见 `docs/analysis/2026-09-04-round7-strategy-factor-deep-analysis.md`。
- **D-025** 主机时钟是基准（2026-09-04 操作者决定，不校准系统时钟）。因此**恒定的整数个 bar 的偏移是可接受状态而不是故障**：偏移接近整数根 bar 时，循环仍在真实 bar 收盘后不久醒来、交易刚收盘的那根。守的是两件真正有害的事：**对齐误差**（偏移对 interval 取余，决定唤醒时刻是否落在未收盘的 bar 里，超过 `guards.max_bar_alignment_seconds` 告警）与**跳变**（两周期间偏移变化超过半根 bar，会重映射全部标签并可能让 income 水位线倒退——2026-09-04 的 -1023）。`cycles.jsonl.clock` 记录 `skew_ms` / `alignment_ms` / `whole_bars` / `jumped`；日报按 `as_of_ms`（数据自带的 bar）分桶，使一天的报告描述的是真正被交易的那一天。
- **D-026** 构建指纹：`construction_fingerprint` 把组合构建（波动率目标、单币/gross 上限、绝对与相对再平衡带、退出层、回撤节流、杠杆策略、各策略权重）摘要成一个 digest，启动心跳记全量、每周期记 digest。registry 指纹标识*信号*、启动门比对*策略参数*，两者都看不见构建层；2026-09-04 采纳 P10 cell B 时实盘的相对带走到 0.40 而 tsmom 的证据是在 0.25 下验证的，运行记录里没有任何东西说明这一点。它**不拦截**任何配置——构建层与证据分层是否可接受属于操作者决定——只保证一条实盘记录能自我说明。
- **D-027** 判定的第二条件对**选择**敏感（2026-09-04）：PASS 还要求走前 OOS Sharpe ≥ 该策略账本去重后配置数下的期望最大值，零假设取单个 OOS Sharpe 估计的抽样分布而非账本合并离散度（后者两个方向都退化过）。`oos_t_stat` 保留但降格为短样本保护——小时级净收益上它就是 `Sharpe × √年数`（比值 1.001），四年以上不约束。启动门同时比对**组合构建**（`construction_problems`）：无 `portfolio` 段的旧报告跳过，从第一份带该段的报告起，band/半衰期的静默漂移会被拒绝启动。
