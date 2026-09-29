# N2–N5 合入前的全新上下文审查（2026-09-29）

审查者：Opus 5.5 子代理，全新上下文，后台运行，2026-09-28T18:59Z 到 19:15Z。被审的是 #259 的第一版提交 `d8c616b9`
（merge-base `b5974838`），下文的行号都是那一版的。作者按审查改过（`e909b17a`、`1c8387e0`），行号已漂。处置写在
`2026-09-29-research-analyst-prompt-vs-beidou.md` 执行记录的「合入前审查查出的、已改」一节。

审查原文在会话里交付，当时没有入库。09-29 复查指出「一高五中五低」无法从仓库核实，于是从会话记录取回，逐字入库。
文中的 `WT` 与 `review-ev/` 是当时的 scratchpad 路径，现已不存在。

---

**总判：** 没有致命问题。拼接、对齐、前视和 verdict 独立性我都核过，是对的。但这个提交会让 source budget 门变红。N2 块有三处标签会误导读者。接线部分没有测试覆盖。

以下 `WT` = `/private/tmp/claude-501/-Users-maguannan-beidou/0d60bcb1-c1ac-4af6-9810-daaaffcf2b83/scratchpad/wt-ev`，只读脚本在同目录的 `review-ev/` 下。

## 已核无误（重点 1、2、5）

- **拼接**：`stitch_check.py` 在合成数据上逐字复刻了 `research_validate_cmd.py:538-540` 那三行。数据有 3 个配置，预热长度各不同，带 guards，五个 fold 的选择分别是 a=1/a=1/a=1/a=2/a=-1。
  - `oos_net.sum(axis=1)` 与 `wf.oos_returns` 索引相同，按 uint64 逐位相等。
  - `oos_weights` 乘同一根 bar 的资产收益等于 gross；乘下一根不等。
  - 真实报告里 `concentration.oos.sharpe` 与 `walk_forward.oos_sharpe` 的 16 位完全相同。
- **持仓口径**：`weights` 是 guards 之后执行的持仓，与 `net` 在同一根 bar 上（`backtest.py:262,279-282,299`）。
- **前视**：成员表只用刷新日之前的数据选（`beidou_data/pool.py:100,125`）。篮子与书用同一个 `execution`。变异「用上一根的敞口」「signal_state 读下一根」都被测试抓住了。
- **nw_ols**：带宽夹紧、Bartlett 权重、t 的定义与 `nw_covariance` 逐行一致（`basket.py:43-54` 对 `benchmark.py:97-107`）。
- **verdict**：`decide` 只读 walk_forward、multiple_testing、cpcv、cost_stress、oos_selection 里原有的键（`verdict.py:108-159`）。新键一个都不读。邻域读数也没有任何门在读。
- **CAGR 年数**：49,600 根正好是 2021-01-31 01:00 到 2026-09-28 16:00 的整点数，没有缺口。bar 数 / 8760 在这次 run 上是对的。
- **读数复现**：从 `.beidou/data` 只读重算（`basket_check.py`）。篮子 −89.78%、MDD −97.36%，与报告逐位一致；BTC 读数 160.88% 也对上。

## 高

**H1. source budget ratchet 会红，CI 过不去。**
- 证据：在 WT 跑 `tests/architecture/test_source_budget.py`，报 `{'beidou_alpha': (11254, 11179), 'beidou_cli': (8868, 8804)}`。
- 按 git 对象计：merge-base `b5974838` 时是 11,067 / 8,769。本提交加了 +187 / +99，两个顶都没动（`test_source_budget.py:43,53`）。
- origin/main 已经到 `29a7855c`（#256 给 alpha 加了 79 行）。合并后 alpha 会是 11,333。
- 修法：先 merge 最新 main 再实测。在同一个提交里把两个顶抬到「实测 + `headroom_policy`」，理由写进 `docs/SOURCE_BUDGET_LOG.md`。

## 中

**M1. static 宇宙下，篮子照样标成 point-in-time。**
- `--universe` 的默认值是 static（`research_options.py:59`）。此时 membership 是 None，篮子等于全部面板符号，也就是 `backtest.py:405-411` 自己说的 hindsight。
- 但 md 标签写死了 "point-in-time"（`research_report.py:214`），文档串也把它当规则写（`basket.py:9`）。
- `research backtest` 已经用 `benchmark_basket(membership)` 给篮子命名（`research_backtest_cmd.py:163`），validate 的新块没有用。
- 修法：块里加 `"basket": benchmark_basket(membership)`，并放进标签。

**M2. 对冲书的 N2 读数没有意义。**
- hedged 时，书在 `spread_panel` 上定价（`research_validate_cmd.py:300-303`）。权重是非负的价差名义，合计 2/3（`hedged.py:117-119`）。
- 篮子和 BTC 却取自永续面板（`:533,541`），而 `exposure` 就是价差名义之和（`basket.py:98`）。
- 结果：signal state 会报全多头接近 100%、net 约 0.67。条件回归把一个市场中性的价差当成了 0.67 倍净多。`basket.py:13` 的「全多头 bar 上书就是恒定多头」对它不成立。
- 修法：hedged 时 `against_basket` 写 n/a 并说明原因。concentration 可以保留。

**M3. 研究侧的条件 beta 吸收了信号自己的择时，块里没有说。**
- D-045 用条件回归，是为了剥掉操作者改 `vol_target` 带来的敞口变化（`benchmark.py:19-24`）。回测里，敞口完全是信号自己选的。
- 验收读数：常数 alpha 0.351、条件 alpha 0.244 bps/bar。差额 0.107，约占书均值的三成，约 9%/年（算术）。
- 这 0.107 几乎全是 cov(敞口, 篮子)，也就是择时。因为 mean(e)·mean(m) ≈ (−0.013)×(−0.075 bps) ≈ 0.001 bps。
- md 那一行只写「(D-045)」（`research_report.py:217`），"beta 0.73 (t 56.1)" 很容易读成「七成是市场」。
- `basket.py:11-12` 的理由在回测里说反了：常数回归里被记成 alpha 的那部分敞口，本来就是信号的收益。
- 修法：加一行 basis，写明两点：
  - 敞口是书自己的净敞口，条件拟合把择时算进了 beta。
  - 「是不是被动市场敞口」要看常数拟合（−0.02）。

**M4. 测试没盖住接线，独立性测试有漏洞。**
我在副本里做了 11 个变异，测试只抓住 3 个；副本已删。
- **接线零覆盖**：tests 里除新文件外，没有任何文件引用 `against_basket`、`concentration`、`oos_cagr`。把 `:538` 的逐折 key 换成 `best_key`，不会有测试变红。验收 run 的 `selection_consistent=True`，所以也分辨不出这种错误。
- **verdict 测试只盖它自己设了值的子键**：让 `decide` 读以下四个键，变异全部存活——
  - `against_basket.conditional.alpha_t`
  - `signal_state.all_long_share`
  - `book.sharpe`
  - `concentration.full_sample.top1_share`

  只有读 `constant.beta` 的那个被抓住。
- **concentration**：两个测试把同一帧同时当 full 和 oos 传入（test 文件 `:83,92`）。对调两者、或把留一改在全样本上算，变异都存活。
- **估计量对齐测试**：只在 n=800 上比，碰不到夹紧。把研究侧 `MAX_LAG_SHARE` 改成 0.5，变异存活。
- 修法：
  - 加一条夹具级测试。让各 fold 选出不同配置，断言 `against_basket.bars == oos_bars`、`concentration.oos.sharpe` 与 `oos_sharpe` 逐位相等。
  - verdict 测试改成把整块替换成敌意值，再加一条源码层断言：`decide` 不引用这些键。
  - concentration 测试给 full 和 oos 传不同的帧。
  - 估计量测试加一个 n<192 的样本，并断言两边常量相等。

**M5. N5 只落了一半。**
- K-03 的处置是「字段名带 full_sample，正文指向 k 决策的读数」（分析文档 `:259,:370`）。
- 实现只在代码注释里指过去（`research_validate_cmd.py:611-612`）。md 里只印了一行 `cagr_full_sample 0.3568`，没有指针；k=0.175 的诚实漂移 CAGR 中位是 22.5%/24.3%。
- `payoff_ratio`（`:615`）的名字不带 bar 级口径，与常见的逐笔 payoff 同名。
- 修法：md 加一行指针；字段改名 `payoff_ratio_bar`。

## 低

- **L1. 与实盘 D-045 的口径差**：数值本身没错，但不能直接和 `report beta` 比。
  - 实盘回归对数收益（`benchmark.py:387-389`），这里回归简单收益（`basket.py:99-107`）。
  - 实盘不足 48 根会拒绝（`:390-391`）；这里 n=10 也给 t（实测 beta_t 41，nw_lags 2）。
  - 这里没有 `nw_covers_intended_horizon`。
  - 实盘的 signal_state 数信号贡献 ±1（`:200-229`）；这里数执行后的持仓符号，分母还含平仓 bar。
  - 修法：块里加 basis，写明这几处。
- **L2. 篮子 −89.8% 是真的，但对再平衡频率敏感，标签没写。**
  - D-045 口径（close_to_close、两端都是成员）是 −88.53%；按日再平衡是 −79.5%。
  - Sharpe −0.08 说明算术均值接近 0。这个亏损主要是约 86% 年化波动下的拖累。
  - 修法：标签加 "rebalanced every bar"，并提示读者比 Sharpe，不比复利收益。
  - BTC 逐 bar 复利 160.88%，真实买入持有 161.16%，差 0.28pp，可以不改。
- **L3.** `cagr([0.5, 0.1], 8760)` 会抛 `OverflowError`（`metrics.py:154`，Python float 幂运算溢出）。真实 run 碰不到，建议改成用对数计算。
- **L4. 集中度的单位**：
  - `net_pnl_total +1.6065` 是逐 bar 简单收益之和，没有复利。它紧挨着 +362.8% 的复利收益（md `:220` 对 `:208`）。
  - 份额可以超过 100%（实测 500%），`top` 只列正贡献，这两点都没写。
  - 留一没有重放 guards 和 exits。「understates」（`concentration.py:8-9`）的方向没有证据支撑。
- **L5. N4 偏离了规格**：分析文档 `:258` 要的是「best_params 的数值维度」，实现是「默认网格 ∪ 被搜维度」（`research_validate_cmd.py:129-136`）。
  - 被搜的列表型维度（如 `horizons`）会被 `stability.py:36-40` 静默跳过。
  - 如果选中值是 0（`crowding_window=0` 表示关），会被扰动到 −1 和 +1。
  - 修法：在 PR 里写明这个偏差，或者在邻域块里列出不可扰动的维度。

## 我没核的

- 按要求没跑 `research validate`。hedged 和 static 两条路径的实际输出是读代码推断的，不是跑出来的。
- 没跑全量 pytest。只跑了新测试文件（8 passed）、source budget 两个测试、ruff format/check 和 mypy（都是绿的）。运行时没往 WT 写缓存，WT 仍干净。
- 没做分析文档 `:384` 要求的「同一快照、main 对分支」字节比对。
- 对数收益与简单收益的 alpha 差，没在真实书上量过：报告里没有逐 bar 序列。
- 没核 mined 和 pair 类信号的 combos 形状，也没量邻域多出的回测耗时。
