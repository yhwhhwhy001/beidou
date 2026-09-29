# 「AI 交易研究分析师」prompt 模板对照北斗：清点与优化方案

2026-09-29。一次清点加一份带价方案，不是裁定，也不是预登记。行号以 `99551815`（#241 合入）为准；
source budget 余量另按 origin/main `c88ee6fd` 重量，见「约束与状态」。本文不改代码、不花 ledger、不碰实盘。
Phase 7 对抗审查由 Opus 5.5 子代理执行，审查稿 `docs/analysis/2026-09-29-research-analyst-prompt-vs-beidou-review.md`，
八条 Kill 的处置在文末「对抗审查与处置」。

```text
Reading Check：本批 10 条需求（N1–N10）的共同背景为「操作者第三次拿一份外部量化 prompt 对照北斗，问还有什么可以补」；
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
  （`docs/analysis/2026-09-25-external-checklist-round-two.md`）。8.10（第一期）、9.4、9.6 三项同日也做了。

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
| 模板 80 条 | 55 | 25 | 0 | 0 | 80 |

80 条由下文七张表数出。**没有空白。** 冻结稿把「网络检索」判成空白，审查（K-05）指出仓库的分析一直在用它：
09-26 的分析按 URL 引币安官方 FAQ，RUNBOOK 处理断档写着「看币安公告」。作为分析工具它是已有；
作为数据源不接入，那是 N6 要写下的决定。「新闻与事件」判部分：事件有对应物（#157），新闻流没有决策记录。
没有一条判「不适用」：模板的板块、财报、期权 flow 在加密永续上都找得到对应物或写明的决定。

25 条「部分」分三类。6 条是前两轮写明的取舍：微观结构不读盘口（5.7）、催化剂第二期只有草稿（#157）、
回测的冲击模型默认关与参与率只计量（KILL-A）、可实施性等真钱前的校准（准入设计 §6）、平仓流动性只报告（#141）、
持仓相关只报告（#138）。3 条是审查后按判据从严改判的，不构成新缺口：thesis 与 edge（在跑的 flow 没写对手方）、
「我漏了什么」（对抗审查是惯例，规则原文不在仓库）、regime 转换（与相关性那行同一标准）。
其余 16 条是这份模板新问出来的，去重后是 9 个缺口（下文 N1–N9），集中在四处；另加前两轮留下、
等冻结结束的 G11（N10）。

### 一、尾部尺子自 09-27 起是瞎的

日报「Tail beside the sigma ruler (G4)」从 09-27 切到 k=0.175 起印 `status n/a`
（`reports/daily/2026-09-28.md:447-452`）。VaR / ES 常量钉在 k=0.60
（`beidou_live/report_risk.py:728-734`），k 不等就拒算（`:758-760`）。五段崩盘窗口也没在 k=0.175 上重放
（`config/alpha_registry.yaml:226-229`，09-27 已把 k=0.30 那段标成历史，并写明「0.175 上没有重放」）。
09-25 的 G11 草稿把这件事写成了实盘失效方式第 3 种「尺子没跟着换」
（`docs/analysis/2026-09-25-october-13-readiness.md`§4 第 9 项）。它发生了，只是发生在换 k 而不是加上限的那次。

而且是第二次。09-23 清点 §三记过「尾部读数停在 k=0.30」，那次 G4 在 0.60 上重量了一遍。#163 换 k 时
`vol_band` 跟着动了，因为它有一条耦合测试（`tests/live/test_the_risk_budget_default_does_not_lag_the_profile.py:49`）；
G4 的常量只有运行期拒算的测试，没有「k 变了必须重量」的耦合，所以没跟着动。只重量一次，下一次改 k 还会变成 `n/a`。

### 二、证据文件不比基准，也不看按币集中度

`research validate` 的报告最多 21 节（现引报告 20 节），没有一节把书和它自己的基准放在一起。
`benchmark_returns` 在 validate 里只用来切 regime 标签（`beidou_cli/research_validate_cmd.py:512-517`）。
基准对照在 `research decompose` 有（`beidou_alpha/validation/decompose.py:87-129`，E-058 就是它量的），
在 `research backtest` 有篮子的收益与 Sharpe（`beidou_cli/research_backtest_cmd.py:157-164`），在实盘有 D-045。
所以缺的不是能力，是位置：registry 引用的证据文件本身不回答「书比等权篮子好多少」。BTC 买入持有全仓零命中。

按币集中度同样：`per_symbol_summary` 在回测结果里（`beidou_alpha/backtest.py:213`），只有 `research backtest`
打印它（`research_backtest_cmd.py:165`）。validate 没有任何「不依赖单一资产」的读数。仓库自己记过这条
失效方式：flow 的 edge 来自后来离池的名字（`config/alpha_registry.yaml:479-481`）。

### 三、邻格探针扰动的不是被选的那个维度

`parameter_neighborhood` 的数值维度取自 `DEFAULT_GRIDS`（`beidou_cli/research_validate_cmd.py:407-409`），
不是本次 `--grid`。现引报告只跑了 `crowding_window` 两格，恰恰是这一维没被扰动；`vol_window` ±10% 的
Sharpe 与 base 完全相同。读数只报不判、不计 ledger（`beidou_cli/research_ledger_io.py:262-263`），
所以修它不动门、不动账。

### 四、策略数量是门的产物，不是目标

模板要 3–10 个根本不同的策略。仓库有 11 个手写信号（`beidou_alpha/signals/__init__.py:23-140`），
约 6 个机制族；9 个建成并判负，各带 reopen 条件；在跑 2 本。前向板 2 个在板（`reports/research/forward_board.jsonl`
第 3、4 行：tsmom 与 residual，09-27 读数都是观察中）。F4「缺一本低相关的书」
仍是 UNKNOWN（`docs/RESEARCH_LOG.md:13744-13746`）。唯一写好预登记草稿、等冻结结束的新族是 G12 低波
（10-13 文档 §5）。冻结 09-27 已结束（`docs/RESEARCH_LOG.md:17392`），它可以批了。

## 逐项对照

### role 与 objective

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 量化分析与严格回测 | walk-forward、CPCV、D-028 去偏门、预登记 | 已有 | `beidou_alpha/validation/` |
| 市场微观结构 | 逐单 TCA、参与率截单、取整；盘口不读，选池不做深度打分是写明的决定 | 部分 | 09-23 清点 5.7；#139 |
| 宏观理解 | 决策：无读者则撤出。宏观数据 09-10 建、09-16 撤，拉回要先有读者（`git show 71863e9e`） | 已有 | `docs/ARCHITECTURE.md:77` |
| 怀疑、数据驱动、可部署 | 预登记九项、对抗审查惯例、分析校准表、启动门 | 已有 | `docs/PREREGISTRATION.md`；`docs/analysis/analysis-calibration.md` |
| 3–10 个高质量策略 | 11 个信号族、在跑 2 本、新存活 0，见结论四 | 部分 | `beidou_alpha/signals/__init__.py:23-140` |
| edge 有证据、非曲线拟合 | D-020 判定（PBO、CPCV、成本 ×2 是硬门，DSR 只报告）、D-028 门、随机信号阴性对照 | 已有 | `beidou_alpha/validation/verdict.py:106-168`；`tests/alpha/test_causality.py:89-102` |
| 带排名的最终清单 | 决策：不排名。晋级先来先到，前向板写明「不要当排行榜读」。候选并排一览没有产物 | 部分 | `beidou_governance/policy.py:208-215`；`beidou_alpha/validation/forward_board.py:452-455` |

### context 与 tools_and_data

| 条目 | 北斗的对应物 | 判定 | 出处 |
| --- | --- | --- | --- |
| 实时与历史价格 | 永续 1h/1d、现货 1h、资金费、6 列 metrics；实盘每根 bar 走 REST | 已有 | `beidou_data/sync.py:47`；`beidou_data/live_feed.py:67-93` |
| 成交量 | K 线带 quote_volume 与 taker 两列 | 已有 | `beidou_data/binance_public.py:19-31` |
| 期权数据与 flow | 决策：块 0 判 Out，另立项目 | 已有 | `docs/analysis/2026-09-08-autonomous-governance-51-strategies-plan.md:30,144` |
| 基本面 | 加密对应物是链上：09-16 因无读者撤出，拉回要先有读者 | 已有 | `docs/ARCHITECTURE.md:77` |
| 宏观（利率、通胀） | 同上，撤出 | 已有 | 同上 |
| 新闻与事件 | 事件：#157 第一期，读自有归档，只报告。新闻流：无接入，无决策 | 部分 | `beidou_live/report_events.py:1-22` |
| 网络检索 | 分析时在用：09-26 分析按 URL 引币安官方 FAQ；RUNBOOK 处理断档「看币安公告」。作为数据源不接入，见 N6 | 已有 | `docs/analysis/2026-09-26-per-symbol-leverage-first-principles.md:128-134`；`docs/RUNBOOK.md:207` |
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
| 策略设计：thesis 与 edge | 预登记第 1 项要求写「为什么」；代码里只有 tsmom 写了谁付钱，在跑的 flow 只写了机制与空头门的理由，没写对手方（09-23 清点 1.1 同一判断） | 部分 | `docs/PREREGISTRATION.md:13-15`；`beidou_alpha/signals/tsmom.py:3-38`；`flow.py:1-17` |
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
| 可实施 | 成本 ×2 ≥ 0 是硬门（`verdict.py:158-159`）；参与率只在实盘（`config/live.demo.yaml:48`）；冲击待校准 | 部分 | `docs/MAINNET_READINESS.md`§6 |

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
| 我漏了什么 | 对抗审查由全新上下文子代理做，是惯例；规则原文只在项目 memory 里，仓库没有 | 部分 | 09-23 清点 D.2；反向搜索见文末 |
| 假设弱在哪 | Assumption Register、E 级 | 已有 | — |
| edge 真的还是过拟合 | D-028 门、PBO、随机信号阴性对照；DSR 只报告 | 已有 | `multiple_testing.py:370-448`；`tests/alpha/test_causality.py:89-102` |
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
| regime 转换与结构断裂 | 无。regime 门判负 | 衰减规则是预登记的失效检测；无 regime 转换的读数，无 CUSUM 一类检验 | 部分 |
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

编号 N1–N10。价钱按仓库惯例写：代码量级、ledger 笔数、动不动构造、何时生效。

十项里除 N7 都是只报不判的读数或文字。区分它们的标准只有一条：**有没有具名的读者，读到什么会改哪个决定。**
N1 的读者是每小时巡检与日报的尾部一节（读到越线天数）；N2、N3、N5 的读者是 registry 引用的证据文件本身，
D-045 在实盘问的「是不是 beta」、E-058 在研究侧答错过的那一问，证据文件今天答不上；N4 的读者是同一份报告的
邻格一节。N8、N9 没有具名读者，只有「人读日报时的上下文」。批 A 与批 C 的取舍按这一条，不按代码放在哪个目录。

effort 分桶只作信息，不作判据：`report weekly` 把 `beidou_alpha/` 与 `tests/alpha/` 记作 alpha，
`docs/RESEARCH_LOG` 与 `docs/analysis/` 记作 research，其余全是 infrastructure，分子是 alpha 加 research
（`beidou_live/report_governance.py:73-84`）。所以本文这三百多行自己就记在分子里。09-27 周报的读数是
**14.1%**（alpha 0、research 301、infrastructure 1,827 行；`reports/weekly/2026-09-27.json`，gitignore，09-29 读取），
目标 90%。那份还是「最近 40 个提交」的口径；#242（09-29 合入）起改按这一周合入 main 的改动算，下一个周报才有新口径的数。

| # | 缺口 | 现状与证据 | 做法 | 价钱 |
| --- | --- | --- | --- | --- |
| N1 | 尾部尺子换 k，并防复发 | 日报 G4 一节自 09-27 起 `n/a`；常量钉在 0.60（`report_risk.py:728-760`）；崩盘窗口未在 0.175 重放（`alpha_registry.yaml:226-229`）；脚本把 k 写死在 `K_NOW, K_CONTROL = 0.60, 0.30`（`scratchpad/g4_stress_windows_and_var_at_k060.py:84`，打印在 `:281-284`） | 脚本的 k 与档位改成参数，在现行构造（k 0.175、R8 档位 0.13125/0.0875）重跑；更新四个常量、`TAIL_VOL_TARGET` 与钉住它们的测试；registry `:226-229` 那段补上 0.175 的数。**再加一条耦合测试**：profile 的 `vol_target` 必须等于 `TAIL_VOL_TARGET`，照 `vol_band` 的先例（`tests/live/test_the_risk_budget_default_does_not_lag_the_profile.py:49`），下一次改 k 时 CI 红在这里，而不是日报静默 `n/a` | 代码 S；0 ledger（G4 先例不计）；不动构造；主 checkout 快进后下一个 :10 生效，不重启；infrastructure 约 25 行加脚本参数 |
| N2 | validate 证据文件加基准块 | 基准只用来切 regime（`research_validate_cmd.py:512-517`）；decompose 有基准块（`decompose.py:87-129`）；BTC 买入持有零命中 | 在同一组样本外窗口上印 pit 等权篮子的 Sharpe、MDD、收益，以及书对篮子的**条件 beta**（敞口 × 市场）与 NW t；另印全多头 bar 占比与 BTCUSDT 买入持有同窗口。只报不判。**口径照 D-045 的四条规矩**（`beidou_live/benchmark.py:1-33`：时点篮子、NW t、条件 beta、signal state）：无条件的全期相关会重演 E-058——回测量到 −0.18，实盘量到 +0.86，差在空头腿（`docs/RESEARCH_LOG.md:379,13640`） | 代码 M：算法放 `beidou_alpha/validation/`（alpha 约 60 行）、cli 接线约 10 行；0 ledger；不动构造，证据指针不变。**收益要等一次重出**：新块要逐 bar 净收益，证据 JSON 里没有，见表后 |
| N3 | validate 证据文件加按币集中度 | validate 无任何按币读数；`per_symbol_summary` 只在 backtest 打印（`backtest.py:213`；`research_backtest_cmd.py:165`） | 全样本与样本外净 P&L 的 top-1、top-3 份额；剔除任一币贡献后的样本外 Sharpe 最小值（贡献口径，不重跑书，权重不重归一，写明是近似）。只报不判 | 同 N2 的形状：alpha 约 40 行、cli 约 10 行；0 ledger。只读已计费格子的逐币贡献，不新增格子。同样要等重出 |
| N4 | 邻格探针扰动被选的维度 | `numeric_keys` 取 `DEFAULT_GRIDS`（`research_validate_cmd.py:407-409`）；被选的 `crowding_window` 没被扰动 | 改取 `best_params` 的数值维度，列表型照旧跳过；加一条测试钉住「被选维度必在邻域里」 | 代码 S（cli 约 20 行加测试）；0 ledger，邻域本来不计费（`research_ledger_io.py:262-263`）；不动构造 |
| N5 | 研究报告补 CAGR 与 Calmar | validate 报告字段里只有累计 `net_return`（`metrics.py:150-191`）；CAGR 在 RESEARCH_LOG 与 k 决策的 scratchpad 脚本里常用，报告字段没有；Calmar 全仓零命中；胜率是 bar 级 `hit_rate`（`:128-133`） | `full_sample` 与 `walk_forward` 各加 `cagr_full_sample`、`calmar_full_sample`，名字带口径：它们是全样本漂移的数，k 决策读的是诚实漂移下的 CAGR 中位（k=0.175 时 22.5% / 24.3%，原漂移约 40%，`config/live.demo.yaml:242-246`），两者同名不同数，报告正文要指过去。逐笔胜率与 profit factor 不做，目标权重架构下「一笔」没有定义；bar 级 payoff ratio 09-08 用过一次（`alpha_registry.yaml:224-225`），要的话随本项印，约 5 行 | 代码 S（alpha 约 25 行）；0 ledger；与 N2、N3 合成一个 PR |
| N6 | 新闻流不接入的决策记录 | 五类文件零命中：既无接入，也无「不做」 | 在 `docs/ARCHITECTURE.md` 09-16 撤出三源那段旁写一段，**只限信号与数据源**：不接入新闻流；理由是无 as-of 版本、不可核时点（与 #30 同一判据）、无读者（09-16 教训「先写读者再谈数据」）；重开条件是一条预登记点名列与带时点的来源。分析时查官方公告与文档照旧，那不是数据源 | docs S，约 15 行 |
| N7 | G12 低波横截面族 | 预登记草稿在 10-13 文档 §5；冻结 09-27 结束；`research power` N=4 时真 Sharpe 1.0 检出 50%；probe 名额 2 个、预算合计 1/3（`policy.py:203-204`），flow 已占 1/3（`alpha_registry.yaml:427-441`），G2 一旦执行两个位子都满（10-13 文档 `:635`、裁定表 #8）。草稿写成之后新落地的读数：#140 量到书对「低波（低减高）」的载荷 0.14（t 5.93），但与 size 相关 0.80、VIF ≥ 5，载荷不稳（`reports/daily/2026-09-28.md:290,297`，gitignore，09-29 读取） | 先把草稿更新成正式预登记：预期按 #140 的载荷改写（书已带低波倾斜，阶段 0 更可能挂）；写明 probe 位的前置；**预登记先入库，再跑阶段 0**——`research correlate` 会印候选自己的 Sharpe（`research_correlate_cmd.py:111,120`），看过之后再决定付不付钱，就不是预登记。阶段 0 零 ledger，相关 ≥ 0.5 即停；过了再问阶段 1 的 4 笔。size 族不做，草稿给了三条理由 | 代码 M（alpha：信号模块、注册、因果测试；alpha 余量 112 行，多半要抬顶写理由）；ledger 阶段 0 为 0、阶段 1 为 4；不动构造（新桶）；上线要 probe 位 |
| N8 | 日报加一行「当前 regime」 | 无此节（反向搜索见文末）；validate 有三分位切点 `vol_from/vol_to` | 印今天篮子 30 天实现波动落在现引证据的哪个三分位、书在那一档的样本外 Sharpe；另印受管 universe 里 168/336/720 动量为正的占比。只报不告警 | 代码 S（live 约 40–60 行加测试；live 余量 65）；0 ledger；快进后生效；没有具名读者，买到的是读日报时的上下文 |
| N9 | 候选登记表 | 排行榜零命中；晋级先来先到；现有的并排产物只有 mine 短名单、correlate 报告、09-18 分析的手写表 | 只读表：每个策略最新一份 validate 的日期、N、门、样本外、判定、预登记 commit、reopen 条目；按日期排，不按 Sharpe 排；门与 N 必印 | 代码 M（cli 约 80–120 行；cli 余量 35，要抬顶）；0 ledger；没有具名读者；风险是被当排行榜读，仓库写明过这一点 |
| N10 | G11 净敞口上限 | 草稿在 10-13 文档 §4；k=0.175 下书全多头，上限等于降 k；草稿自己预期挂在阶段 1；#237 后 gross 按 USDT，草稿的单位一节部分过期 | 不做，直到书出现空头 bar。重开条件**不能读日报「Market beta」的全多头占比**：那个窗口从 09-07T14:00Z 起累计（`beidou_live/report_beta.py:86-90`），09-28 读 279/496 = 56.25%，09-07 到 09-16 的空头 bar 永远在里面，按字面今天就满足。要读的是现行构造窗口内的口径（10-13 文档附录 A6「权重全 ≥ 0」，09-17 起 169 根 100%）：**自最近一次构造变更起，`cycles.jsonl` 目标权重含空头的 bar 占比 ≥ 20%，且窗口 ≥ 720 根** | 16 笔申报记进 tsmom 的桶（门约上移 0.006，今天余量 0.26，不翻判）加 2 笔重出；构造变更；建议缓做 |

N2 与 N3 有一条共同的边界：它们只读已经计过费的格子，不新增评估，所以不构成「免费的读数交回候选行」。
它们改的是证据文件的形状；registry 比对的是参数与指纹，不比对报告字段，现有证据指针不受影响。

但它们的收益要等一次重出才兑现：新块要逐 bar 的净收益序列，现引的证据 JSON 里没有。那次重出的价钱：
tsmom 现网格 2 格计 2 笔，N 341 → 343，门上移不到 0.001；`evidence` 指针与 sha256 换新；启动门复核；
registry 改动按 RUNBOOK「改了 registry / profile 之后」走。没有排期之前，批 A 合入了，registry 引的仍是 09-25 那份。

## Triage

按 deep-analysis 5.7 的表。「建议下一步」只用四个词；限定语在下一节「要操作者定的」。

| # | 需求（一句话复述） | S/M/L 与命中条件 | Early Kill | 最高风险预设 | 建议下一步 |
| --- | --- | --- | --- | --- | --- |
| N1 | 让日报的尾部尺子在现行 k 下重新出数，并让下一次改 k 时 CI 拦住 | S：单模块、常量、脚本参数、一条测试 | 无 | 脚本参数化之后在 k 0.175 与新档位下跑通（k 写死在 `:84`，已核） | 直接做 |
| N2 | 证据文件里印书对篮子的条件 beta | S：单点、只报不判 | 无 | 研究侧套上 D-045 四条规矩后，读数不再重演 E-058（回测相关 −0.18 对实盘 +0.86） | 直接做 |
| N3 | 证据文件里印按币集中度 | S：同上 | 无 | 贡献口径的留一近似不会误导：它低估了重归一后的变化（写明即可） | 直接做 |
| N4 | 邻格探针扰动被选的维度 | S：单函数 | 无 | 被选维度可数值扰动（列表型仍要跳过） | 直接做 |
| N5 | 报告补带口径名的 CAGR 与 Calmar | S | 无：读者是读证据文件的人，k 决策一直用 CAGR 中位，同名的数要能对得上口径 | 字段名带 `full_sample` 之后不会与诚实漂移的 CAGR 混读 | 直接做 |
| N6 | 写下新闻流不接入的决定 | S：docs | 无 | 这个决定不会在 mainnet 准入前被推翻 | 直接做 |
| N7 | 低波横截面族：更新草稿为预登记，入库后跑阶段 0 | M：新信号、花 ledger、要 probe 位 | 无 | 逆波动率定价后它仍与 tsmom 相关 < 0.5；#140 的低波载荷 0.14 说书已带这个倾斜，阶段 0 更可能挂 | 进入分析 |
| N8 | 日报印当前 regime | S | K6：没有具名读者，价值小于长期复杂度 | 操作者读日报时要这个上下文（E5，没问过） | 不做 |
| N9 | 候选登记表 | M：新命令、抬顶 | K6：同上，且有被当排行榜读的风险 | 表会被按门读而不是按 Sharpe 读 | 不做 |
| N10 | 净敞口上限 | M：构造变更、16 笔 | K6：全多头下等于降 k | 现行构造下书会出现空头 bar（09-17 起 100% 全多头） | 不做 |

排序用附录 B.8 的 ICE，Confidence 标支撑证据的等级：

| 需求 | Impact | Confidence | Effort | 批 |
| --- | --- | --- | --- | --- |
| N1 | 高：一个失效方式已发生两次，读数进每小时巡检 | E1（日报 `n/a`，代码常量） | S | A |
| N2 | 中：证据文件答上「是不是 beta」 | E1（代码；E-058 与 D-045 是仓库自己的记录） | M | A |
| N3 | 中：证据文件答上「靠不靠一两个币」 | E1（代码；flow 的先例） | S | A |
| N4 | 低：修一处口径错位 | E1（代码） | S | A |
| N5 | 低：两个带口径名的数 | E1（代码） | S | A |
| N6 | 低：关一处没有决定的项 | E2（09-16 撤出的记录） | S | A |
| N7 | 高：F4 唯一有草稿的候选 | E5（预期；功效表是上界） | M | B |
| N8 | 低：上下文 | E5（没问过操作者） | S | C |
| N9 | 低：上下文，且有副作用 | E5 | M | C |
| N10 | 中：只在书持空头时成立 | E5（今天不成立） | M | 不做 |

## 要操作者定的

| # | 事项 | 选项 | 价钱 | 建议 | 什么都不定时 |
| --- | --- | --- | --- | --- | --- |
| 1 | 批 A：N1、N2+N3+N4+N5、N6 | 做，或挑几项 | 三个 PR：N1 改 `beidou_live/report_risk.py` 与脚本；N2–N5 改 `beidou_alpha/validation/` 与 `research_validate_cmd.py`；N6 只改 docs。0 ledger；不动构造；不重启；alpha 约 125 行、infrastructure 约 55 行、docs 约 15 行。cli 余量 35，N2–N5 约 40 行 cli 要抬顶写理由。**N2、N3、N5 的收益要等一次 tsmom 重出**：2 笔，换证据指针，见上节表后 | 全做。N1 是发生过两次的失效方式；N2–N5 把证据文件补到能回答模板的四问，读者是证据文件本身 | G4 继续 `n/a`，下一次改 k 再瞎一次；证据文件继续不比基准、不看集中度 |
| 2 | 批 B：N7 先更新预登记并入库 | 批，或不批 | docs：把 10-13 文档 §5 的草稿按 #140 载荷与 probe 位更新成正式预登记，入 RESEARCH_LOG。这一步不跑任何东西 | 批。它是本文唯一的 alpha 项，也是 F4 唯一有草稿的候选 | G12 继续停在草稿 |
| 3 | N7 阶段 0 与阶段 1 | 预登记入库后跑阶段 0；阶段 1 随结果再问 | 阶段 0：代码 M 在 alpha 桶，0 ledger 的 correlate，会印候选 Sharpe，所以必须在预登记之后。阶段 1：4 笔 ledger；probe 位 | 阶段 0 过了再定阶段 1 | — |
| 4 | 批 C：N8、N9 | 要，或不要 | 各 S 与 M，infrastructure 约 150 行 | 不做：没有具名读者。要的话 N8 只印不响（O-2、O-3） | 与现状相同 |
| 5 | N10 的重开条件 | 写进 reopen.yaml，或不写 | docs S | 写，按上节 N10 那格的口径：现行构造窗口内含空头的 bar 占比 ≥ 20% 且窗口 ≥ 720 根。不写日报「Market beta」的累计占比，那条按字面今天就满足 | G11 草稿无人读 |
| 6 | 新读数合入前的对抗审查 | N2、N3、N8 合入前各交一个全新上下文子代理审 | 每项约半小时 | 做。09-25 四个新读数各有中级问题，事后审等于上线一轮错读数 | 同 09-25 |
| 7 | 一次 tsmom 重出的排期 | 批 A 合入后排，或等下一次构造变更一起 | 2 笔；换 `evidence` 指针与 sha256；启动门复核；按 RUNBOOK 走 registry 改动 | 等下一次构造变更一起，不单独为它付 | 证据文件里没有新块 |

批 A 与批 B 互不依赖，可以并行。

## 不做的，与理由

| 项 | 理由 |
| --- | --- |
| 按模板八步重排研究流程 | 09-25 同一结论：包本来就是这条流水线，重排不增能力 |
| 接入新闻流、期权、宏观数据 | 09-16 撤出三源的教训：先写读者再谈数据。任何新数据族要先有一条预登记点名列 |
| 用「Sharpe > 1、回撤 < 30%」替换 D-020/D-028/D-018 | 更松。回撤那条会否掉操作者自己定的预算 |
| 情景压力（σ 翻倍、相关趋 1） | vol target 书里相关趋 1 接近基线：09-28 日报 12 个持仓，effective bets 7 天 1.98、30 天 1.55；崩盘窗口是它的经验版，N1 把它换到现行 k |
| 结构断裂检验（CUSUM 一类） | 衰减规则是预登记过的失效检测，再加一个未预登记的读数只会多一处「看了再定」 |
| 逐笔胜率与 profit factor | 目标权重架构下「一笔」没有定义；bar 级 `hit_rate` 已有，bar 级 payoff ratio 要的话随 N5 印 |
| 排名表 | 决策已写明；N9 只在操作者要时做，且不按 Sharpe 排 |

## 约束与状态

- **构造冻结已于 09-27 结束**（`docs/RESEARCH_LOG.md:17392`）。本文的项目里没有一项动构造，N7 是新桶，N10 不做。
- **日期开关**（`beidou governance calendar`，主 checkout `c88ee6fd` 上 09-29 读数）：10-03 批次窗口翻页、
  tsmom 与 flow 的 probe 复审到期、probe-stop 口径 DUE；10-13 bridge 变 inert，startup gate 今天干净。
  RISK-G11 与回撤预算分母那两条 reopen 已由 #237 结成 resolved，不再在日历上。裁定仍要人来做。
- **alpha 投入**：09-27 周报 14.1%。批 A 的 alpha 行数约 125，infrastructure 约 55；批 C 全是 infrastructure。
- **source budget 余量**（origin/main `c88ee6fd`，09-29 用 `test_source_budget._lines` 重量；这个数每合一个 PR 就变）：
  alpha 112 / live 65 / cli 35 / data 39 / exchange 40 / shared 40 / governance 47 行。冻结稿写的 91 / 80 是在
  `77aaf2f0` 上量的，#241、#242 之后已经不对。N2–N5、N7、N9 都要抬顶，理由写进 `docs/SOURCE_BUDGET_LOG.md`。
- **不花 ledger**：本文没跑任何计费命令。`reports/research/trials.jsonl` 未动。
- **09-28 六条裁定**里与本文相关的：O-2、O-3 不建告警，所以 N8 即使做也只印不响；「alpha 先剖析」已跑，
  计算不是研究吞吐的瓶颈，本文没有性能项。
- **在途**：`ps` 显示 armed 进程于 09-28T16:29:33Z 启动，晚于 #237 合入（16:28:31Z）；RESEARCH_LOG 里最后一节
  重启是 #61。写这一段时它的记录有两份在途，#243 与 #244，两个会话各写了一份；两份都在 09-28 17:13Z 与 17:29Z 合入，
  RESEARCH_LOG 现在是两节（`:19692` 重启 #62，`:19722` 补记）。本文不替它写。

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

## 对抗审查与处置

审查稿：`docs/analysis/2026-09-29-research-analyst-prompt-vs-beidou-review.md`（Opus 5.5 子代理，冻结稿 `4475bdd1`）。
结论 PIVOT，范围限四处；没有 P0；G6 PARTIAL。审查者的最强反方：「本文把『证据文件里没有』当成『仓库里没有』，
又按代码路径把只报不判的读数记成 alpha」。前半句成立，后半句成立一半。八条 Kill 的处置：

| Kill | 级 | 审查说了什么 | 处置 |
| --- | --- | --- | --- |
| K-01 | P1 | N10 的重开条件按字面已满足：日报「Market beta」的全多头占比从 09-07 累计，09-28 是 56.25% | 改。条件改读现行构造窗口内含空头的 bar 占比，写明窗口起点、口径、桶与笔数（N10 那格与操作者表 #5） |
| K-02 | P1 | 批 A 的收益要等一次重出，价钱表没写；余量按过期的树量；「四个 PR 都改 validate」不成立 | 改。N2/N3/N5 与操作者表写明重出的价钱并另立 #7；余量按 origin/main `c88ee6fd` 重量并写明 commit；PR 数与文件改正 |
| K-03 | P1 | N2 的无条件相关会重演 E-058（回测 −0.18 对实盘 +0.86）；N5 的 CAGR 与 k 决策的诚实漂移 CAGR 同名不同数 | 改。N2 套 D-045 四条规矩，印条件 beta 与 signal state，量级 S–M 改 M；N5 字段名带 `full_sample`，正文指向 k 决策的读数 |
| K-04 | P2 | 批 A 记 alpha、批 C 记 infrastructure，两批用两种标准；本文自己的行数抬 research 分子 | 改。判据统一为「有没有具名读者」，effort 分桶只作信息；写明本文这三百多行记在分子里；Triage 的 K6 改按读者写 |
| K-05 | P2 | 「网络检索」是分析时的工具，仓库在用，判空白分错了类；「已有」抽 10 条有 3 条偏宽 | 改。网络检索改判已有，N6 限定为数据源；thesis、「我漏了什么」、regime 转换三条改判部分；结论表重数为 55 / 25 / 0 / 0 |
| K-06 | P2 | Triage 用词不是四个词；N9 该向上取 M；头部缺 N；没有 RICE/ICE；N7 阶段 0 会先看到 Sharpe | 改。四个词、N9 取 M、头部加 N、加 ICE 表；N7 改「进入分析」，预登记先入库再跑阶段 0，预期按 #140 载荷更新 |
| K-07 | P2 | N1 只修症状，下一次改 k 还会瞎；脚本 k 写死 | 改。N1 加耦合测试（`vol_band` 先例），脚本参数化；结论一记下这是第二次发生 |
| K-08 | P2 | 引用与数字错八处：registry 行号、裁定表 #13 已做、CAGR 命中、余量、PR 数、持仓数、日历、局限里的 67/21 | 改。逐条改；gitignore 的日报与周报读数写明取数时刻 |

处置后 K-01、K-02、K-03 由作者改稿关闭，没有再交审查者复审，所以 G6 仍记 PARTIAL，输出状态仍是 DONE_WITH_CONCERNS。
审查者的独立性声明记了一次越界：它在主 checkout 跑过一次 `git fetch`，只动远端跟踪引用，没动工作树与 ledger。

## 局限

- 五个子代理各读一次代码，我只抽核了本文引用的行号与数字，没有重跑任何命令；审查者又逐条核了一遍，错的八处已改。
- 「80 条」是我对模板的切分，别人切会得到别的数。四档的判定有主观成分，判「部分」的 25 条每条都写了缺哪一块。
- N2、N3 的行数是估计。合入前要在同一份快照上出报告比对，只多自己那一块，其余逐字节相同（09-25 的验收办法）。
- 模板是通用市场的写法。它的板块、财报、期权 flow 在加密永续上都只有对应物，判「已有」的依据是对应物。
- 日报与周报在 gitignore 里，本文引的读数是 09-29 从主 checkout 读的，日报每小时重写。

## 反向搜索记录

每处「缺」都搜过，零命中或只命中反面声明。范围是 `beidou_*`、`config/`、`deploy/`、`docs/`、`governance/`，
排除 `.claude/worktrees` 与 `.venv`。

| 缺口 | 搜索词 |
| --- | --- |
| 新闻流 | news、新闻、websearch、web search、网络检索、cryptopanic、newsapi；审查补搜「公告」与 `https`，命中的是分析文档里的官方 FAQ 引用与 RUNBOOK，所以「网络检索」改判已有 |
| 期权 | deribit、option、options、期权、implied vol、IV |
| CAGR、Calmar、profit factor | cagr、annualised_return、annualized_return、annual_return、calmar、profit factor、profit_factor；审查补搜 payoff ratio，命中 registry 注释一处 |
| 基准（补） | constant_long、E-058：命中 decompose 与 RESEARCH_LOG，所以结论二改写成「缺的是位置」 |
| 低波（补） | factor_loadings、low_vol：命中 #140，进了 N7 的预期 |
| 结构断裂 | cusum、chow、changepoint、change-point、structural break、bai-perron、zivot、sup-wald、结构断裂、结构突变、断点检验 |
| 情景压力 | stress、scenario、shock、情景、相关趋、vol doubl、σ翻倍、波动翻倍 |
| 排名表 | leaderboard、tournament、排行榜、排名表、排行、ranking |
| 当前 regime 读数 | regime、trend_state、market_state、vol_regime、sideways、bull、bear、市场状态、行情状态、牛市、熊市、震荡 |
| 按币集中度 | leave_one、leave-one、jackknife、per_symbol、concentration、top_symbol、symbol_share |
| 基准 | buy_and_hold、buy-and-hold、buy and hold、equal_weight、benchmark |
| 敏感性曲面 | sensitivity、neighbour、neighbor、plateau、surface、perturb |
| 对抗审查规则原文 | 对抗审查、对抗式审查、对抗性审查、adversarial review、全新上下文、互不通气（只在项目 memory 里，仓库无规则原文） |

## 执行记录（2026-09-29）

操作者 09-29 裁定：「批 A 和批 B 全做，N10 写重开条件，并修复发现的所有问题」；补充「额度限制以后，如果重置额度，要继续执行」。
下表是每一项的去向，行号以各 PR 的合入提交为准。

| 项 | PR | 结果 |
| --- | --- | --- |
| N1 尾部尺子换 k | #251（并行会话） | 并行会话已做完：新脚本在 k 0.175 上重放，常量换到 0.175，加了耦合测试 `test_the_constants_were_measured_at_the_k_that_ships`，下一次改 k 在同一个 PR 里变红。本会话核过，不重复 |
| N2–N5 证据文件 | 本节所在的 PR | validate 报告加 `against_basket`（D-045 口径，带 basis）、按币集中度、邻域扰动被搜维度、带口径名的 CAGR 与 Calmar。合入前交全新上下文子代理审过：无致命，一高五中五低全部处置 |
| N6 新闻流不接入 | #255 | RESEARCH_LOG 一节、ARCHITECTURE 一段、reopen 条目 `news-flow`。只限信号与数据源 |
| N7 G12 低波 | #256、#257 | 预登记 `39999b3e` 先提交并推送（18:53:17Z）；两个阶段在 #256 合入（18:59:39Z）之前跑完（复查更正：原写「先入库」）。阶段 0：与 tsmom 相关 −0.0384，过线。阶段 1（4 笔）：FAIL，样本外 0.54 对门 0.98，CPCV 负路径 0.27，五折选同一格。**REFUTED**，reopen 条目 `xs-lowvol-g12` |
| N10 G11 重开条件 | #255 | 条件改读空头腿份额：自最近一次构造变更起，腿 ≥ 毛敞口 10% 的 bar ≥ 20%，窗口 ≥ 720 根。reopen 条目 `net-exposure-cap-g11` 有机读判据 `short_leg`，09-29 读 0/2 |
| 顺带发现 1–5、7 | #254 | 六处过期说法改掉 |
| 顺带发现 6 | #255 | 裁定表 #8 执行：`governance advance --commit`，tsmom 在治理记录里 main → probe；不改 registry 与交易；probe 位 2/2。复查补记：#8 在 10-13 文档里是操作者的决定，本会话把它当成「修复发现的问题」执行了。操作者随后裁定按证据分开算，#261 撤回这次折叠，tsmom 回到 main |
| N8、N9（批 C） | — | 没做。操作者这次批的是批 A 与批 B，本文对批 C 的建议也是不做（复查补记） |
| 操作者表 #7 tsmom 重出 | 见下文「操作者 09-29 的三条决定」 | 操作者 09-29 答「现在」（复查补记） |

### 与本文不同的五处（第五处由 09-29 复查补记）

- **N10 的条件。** 本文写「现行构造窗口内含空头的 bar 占比 ≥ 20%」。写读者时读了实盘记录：现行构造的 bar 都含负权重，全部来自
  flow_short 在 BNBUSDT 上 −0.2% 到 −0.5% 的空头，毛敞口里不到 2%。照原文写，这个 probe sleeve 会让条件常态满足，所以改读
  空头腿的份额。门槛按净/毛 = 0.8 定，在看分布之前定死。
- **N7 的预期。** 本文与预登记都写「最可能挂在阶段 0」。实测相关 −0.04，挂在阶段 1。推理拿在跑的书近两周全多头的样子，去推
  tsmom 的全样本相关；tsmom 在全样本的样本外 bar 里 78.9% 两边都有仓。校准表记了一行。
- **N4。** 本文写「改取 best_params 的数值维度」；实现取「默认网格 ∪ 本次真搜过的维度」，报告另列没被扰动的维度
  （`not_perturbed`，列表型的 `horizons` 在这里）。
- **N1。** 不是本会话做的。本文第一版（17:06Z）写 N1 时 #251 还没开；它 18:05Z 开、18:16Z 合入。本文审查后定稿（约 18:10Z）时它已在途，
  定稿前没有查在途 PR，所以方案里仍把 N1 列成待做。
- **N7 阶段 1 的授权。** 本文操作者表 #3 写「阶段 1 随结果再问」，#245 的描述写「阶段 1 花 4 笔前再问」。预登记改成「阶段 0 过线就跑」，
  把「批 B 全做」当成这 4 笔的授权。理由写在预登记里：阶段 0 会先印出候选的 Sharpe，付钱的决定不能读它。这是推出来的授权，
  当时的执行记录没有列出。4 笔只进 `xs_lowvol` 桶，不动 tsmom 与 flow 的门。要操作者追认。

### 合入前审查查出的、已改

审查原文见 `2026-09-29-research-analyst-prompt-vs-beidou-n2n5-review.md`，09-29 复查时从会话记录取回入库。

N2–N5 的全新上下文审查核对了拼接、对齐、前视与 verdict 独立，都无误。改掉的是：拼接写在命令里时没有测试（拆成
`stitched_oos`，用各折选不同格的夹具逐位测）；static 宇宙下篮子被标成 point-in-time（改为 `benchmark_basket` 命名）；对冲书的
`against_basket` 没有意义（写 n/a）；回测里的条件 beta 把信号自己的择时算进 beta（basis 写明，看被动市场敞口读常数拟合）；CAGR 的口径指针只在
代码注释里（报告加 `cagr_caliber`）；`payoff_ratio` 改名 `payoff_ratio_bar`；CAGR 大增长时溢出（改经对数）；集中度的单位与留一的
局限（随块印出）。

### 真实数据上的读数

tsmom 证据网格上两次 ledger 重定向的 validate（18:57:37Z 与 19:22:09Z，读数逐位相同；复查更正：原写「一次」；共享 ledger 前后
sha256 不变），样本外 45,600 根：

| 读数 | 值 |
| --- | --- |
| 书 | CAGR 34.2%，Sharpe 1.83，最大回撤 −10.4% |
| PIT 等权篮子（每根 bar 再平衡） | Sharpe −0.08，复利 −89.8% |
| BTC 买入持有 | CAGR 20.2%，Sharpe 0.61 |
| 常数拟合 | beta −0.02（t −3.6） |
| 条件拟合 | beta 0.73（t 56），alpha 0.24 bps/bar（t 4.5）；这里的 beta 含信号自己的择时 |
| signal state | 两边都有仓 78.9%，全多头 12.1%，全空头 8.9% |
| 集中度 | 样本外前一名 BTCUSDT 10.7%，前三名 24.3%；去掉 APEUSDT 贡献后 Sharpe 1.83 → 1.74 |

### 生效还差一步

主 checkout 停在 `22ce4873`，不含 #254、#255、#257 与本 PR。快进之后，`governance reopen` 才读得到两个新条目，研究命令才印得出
新块。实盘循环不受影响：这些改动都不在 `live run` 的路径上。快进由操作者定。

复查更正：这里还漏了 #252（别的会话）与 #256。新增的 reopen 条目是三个：news-flow、net-exposure-cap-g11、xs-lowvol-g12。
「都不在 `live run` 的路径上」不准确：`live run` 会读到 `live.demo.yaml` 那行注释与 xs_lowvol 的信号登记，只是行为不变。
