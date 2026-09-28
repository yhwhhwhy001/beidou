# 「AI 交易研究分析师」prompt 模板对照北斗：清点与优化方案

2026-09-29。一次清点加一份带价方案，不是裁定，也不是预登记。行号以 `99551815`（#241 合入）为准。
本文不改代码、不花 ledger、不碰实盘。

```text
Reading Check：本批需求的共同背景为「操作者第三次拿一份外部量化 prompt 对照北斗，问还有什么可以补」；
最高风险预设为「这份模板对北斗还有增量」——若不成立（前两轮已把清单补完），本文只剩状态更新与顺带发现。
Interaction：Yellow（🟢🟢🟡🟡🟢🟢）｜ 等级：Triage ｜ 当前决策上限：N/A（未完成分析）｜ 输出状态：DONE_WITH_CONCERNS ｜ 外部动作授权：无
```

## 它接的是哪个问题

操作者给了一张图：一份英文 prompt 模板。角色是「elite AI trading research analyst」，目标是为某个市场
找到并验证 3–10 个策略，交付排名清单。模板分 12 段：role、objective、context、research_process、
detailed_steps、analysis_requirements、evaluation_criteria、rules、risk_management、tools_and_data、
output_format、final_check。原文不入库：仓库公开，来源与版权不明。下文用中文转述，段名照抄英文。

这是第三份外部清单。前两份的记录：

- 09-23：@TechElyra 的 10 条 prompt，100 项逐项清点，已有 70、部分 27、空白 0、不适用 3
  （`docs/analysis/2026-09-23-external-prompt-checklist-vs-beidou.md`），随后 G1–G10 全部合入
  （`docs/analysis/2026-09-23-optimization-plan-from-external-checklist.md`）。
- 09-25：第二轮，拆监控层、第二批补缺、预登记第九项、10-13 准备
  （`docs/analysis/2026-09-25-external-checklist-round-two.md`）。8.10、9.4、9.6 三项同日也做了。

两轮之后，清单能补的仪器基本补完。所以本文不重复清点那 100 项。它只做两件事：

1. 把这份模板的每条要求映射到前两轮判过的项，能对上的写项号与 PR，不再展开。
2. 对映射不上的条目做新的核查，每处「缺」都做过反向搜索，搜索词在文末。

本文的对照由五个只读子代理分头核查，我逐条抽核了引用的行号与数字。

## 判据

沿用 09-23 的四档：

| 判定 | 意思 |
| --- | --- |
| 已有 | 仓库里有机制回答这个问题：代码、测试，或写明的决策。写明「决定不做」并给了理由的也算 |
| 部分 | 有机制，但漏了这一项点名的某一块；或这一项要常设机制，仓库只有一次性测量 |
| 空白 | 问题适用于北斗，仓库里找不到答案，也找不到「不做」的决定 |
| 不适用 | 这一项预设了北斗没有的业务 |

判「不适用」之前先找加密永续上的对应物。板块对应 universe 的成员，财报对应上新与重新计价，
机构持仓对应大户多空比。

## 结论

| | 已有 | 部分 | 空白 | 不适用 | 合计 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 模板 80 条 | 57 | 22 | 1 | 0 | 80 |

80 条由下文七张表数出。**唯一的空白是「网络检索」**：仓库既没有接入，也没有写过「不做」。
「新闻与事件」判部分：事件有对应物（#157），新闻没有决策记录。没有一条判「不适用」：
模板的板块、财报、期权 flow 在加密永续上都找得到对应物或写明的决定。

22 条「部分」里，6 条是前两轮写明的取舍：微观结构不读盘口、冲击模型默认关、新读数只报告不告警、
真钱前的成本校准放在准入设计。其余 16 条是这份模板新问出来的，去重后是 9 个缺口（下文 N1–N9），
集中在四处；另加前两轮留下、等冻结结束的 G11（N10）。

### 一、尾部尺子自 09-27 起是瞎的

日报「Tail beside the sigma ruler (G4)」从 09-27 切到 k=0.175 起印 `status n/a`
（`reports/daily/2026-09-28.md:447-452`）。VaR / ES 常量钉在 k=0.60
（`beidou_live/report_risk.py:728-734`），k 不等就拒算（`:758-760`）。五段崩盘窗口也没在 k=0.175 上重放
（`config/alpha_registry.yaml:227-229`）。09-25 的 G11 草稿把这件事写成了实盘失效方式第 3 种
「尺子没跟着换」（`docs/analysis/2026-09-25-october-13-readiness.md`§4 第 9 项）。它发生了，
只是发生在换 k 而不是加上限的那次。

### 二、证据文件不比基准，也不看按币集中度

`research validate` 的报告有 21 节，没有一节把书和它自己的基准放在一起。
`benchmark_returns` 在 validate 里只用来切 regime 标签（`beidou_cli/research_validate_cmd.py:512-517`）。
基准对照在 `research decompose` 有（`beidou_alpha/validation/decompose.py:87-129`），在 `research backtest`
有三个数（`beidou_cli/research_backtest_cmd.py:157-164`），在实盘有 D-045。但 registry 引用的证据文件
本身不回答「书比等权篮子好多少」。BTC 买入持有全仓零命中。

按币集中度同样：`per_symbol_summary` 在回测结果里（`beidou_alpha/backtest.py:213`），只有 `research backtest`
打印它（`research_backtest_cmd.py:165`）。validate 没有任何「不依赖单一资产」的读数。仓库自己记过这条
失效方式：flow 的 edge 来自后来离池的名字（`config/alpha_registry.yaml:457-461`）。

### 三、邻格探针扰动的不是被选的那个维度

`parameter_neighborhood` 的数值维度取自 `DEFAULT_GRIDS`（`beidou_cli/research_validate_cmd.py:407-409`），
不是本次 `--grid`。现引报告只跑了 `crowding_window` 两格，恰恰是这一维没被扰动；`vol_window` ±10% 的
Sharpe 与 base 完全相同。读数只报不判、不计 ledger（`beidou_cli/research_ledger_io.py:262-263`），
所以修它不动门、不动账。

### 四、策略数量是门的产物，不是目标

模板要 3–10 个根本不同的策略。仓库有 11 个手写信号（`beidou_alpha/signals/__init__.py:23-140`），
约 6 个机制族；9 个建成并判负，各带 reopen 条件；在跑 2 本。前向板 2 个 OBSERVING。F4「缺一本低相关的书」
仍是 UNKNOWN（`docs/RESEARCH_LOG.md:13744-13746`）。唯一写好预登记草稿、等冻结结束的新族是 G12 低波
（10-13 文档 §5）。冻结 09-27 已结束（`docs/RESEARCH_LOG.md:17392`），它可以批了。

## 逐项对照

### role 与 objective

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 量化分析与严格回测 | walk-forward、CPCV、D-028 去偏门、预登记 | 已有 | `beidou_alpha/validation/` |
| 市场微观结构 | 逐单 TCA、参与率截单、取整；盘口不读，选池不做深度打分是写明的决定 | 部分 | 09-23 清点 5.7；#139 |
| 宏观理解 | 决策：不做。宏观数据 09-10 建、09-16 撤，无读者 | 已有 | `docs/ARCHITECTURE.md:77` |
| 怀疑、数据驱动、可部署 | 预登记九项、对抗审查惯例、分析校准表、启动门 | 已有 | `docs/PREREGISTRATION.md`；`docs/analysis/analysis-calibration.md` |
| 3–10 个高质量策略 | 11 个信号族、在跑 2 本、新存活 0，见结论四 | 部分 | `beidou_alpha/signals/__init__.py:23-140` |
| edge 有证据、非曲线拟合 | D-020 判定、D-028 门、DSR、PBO、阴性对照 | 已有 | `beidou_alpha/validation/verdict.py:106-168` |
| 带排名的最终清单 | 决策：不排名。晋级先来先到，前向板写明「不要当排行榜读」。候选并排一览没有产物 | 部分 | `beidou_governance/policy.py:208-215`；`beidou_alpha/validation/forward_board.py:452-455` |

### context 与 tools_and_data

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 实时与历史价格 | 永续 1h/1d、现货 1h、资金费、6 列 metrics；实盘每根 bar 走 REST | 已有 | `beidou_data/sync.py:47`；`beidou_data/live_feed.py:67-93` |
| 成交量 | K 线带 quote_volume 与 taker 两列 | 已有 | `beidou_data/binance_public.py:19-31` |
| 期权数据与 flow | 决策：块 0 判 Out，另立项目 | 已有 | `docs/analysis/2026-09-08-autonomous-governance-51-strategies-plan.md:30,144` |
| 基本面 | 加密对应物是链上：09-16 撤出，无读者 | 已有 | `docs/ARCHITECTURE.md:77` |
| 宏观（利率、通胀） | 同上，撤出 | 已有 | 同上 |
| 新闻与事件 | 事件：#157 第一期，读自有归档，只报告。新闻：无接入，无决策 | 部分 | `beidou_live/report_events.py:1-22` |
| 网络检索 | 无接入，无决策 | 空白 | 反向搜索见文末 |
| 回测框架、代码执行 | 全在仓库里 | 已有 | `docs/ARCHITECTURE.md:6` |

### research_process 与 detailed_steps

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 市场概览：regime | 作为驱动判负（#47）；历史 regime 三分位在 validate 只报不判（G5）；**日报没有「当前 regime」一节** | 部分 | `governance/reopen.yaml:82-94`；`beidou_alpha/validation/stability.py:173-198` |
| 市场概览：叙事 | 决策：情绪类数据不可得、不可核时点（#30） | 已有 | `governance/reopen.yaml:145-152` |
| 市场概览：资金流 | 对应物 taker 流在跑（flow）；现货主动买卖零 ledger 看过，NO-GO | 已有 | `docs/RESEARCH_LOG.md:17870` |
| 市场概览：机构持仓 | 对应物大户多空比，零 ledger 看过，NO-GO | 已有 | 同上 |
| 市场概览：贵与便宜 | 无估值。对应物 carry、basis 判负 | 已有 | `governance/reopen.yaml:96-105,164-178` |
| 机会扫描：异常强弱 | xsmom、residual 测过；`research diagnose` 的 IC | 已有 | `beidou_cli/research_diagnose_cmd.py:1-4` |
| 机会扫描：催化剂 | 上新事件研究判负；#157 只报告；第二期只有草稿 | 部分 | `docs/analysis/2026-09-25-event-risk-rule-prereg-draft.md:3` |
| 机会扫描：量化信号 | 动量、成交量、相对强弱都测过；期权 flow 见上 | 已有 | 09-23 清点 #4 |
| 策略设计：根本不同的机制 | 时序趋势 3、横截面 2、均值回归与配对 2、订单流 1、资金费 2、择时 1；事件驱动与基本面无信号模块 | 部分 | 结论四 |
| 策略设计：thesis 与 edge | 预登记第 1 项要求写「为什么」；代码里只有 tsmom 写了谁付钱 | 已有 | `docs/PREREGISTRATION.md:13-15`；`beidou_alpha/signals/tsmom.py:3-38` |
| 策略设计：不预设谁最好 | 判据先写后跑；晋级不按 Sharpe 排队 | 已有 | `beidou_governance/lifecycle.py:118-130` |
| 实现：干净代码 | source budget ratchet、59k 行测试 | 已有 | `tests/architecture/test_source_budget.py` |
| 回测：现实假设 | 7 bps 平收加实际资金费；冲击模型实现但默认关，多条路径不带；参与率只计量不改仓；真钱前的校准写在准入设计 | 部分 | `beidou_alpha/backtest.py:52,104-106,129-137`；`docs/MAINNET_READINESS.md`§6 |
| 回测：样本内与样本外 | 扩张训练窗 5 fold、CPCV 15 条路径 | 已有 | `beidou_alpha/validation/walk_forward.py:43-75` |
| 回测：完整绩效 | 有 Sharpe、Sortino、MDD、skew、kurtosis、换手、m*；**缺 CAGR、Calmar；胜率只有 bar 级** | 部分 | `beidou_alpha/validation/metrics.py:104-191` |
| 评估：稳健指标、样本外 | 判定以样本外为主 | 已有 | `beidou_alpha/validation/verdict.py:41-54` |
| 迭代：改进或淘汰 | 七道淘汰机制；「改进」按预登记只许新信息、不许新网格 | 已有 | 下表「淘汰机制」 |
| 迭代：参数敏感性 | `parameter_neighborhood` ±10% 只报不判；**扰动的是默认网格的维度**，见结论三 | 部分 | `beidou_cli/research_validate_cmd.py:407-409` |
| 迭代：walk-forward | 同上样本外 | 已有 | 同上 |
| 迭代：regime 依赖 | G5 三分位、时间四等分，只报不判 | 已有 | #104 |
| 迭代：压力测试 | 成本 ×1.5/×2、滑点网格、崩盘窗口；**窗口与 VaR/ES 停在 k=0.60**，见结论一 | 部分 | `beidou_live/report_risk.py:728-760` |
| 最终选择 | registry ADOPTIONS、启动门、probe 放行（D-029） | 已有 | `beidou_alpha/registry.py:384-481` |

淘汰机制一览（子代理核过，行号在各文件）：validate 判定（`verdict.py`）、书级判定与相关闸（`research_book_eval.py:174-186`、
`lifecycle.py:180-189`）、family gate 每日重算（`family_gate.py:95-221`）、前向板出口（`forward_board.py:71-99`）、
衰减规则（`report_decay.py:288-355`）、probe 止损（`probe.py:168-268`）、reopen.yaml 23 条。

### analysis_requirements 与 evaluation_criteria

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 策略细节：universe、进出场、仓位、频率 | registry 参数、预登记第 3 项协议 | 已有 | `config/alpha_registry.yaml` |
| 指标：总收益、CAGR、Sharpe、Sortino、MDD、胜率、profit factor | 缺 CAGR、Calmar、逐笔胜率、profit factor | 部分 | 结论二 |
| 对基准比较 | validate 证据文件无基准块；decompose 与实盘 D-045 有 | 部分 | 结论二 |
| 风险分析与稳健性 | 见上表 | 已有 | — |
| 代码实现 | 全在仓库 | 已有 | — |
| 扣成本跑赢基准 | validate 不比；D-045 显示 alpha 的 t 不到 1 | 部分 | `docs/analysis/2026-09-25-october-13-readiness.md`§4 基线 |
| Sharpe > 1.0 | 更严：D-020 PASS 线 1.0 加 D-028 门，N=341 时 1.57 | 已有 | `beidou_alpha/validation/multiple_testing.py:370-448` |
| 回撤 < 30% | 决策：回撤由 k 与预算管，不设在信号上。操作者预算是可动用 USDT 的 −70% | 已有 | `beidou_live/risk_budget.py:3-9` |
| 跨时期与 regime | fold 一致性 ≥ 0.6 是硬门；regime 与时间切分只报 | 已有 | `verdict.py:106-168` |
| 不依赖单一参数 | PBO（≥ 4 格）、D-043 封顶；邻格只报 | 已有 | 同上 |
| 不依赖单一资产 | 实盘单币 15%；**研究侧无按币集中度读数** | 部分 | 结论二 |
| 经济逻辑 | 预登记第 1 项 | 已有 | `docs/PREREGISTRATION.md:13-15` |
| 可实施 | 成本 ×2 ≥ 0 是硬门；参与率只在实盘；冲击待校准 | 部分 | `config/live.demo.yaml:48`；`docs/MAINNET_READINESS.md`§6 |

「Sharpe > 1、回撤 < 30%」这两条固定阈值比北斗的门松。D-028 的门随 ledger 的 N 上升；现引报告 N=341
时门 1.5733，真 Sharpe 1.0 的检出力 9.4%，真 1.5 时 43.3%（`reports/research/tsmom-validation-20260925T143836Z.json`）。
回撤那条反过来：k=0.175 下主书自助 q95 回撤约 −36%（总权益口径，`docs/RESEARCH_LOG.md:18126`），按模板会不合格。
那是操作者 09-14 定的预算，不是缺口。

### rules 与 final_check

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 最新最可靠的数据 | 归档每晚同步，滞后 1–24 根并印在日报；pool lag 告警 | 已有 | `deploy/com.beidou.data.plist:12-18`；#120 |
| 怀疑、避免过拟合 | 预登记、ledger 计费、D-028 | 已有 | — |
| 计入成本 | 每份报告两套数，成本超毛利 40% 标红 | 已有 | `beidou_cli/research_report.py:47-54` |
| 分开事实与解释、标注假设 | RESEARCH_LOG 的「推理」标注惯例；分析文档的 E 级 | 已有 | 本文亦照此 |
| 代码、图表、数据 | 决策：不出图（D-047），每个数带出处 | 已有 | `docs/ARCHITECTURE.md:150-154` |
| 无回测不声称 | 预登记先于报告由状态机检查 | 已有 | `beidou_governance/lifecycle.py:169` |
| 不用社交情绪 | #30 不可得 | 已有 | `governance/reopen.yaml:145-152` |
| 诚实说明局限 | 每份分析有「局限」「拿不准」；校准表记错在哪 | 已有 | `docs/analysis/analysis-calibration.md` |
| 我漏了什么 | 对抗审查由全新上下文子代理做；是约定，没有代码门 | 已有 | 09-23 清点 D.2 |
| 假设弱在哪 | Assumption Register、E 级 | 已有 | — |
| edge 真的还是过拟合 | D-028、DSR、PBO、随机信号阴性对照 | 已有 | `multiple_testing.py:181` |
| 考虑了不同 regime | G5 | 已有 | #104 |
| 什么证据会推翻 | 证伪线、预登记第 6 项、reopen 条件 | 已有 | `CONTEXT.md:196-203` |
| 稳健且现实 | 成本与滑点压力、崩盘窗口 | 已有 | — |
| 什么会让我改变想法 | reopen.yaml 23 条重开条件；M-G05 人机分歧率 | 已有 | `beidou_governance/verdicts.py:185-226` |

### risk_management

| 条目 | 驱动动作的 | 只报告的 | 判定 |
| --- | --- | --- | --- |
| 成本与滑点 | 无交易带压小单 | M-Q08、TCA、滑点超 4 bps 告警 | 已有 |
| 仓位与每笔风险 | vol target 0.175、单币 15%、gross 2 × USDT（#237）、日亏 −5% 只减不加 | — | 已有 |
| 流动性与冲击 | 参与率 2% 截单、取整、保证金缩单 | #141 平仓流动性；冲击模型未校准 | 部分 |
| 相关性与分散 | EWMA 协方差被动缩书 | #138 持仓相关与 effective bets；无相关驱动的限制 | 部分 |
| 最大回撤与尾部 | R8 阶梯缩仓、exit overlay 6σ、probe 止损 | VaR/ES **拒算**、强平距离 | 部分 |
| regime 转换与结构断裂 | 无。regime 门判负 | 衰减规则是预登记的失效检测；无 CUSUM 一类检验 | 已有 |
| 逆境压力测试 | 无 | 崩盘窗口停在 k=0.60；成本与滑点网格；无 σ 翻倍、相关趋 1 情景 | 部分 |

行号：仓位那一行在 `beidou_live/guards.py:83-109`，阶梯在 `beidou_alpha/overlays/ladder.py:143-176`，
参与率在 `beidou_live/rebalancer.py:258-276`，相关在 `beidou_live/report_risk.py:1458-1523`。
「只报告」那一列 09-25 写明了理由：上线前没有预登记阈值，看了读数再定的阈值不算阈值。

### output_format

| 模板的节 | 北斗的载体 | 判定 |
| --- | --- | --- |
| Executive Summary | RESEARCH_LOG 各节的首段结论；深度分析的 Decision Memo | 已有 |
| Market Analysis | 无「当前 regime」读数 | 部分 |
| Strategy Details | 预登记九项、registry、信号 docstring | 已有 |
| Backtest Results | validate 报告 21 节，无图 | 已有 |
| Comparison & Ranking | 不排名是决策；并排一览没有产物 | 部分 |
| Final Recommendations | ADOPTIONS、probe 放行与 `accepted_despite` | 已有 |
| Deployment Considerations | RUNBOOK、MAINNET_READINESS、启动门两半 | 已有 |
| What Could Go Wrong | 预登记第九项「实盘失效方式」（#136）、reopen 条件、异常线 | 已有 |

## 这份模板新问出来的：十项

编号 N1–N10。价钱按仓库惯例写：代码量级、ledger 笔数、动不动构造、何时生效。另加一列 effort 分桶：
`report weekly` 把 `beidou_alpha/` 与 `tests/alpha/` 记作 alpha，`docs/RESEARCH_LOG` 与 `docs/analysis/`
记作 research，其余全是 infrastructure（`beidou_live/report_governance.py:73-84`）。09-27 那周的读数是
**14.1%**（alpha 0、research 301、infrastructure 1,827 行；`reports/weekly/2026-09-27.json`），目标 90%。
所以每一项 infrastructure 的行数都要说清买到什么。

| # | 缺口 | 现状与证据 | 做法 | 价钱 |
| --- | --- | --- | --- | --- |
| N1 | 尾部尺子换 k | 日报 G4 一节自 09-27 起 `n/a`；常量钉在 0.60（`report_risk.py:728-760`）；崩盘窗口未在 0.175 重放（`alpha_registry.yaml:227-229`） | 用 `scratchpad/g4_stress_windows_and_var_at_k060.py` 在现行构造（k 0.175、R8 档位 0.13125/0.0875）重跑；更新四个常量、`TAIL_VOL_TARGET` 与钉住它们的测试；registry 里 k=0.30 的窗口注释同批更新（10-13 文档裁定表 #13） | 代码 S；0 ledger（G4 先例不计）；不动构造；主 checkout 快进后下一个 :10 生效，不重启；infrastructure 约 15 行加脚本参数 |
| N2 | validate 证据文件加基准块 | 基准只用来切 regime（`research_validate_cmd.py:512-517`）；decompose 有基准块（`decompose.py:87-129`）；BTC 买入持有零命中 | 在同一组样本外窗口上印 pit 等权篮子的 Sharpe、MDD、收益，书与篮子的相关，另印 BTCUSDT 买入持有同窗口。只报不判 | 代码 S–M：算法放 `beidou_alpha/validation/`（alpha 约 40 行）、cli 接线约 10 行；0 ledger；不动构造，证据指针不变；现引报告要重出才有这一块 |
| N3 | validate 证据文件加按币集中度 | validate 无任何按币读数；`per_symbol_summary` 只在 backtest 打印（`backtest.py:213`；`research_backtest_cmd.py:165`） | 全样本与样本外净 P&L 的 top-1、top-3 份额；剔除任一币贡献后的样本外 Sharpe 最小值（贡献口径，不重跑书，权重不重归一，写明是近似）。只报不判 | 同 N2 的形状：alpha 约 40 行、cli 约 10 行；0 ledger。只读已计费格子的逐币贡献，不新增格子 |
| N4 | 邻格探针扰动被选的维度 | `numeric_keys` 取 `DEFAULT_GRIDS`（`research_validate_cmd.py:407-409`）；被选的 `crowding_window` 没被扰动 | 改取 `best_params` 的数值维度，列表型照旧跳过；加一条测试钉住「被选维度必在邻域里」 | 代码 S（cli 约 20 行加测试）；0 ledger，邻域本来不计费（`research_ledger_io.py:262-263`）；不动构造 |
| N5 | 研究报告补 CAGR 与 Calmar | 只有累计 `net_return`（`metrics.py:150-191`）；CAGR、Calmar 零命中；胜率是 bar 级 `hit_rate`（`:128-133`） | `full_sample` 与 `walk_forward` 各加 `cagr`、`calmar`；逐笔胜率与 profit factor 不做，目标权重架构下「一笔」没有定义 | 代码 S（alpha 约 20 行）；0 ledger；可与 N2、N3 合成一个 PR |
| N6 | 新闻与网络检索的决策记录 | 五类文件零命中，既无接入也无「不做」 | 在 `docs/ARCHITECTURE.md` 09-16 撤出三源那段旁写一段：不接入；理由是无 as-of 版本、不可核时点（与 #30 同一判据）、无读者（09-16 教训「先写读者再谈数据」）；重开条件是一条预登记点名列与带时点的来源 | docs S，约 15 行 |
| N7 | G12 低波横截面族 | 预登记草稿在 10-13 文档 §5；冻结 09-27 结束；`research power` N=4 时真 Sharpe 1.0 检出 50%；probe 名额 2 个、预算合计 1/3（`policy.py:203-204`），flow 已占 1/3（`alpha_registry.yaml:427-441`） | 按草稿走：阶段 0 零 ledger 的 `research correlate` 对 tsmom，相关 ≥ 0.5 即停；过了再问阶段 1 的 4 笔。size 族不做，草稿给了三条理由 | 代码 M（alpha：信号模块、注册、因果测试；alpha 余量 112 行，多半要抬顶写理由）；ledger 阶段 0 为 0、阶段 1 为 4；不动构造（新桶）；上线要 probe 位，得等 flow 退出或降到 1/6 并重出书级报告 |
| N8 | 日报加一行「当前 regime」 | 无此节（反向搜索见文末）；validate 有三分位切点 `vol_from/vol_to` | 印今天篮子 30 天实现波动落在现引证据的哪个三分位、书在那一档的样本外 Sharpe；另印受管 universe 里 168/336/720 动量为正的占比。只报不告警 | 代码 S（live 约 40–60 行加测试；live 余量 91）；0 ledger；快进后生效；**infrastructure**，买到的是读日报时的上下文，不改任何动作 |
| N9 | 候选登记表 | 排行榜零命中；晋级先来先到；现有的并排产物只有 mine 短名单、correlate 报告、09-18 分析的手写表 | 只读表：每个策略最新一份 validate 的日期、N、门、样本外、判定、预登记 commit、reopen 条目；按日期排，不按 Sharpe 排；门与 N 必印 | 代码 S–M（cli 约 80–120 行；cli 余量 80，要抬顶）；0 ledger；**infrastructure**；风险是被当排行榜读，仓库写明过这一点 |
| N10 | G11 净敞口上限 | 草稿在 10-13 文档 §4；k=0.175 下书全多头，上限等于降 k；草稿自己预期挂在阶段 1；#237 后 gross 按 USDT，草稿的单位一节部分过期 | 不做，直到书出现空头 bar。可观测的重开条件：日报「Market beta」的信号全多头 bar 占比低于 100% 连续 30 天 | 16 笔申报加 2 笔重出；构造变更；建议缓做 |

N2 与 N3 有一条共同的边界：它们只读已经计过费的格子，不新增评估，所以不构成「免费的读数交回候选行」。
它们改的是证据文件的形状；registry 比对的是参数与指纹，不比对报告字段，现有证据指针不受影响。

## Triage

按 deep-analysis 5.7 的表。「建议下一步」只用四个词。

| # | 需求（一句话复述） | S/M/L 与命中条件 | Early Kill | 最高风险预设 | 建议下一步 |
| --- | --- | --- | --- | --- | --- |
| N1 | 让日报的尾部尺子在现行 k 下重新出数 | S：单模块、只改常量与脚本参数 | 无 | G4 脚本在 k 0.175 与新档位下能原样跑通（脚本写在 0.60 时代，可能把 k 写死了） | 直接做 |
| N2 | 证据文件里印书对篮子的对照 | S：单点、只报不判 | 无 | 这一块会改变读者判断，而不是重复 decompose 的读数（decompose 不是 registry 引的证据） | 直接做 |
| N3 | 证据文件里印按币集中度 | S：同上 | 无 | 贡献口径的留一近似不会误导：它低估了重归一后的变化（写明即可） | 直接做 |
| N4 | 邻格探针扰动被选的维度 | S：单函数 | 无 | 被选维度可数值扰动（列表型仍要跳过） | 直接做 |
| N5 | 报告补 CAGR 与 Calmar | S | K6 边缘：两个数不进任何门 | 操作者读证据时会用到年化口径（RESEARCH_LOG 的 k 决策一直用 CAGR 中位） | 直接做，并入 N2 的 PR |
| N6 | 写下新闻与网络检索不接入的决定 | S：docs | 无 | 这个决定不会在 mainnet 准入前被推翻 | 直接做 |
| N7 | 低波横截面族按草稿预登记并跑阶段 0 | M：新信号、花 ledger、要 probe 位 | 无 | 逆波动率定价后它仍与 tsmom 相关 < 0.5（草稿自己预期这一步最可能挂） | 直接做阶段 0；阶段 1 花 ledger 前再问 |
| N8 | 日报印当前 regime | S | K6：infrastructure 行数换上下文，alpha 占比 14.1% | 操作者读日报时要这个上下文（E5，没问过） | 不做，除非操作者要 |
| N9 | 候选登记表 | S–M | K6：同上，且有被当排行榜读的风险 | 表会被按门读而不是按 Sharpe 读 | 不做，除非操作者要 |
| N10 | 净敞口上限 | M：构造变更、16 笔 | K6：全多头下等于降 k | 书会出现空头 bar | 不做，写重开条件 |

## 要操作者定的

| # | 事项 | 选项 | 价钱 | 建议 | 什么都不定时 |
| --- | --- | --- | --- | --- | --- |
| 1 | 批 A：N1、N2+N3+N5、N4、N6 | 做，或挑几项 | 四个 PR；0 ledger；不动构造；不重启；alpha 约 100 行、infrastructure 约 50 行、docs 约 15 行 | 全做。N1 是已发生的失效方式，其余三项把证据文件补到能回答模板的四问 | G4 继续 `n/a`；证据文件继续不比基准、不看集中度 |
| 2 | 批 B：N7 阶段 0 | 跑，或不跑 | 0 ledger 的 correlate；代码 M 在 alpha 桶 | 跑。它是本文唯一的 alpha 项，也是 F4 唯一有草稿的候选 | G12 继续停在草稿 |
| 3 | N7 阶段 1 | 随阶段 0 结果再问 | 4 笔 ledger；probe 位 | 阶段 0 过了再定 | — |
| 4 | 批 C：N8、N9 | 要，或不要 | 各 S–M，infrastructure 约 150 行 | 不做。alpha 占比 14.1% 时不加只读上下文 | 与现状相同 |
| 5 | N10 的重开条件 | 写进 reopen.yaml，或不写 | docs S | 写：全多头 bar 占比低于 100% 连续 30 天 | G11 草稿无人读 |
| 6 | 新读数合入前的对抗审查 | N2、N3、N8 合入前各交一个全新上下文子代理审 | 每项约半小时 | 做。09-25 四个新读数各有中级问题，事后审等于上线一轮错读数 | 同 09-25 |

批 A 与批 B 互不依赖，可以并行。批 A 四个 PR 都改 `beidou_cli/research_validate_cmd.py` 或 `beidou_alpha/validation/`，
建议合成两个：N1 一个，N2+N3+N4+N5 一个。

## 不做的，与理由

| 项 | 理由 |
| --- | --- |
| 按模板八步重排研究流程 | 09-25 同一结论：包本来就是这条流水线，重排不增能力 |
| 接入新闻、网络检索、期权、宏观 | 09-16 撤出三源的教训：先写读者再谈数据。任何新数据族要先有一条预登记点名列。近三次零 ledger 看新族（大户多空比、现货主动买卖、LS 叶）全是 NO-GO 或 REFUTED |
| 用「Sharpe > 1、回撤 < 30%」替换 D-020/D-028/D-018 | 更松。回撤那条会否掉操作者自己定的预算 |
| 情景压力（σ 翻倍、相关趋 1） | vol target 书里相关趋 1 接近基线（17 个持仓约 2 个 bet）；崩盘窗口是它的经验版，N1 把它换到现行 k |
| 结构断裂检验（CUSUM 一类） | 衰减规则是预登记过的失效检测，再加一个未预登记的读数只会多一处「看了再定」 |
| 逐笔胜率与 profit factor | 目标权重架构下「一笔」没有定义；bar 级 `hit_rate` 已有 |
| 排名表 | 决策已写明；N9 只在操作者要时做，且不按 Sharpe 排 |

## 约束与状态

- **构造冻结已于 09-27 结束**（`docs/RESEARCH_LOG.md:17392`）。本文的项目里没有一项动构造，N7 是新桶，N10 不做。
- **日期开关**（`beidou governance calendar`，09-29 读数）：10-03 批次窗口翻页、tsmom 与 flow 的 probe 复审到期、
  probe-stop 口径 DUE；10-13 bridge 变 inert、两条 reopen 变 MET。裁定仍要人来做。
- **alpha 投入**：09-27 周报 14.1%。批 A 的 alpha 行数约 100，infrastructure 约 50；批 C 全是 infrastructure。
- **source budget 余量**（09-29 实测）：alpha 112 / live 91 / cli 80 / data 40 / exchange 40 / shared 40 / governance 47 行。
  N7 与 N9 大概率抬顶，理由写进 `docs/SOURCE_BUDGET_LOG.md`。
- **不花 ledger**：本文没跑任何计费命令。`reports/research/trials.jsonl` 未动。
- **09-28 六条裁定**里与本文相关的：O-2、O-3 不建告警，所以 N8 即使做也只印不响；「alpha 先剖析」已跑，
  计算不是研究吞吐的瓶颈，本文没有性能项。
- **在途**：`ps` 显示 armed 进程于 09-28T16:29:33Z 启动，晚于 #237 合入（16:28:31Z）；RESEARCH_LOG 里最后一节
  重启是 #61。对应的重启记录在分支 `docs/restart-62-usdt-gross-cap` 上，尚未合入。本文不替它写。

## 顺带发现，未修

| 发现 | 出处 |
| --- | --- |
| `beidou_live/reports.py:513-515` 与 `report_decay.py:360-363` 仍写「no job runs this report」，而 `com.beidou.weekly` 09-28 起已加载（`launchctl list`，退出码 0） | 子代理 D |
| `beidou_cli/research_cmd.py:1` 写「九个子命令」，现有 11 个（含 `forward` 一组）；`docs/RUNBOOK.md:32` 写「17 个子命令」，现有 18 个（`calendar` 09-28 加），表格只列 15 个 | 子代理 B、D |
| `beidou_cli/__init__.py:31-33` report 命令组的说明仍写「Daily attribution reports.」 | 子代理 D |
| `config/live.demo.yaml:522` 注释「for the rest of the UTC day」；代码每周期重判（`guards.py:86-88`），权益回到 −5% 以上就恢复加仓；回测 `backtest.py:352` 同一语义。是注释不准，不是行为分歧 | 子代理 E，本文核过 |
| `beidou_alpha/backtest.py:289` 注释仍写冲击「under 0.01 bps」，`config/costs.yaml:78-86` 09-09 已撤回为 0.48 bps | 子代理 C，本文核过 |
| `governance/governance_state.json` 停在 09-09，tsmom 仍记 main；`advance --commit` 不在任何定时任务里（`deploy/run_governance_gate.sh:27-28`）。10-13 文档裁定表 #8 仍悬着 | 子代理 B、D |
| 09-23 清点里两句已过时：「研究侧对照取全体 `panel.symbols`」（#113 已改按成员）、「`regime_split_sharpes` 零调用者」（#104 已接进 validate）。历史文档不改 | 子代理 C |

## 局限

- 五个子代理各读一次代码，我只抽核了本文引用的行号与数字，没有重跑任何命令。
- 「67 条」是我对模板的切分，别人切会得到别的数。四档的判定有主观成分，判「部分」的 21 条每条都写了缺哪一块。
- N1 的价钱假定 G4 脚本改参数就能跑；它写在 k=0.60 时代，可能把 k 或档位写死了，做的时候先读脚本。
- N2、N3 的行数是估计。合入前要在同一份快照上出报告比对，只多自己那一块，其余逐字节相同（09-25 的验收办法）。
- 模板是通用市场的写法。它的板块、财报、期权 flow 在加密永续上都只有对应物，判「已有」的依据是对应物。

## 反向搜索记录

每处「缺」都搜过，零命中或只命中反面声明。范围是 `beidou_*`、`config/`、`deploy/`、`docs/`、`governance/`，
排除 `.claude/worktrees` 与 `.venv`。

| 缺口 | 搜索词 |
| --- | --- |
| 新闻、网络检索 | news、新闻、websearch、web search、网络检索、cryptopanic、newsapi |
| 期权 | deribit、option、options、期权、implied vol、IV |
| CAGR、Calmar、profit factor | cagr、annualised_return、annualized_return、annual_return、calmar、profit factor、profit_factor |
| 结构断裂 | cusum、chow、changepoint、change-point、structural break、bai-perron、zivot、sup-wald、结构断裂、结构突变、断点检验 |
| 情景压力 | stress、scenario、shock、情景、相关趋、vol doubl、σ翻倍、波动翻倍 |
| 排名表 | leaderboard、tournament、排行榜、排名表、排行、ranking |
| 当前 regime 读数 | regime、trend_state、market_state、vol_regime、sideways、bull、bear、市场状态、行情状态、牛市、熊市、震荡 |
| 按币集中度 | leave_one、leave-one、jackknife、per_symbol、concentration、top_symbol、symbol_share |
| 基准 | buy_and_hold、buy-and-hold、buy and hold、equal_weight、benchmark |
| 敏感性曲面 | sensitivity、neighbour、neighbor、plateau、surface、perturb |
| 对抗审查规则原文 | 对抗审查、对抗式审查、对抗性审查、adversarial review、全新上下文、互不通气（只在项目 memory 里，仓库无规则原文） |
