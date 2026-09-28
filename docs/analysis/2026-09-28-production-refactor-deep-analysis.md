# 深度分析：北斗 V5 面向生产的重构方案——alpha 模块不重排，生产边界与变更成本才是可动的杠杆（V5 · 2026-09-28）

> 深度分析 V3.8 · 等级 L · 只写方案不执行（操作者原话：「先写方案，不要执行」）。Phase 7 由 Opus 5.5 独立子代理执行（操作者点名）。
>
> **Reading Check**：本次理解为「操作者要一份覆盖整个北斗 V5 的深度分析与重构方案，重点放在 alpha 模块，方案要能直接指导把系统推向『生产』；先写方案、经 Opus 5.5 对抗审查后定稿，不执行任何改动；交付对象是单一操作者」。最高风险预设为 Pre-PR1（P0）「重排 `beidou_alpha` 的代码结构能提高可上线策略的吞吐或系统的生产可用性」。若它不成立——alpha 模块的瓶颈在统计功效与数据 parity，而生产差距在宿主、监控、部署与变更成本——那么方案的主体不在 alpha 包内，而是围绕它的生产边界与每次改动要付的税；alpha 包本身只补两件仪器。
>
> **Interaction**：Yellow（完整度 🟢用户 🟢场景 🟡损失 🟡成功标准 🟢约束 🟢可得证据：「面向生产」没有指标，损失只有定性描述）｜ 等级：**L**，命中「≥3 模块（alpha / live / cli / governance / data / deploy / tests）」「核心链路（实盘循环、启动门、部署）」「资金链路（demo；~~规则将带进真实资金，CLAUDE.md~~ **[R 修订]** 这句不在 CLAUDE.md，出自此前分析的 Interaction 行，E5；memory 记的是「production/mainnet explicitly out of scope for now」）」「AI/策略：自主决策、生产影响」；本档不覆盖：无默认省略 ｜ 当前决策上限：~~Weak GO（H3）~~ **[R 修订] PIVOT**（审查命中 H2、H1、H7，处理后仍命中 H3、H5，见 §7）｜ 输出状态：**DONE_WITH_CONCERNS**（六个问题交操作者，**治理类问题未答不执行任何默认**）｜ 外部动作授权：无（只读：仓库、`.beidou/live/{state,heartbeat,cycles}.json[l]`、`reports/`；跑了四道门与几段只读统计脚本；未跑 validate / mine / book、未写 ledger、未改配置、未重启、未下单）。
>
> **[R 修订，2026-09-28] 本文的冻结稿（§0–§6）在 Phase 7 被 Opus 5.5 独立子代理判 PIVOT：P0 ×2、P1 ×12、P2 ×3，全部 OPEN；G6 FAIL。** 抽到的数字全部复现，错在读法与引用（与校准表「缺的不是数据是读法」同形）。两条 P0：① WP-P1 把 dead-man URL 写进 `env.sh`，而本机**没有** `env.sh`，凭据来自 `~/.zshrc`——照文新建一个只含 URL 的 `env.sh` 会让下一次重启以 78 退出、告警静默（KILL-01）；② F-D 臂的算术不成立：275 次触碰 ratchet 文件里 267 次改的是 CEILING 数值，M-PR01 数的是「包长过了顶」的次数，WP-C1 动不了它、WP-C2 靠放宽控制压低它、D-PR03 的 +5% 预算按近周增速几天用完（KILL-02）。被推翻的段落**不删**，标 **[R 修订]**；修订后的方案见 §6.3 与 §8：**零裁定的卫生项一批（WP-P4、C6、C7、C8、C9、P3）按 Weak GO 的含义受控开工；凡改动一道控制的（ratchet、预算、重启、looks、guard、宿主外监控）全部改为交操作者的带价问题，未答不执行。** 审查稿原样存档：`docs/analysis/2026-09-28-production-refactor-adversarial-review.md`。审查者未复审本修订版。
>
> **Context Intake**：L 级首轮本应暂停问五类问题。本会话自治运行、操作者不在线，且指令链以「拿到最终方案」为终点，按 SKILL 3.2 规则 3 与 5.6「一次出完」推进：五问写在 §P1.6，未答项按 UNKNOWN 压低决策上限；操作者答任一条即改写对应 Scope。**这一步要记进校准表**：09-26 那行已把「Phase 1 没有暂停」记为流程缺口。
>
> ID 命名空间 `PR`（production refactor）。Constitution：仓库无 `deep-analysis-constitution.md`；硬约束取自 `CLAUDE.md`（公开仓库、PR 流程、`.venv` 四道门、重启纪律、ratchet 抬顶规则）、`tests/architecture/test_import_rules.py`（依赖方向）、`tests/architecture/test_source_budget.py`（ratchet）、`beidou_governance/policy.py`（0.3.6）、`governance/reopen.yaml`、`docs/RUNBOOK.md`。§10.4 建议操作者用本文答案生成一份 Constitution。
>
> 与本文并读：`docs/analysis/2026-09-17-alpha-module-deep-analysis.md`（alpha 模块逐行审查，PIVOT）、`2026-09-18-system-optimization-and-factor-module-deep-analysis.md`（「51 只活 2」的分母）、`2026-09-25-external-checklist-round-two.md`（否决「按 7 步流水线重排整个包」）、`2026-09-25-october-13-readiness.md`（日期开关）、`analysis-calibration.md`（13 行，G2 误判 11 次）。本文不重跑它们的实验，只引用并同引它们的修订段。

---

## 0. Decision Memo

**[R 修订]** 冻结稿的 Memo 第一行是 **Weak GO（H3）**，授权「受控执行 Phase 0–1」。审查判 PIVOT（§7）；下表是修订版，被推翻的原句用删除线保留。

| 项目 | 结论 |
| --- | --- |
| Final Decision | **PIVOT**（审查命中 H2「未处理的强反证」——09-06 D-P4 的具名 ACCEPTED、`cycles.jsonl` 的重启行与 ERROR 行、CEILING 的逐提交构成；处理后仍命中 H3、H5）。问题成立，方案改形：~~受控执行 Phase 0–1~~ → **只有零裁定的卫生项一批按 Weak GO 的含义受控开工**（WP-P4 归档专属测试归位、WP-C6 armed 进程 import 闭包、WP-C7 接线点类型、WP-C8 exits 同输入测试、WP-C9 配置键读者测试、WP-P3 日期开关登记表——六项零构造、零 ledger、零 Policy、不改任何一道控制）；**其余全部改为交操作者的带价问题**（Q1–Q6，§P1.6），未答不执行任何默认。**alpha 模块本身不重排、不重写**这一条从「作者决定」改为**交操作者的问题 Q6**（D-PR04 [R 修订]）：耦合一半有读数支持（09-10 起 5 个信号提交里 4 个不碰非 alpha 源码，审查者 R-18），吞吐一半 UNKNOWN（GAP-PR06 未跑），09-17 的残项「先 M6 后下沉」第二半未做（`docs/analysis/2026-09-17-alpha-module-deep-analysis.md:498`） |
| 一句话问题 | 系统在 demo 上已能无人值守（598 周期、昨日 0 次漏再平衡、四道门在 CI 全绿），但「生产」没有定义、没有读数：宿主离线时没有任何东西会通知操作者（09-06 他具名接受了这条残余风险，而当时选的「第二通道」在本机只导出了一个 webhook 变量）；退出规则只在软件里、场地上没有一张保护单；mainnet 被 `guard.py` 结构性拒绝、冲击成本系数是 E5——这三项都属于操作者尚未重开的 mainnet 范围。近 13 天真正丢掉的 7 根 bar 全部来自传输层的失败周期（6 次代理 503、1 次 venue 错误），**0 根来自重启**。变更成本一侧，真正没被裁定的是**非 alpha 源码的增长率**（09-21 → 09-28 每天约 400–1,400 行）：ratchet 每 3 次提交被触碰一次，是因为包每 3 次提交就长过一次顶；本地专属测试自 09-25 起在操作者机器上是红的而 CI 看不见。~~部署等于重启（24.4 天 60 次）~~ **[R 修订]** 重启多，但 09-15 起没有一次因时机丢 bar（E-PR39）。V5 计划自己写下的 KILL（C-006「重建再次膨胀成治理工程」）已按它自己的度量成真，且自 09-04 起是一个开放的操作者决定 |
| 选定的问题框定 / 放弃的框定 | D-PR00：选 F-C「生产边界问题」+ F-D **[R 修订]**「非 alpha 增长率没有裁定」（原「变更成本 = ratchet 税」被 KILL-02 推翻）；F-B「alpha 是功效与 parity 问题」承认为真但**不由重构解决**，只补一件仪器（WP-A1）；F-A「alpha 代码结构问题」~~被证据排除~~ **[R 修订] 未判**：耦合一半有读数（R-18），吞吐一半 UNKNOWN（GAP-PR06），交 Q6 |
| Success Definition（领先·滞后·护栏，**[R 修订]** 按问题重写，§3.5） | **M-PR01**（领先）非 alpha 源码行数的日增长率与整周口径的 alpha 投入占比，今天约 400–1,400 行/天、32.5% / 36.2% / 21.5%（近三周）；**M-PR02**（领先）只在操作者机器上跑、且状态与 CI 不同的测试数，今天 7 条里红 1；**M-PR03**（滞后）每月因**失败周期**与因**重启时机**各丢掉的 bar 数，09-15 起：7 / 0；**M-PR04**（护栏）任何 PR 前后三个 digest 逐字不变且 `live verify --check` 差异为 0；**M-PR05**（护栏）`trials.jsonl` sha256 不变；**M-PR06**（滞后）每个候选数据族的「实盘覆盖 bars ÷ 信号 lookback」有日常读数，今天无；**M-PR07**（滞后）宿主离线到操作者知道的最长延迟，今天无上限 |
| 推荐方案 / In Scope / Out of Scope / 被接受的损失与补偿 | D-PR01 **[R 修订]**：**一批卫生项**（WP-P4、C6、C7、C8、C9、P3；零裁定、零构造、零 ledger、每项一个 PR、按 D-PR05 协议验收）+ **六个带价问题**（Q1 生产定义含「只 L-A」；Q2a/Q2b ratchet 记录搬迁与 headroom 政策分开问；Q3 是否重开 09-06 的 D-P4，附两条新事实；Q5 非 alpha 增长率的裁定；Q6 「先 M6 后下沉」第二半开不开工）+ **一项由操作者做的动作**（换代理节点，09-22 判读的唯一便宜解）+ **两项零 ledger 测量**（GAP-PR06 validate 每格耗时；GAP-PR10 失败周期窗口内重试的收益）。Out：WP-P1（改为 Q3）、WP-P2（问题 09-15 起未发生）、WP-C1/C2/D-PR03（改为 Q2/Q5）、WP-C3（补偿不存在，KILL-15）、WP-P6（不动 `guard.py`；09-05 已有解除条件；随 Q1）、WP-A2（等 HC-6 计入口径裁定）、WP-P5 前半（GAP-PR04 已由 09-27 的 `allow` 行关闭）；以及原 Won't 全部。被接受的损失：操作者要答六个问题（补偿：每个带价、带默认**但默认不执行**，答一个即解锁一块）；卫生项不带来任何可见收益（补偿：每项 ≤ 1 天、可 revert） |
| 最大价值 / 最大风险 | 价值：把「生产」从形容词变成七个读数，并把两条**已经在发生、没人量过**的事写下来——传输层每两天丢一根 bar 的退出检查；非 alpha 每天长几百到上千行。风险：RISK-PR02——操作者第六次听到「不重排 alpha」而把本文读成又一次「等」；补偿是六项卫生项本周可落，且 Q6 把 alpha 结构的问题交回他手里而不是替他答 |
| Strategic Fit / Relative Value / Economic | ~~High~~ **[R 修订] Medium**（卫生项是工具卫生，不移动 alpha；mainnet 在 memory 里明写 out of scope）/ 卫生项 Strong（零计费、零构造、修的是复核成立的五处缺口）；~~Phase 2–3 Adequate~~ **Unproven**（KILL-12，H5）；「重排 alpha」Unproven（09-25 执行记录写明没做，依据是清单覆盖，不是裁定）/ N/A（单人） |
| 开放 P0/P1 / Need Evidence | A-PR01「生产的定义」UNKNOWN（Q1）；C-PR01 吞吐一半 UNKNOWN（GAP-PR06）；GAP-PR08 ratchet 文件的合并冲突次数（「税」有没有独立于增长的成本）；GAP-PR10 失败周期窗口内重试能救回几根 bar；HC-8 BNX 夹具与归档哪边对 |
| G0–G7 一行 | G0 PASS · G1 **PARTIAL**（F-D 把症状写成根因，KILL-02/17）· G2 **PARTIAL**（数字全对；读法四处、引用五处、过期一处，全部已按 §7 更正）· G3 **PARTIAL**（同尺子重定价后卫生项 Strong、其余 Unproven）· G4 **PARTIAL**（机会成本改按操作者注意力定价）· G5 **PARTIAL**（WP-P1、WP-C2、D-PR03 不进契约）· **G6 FAIL → 见 §7**（17 条 Kill 全部以撤方案、改状态、改范围或新证据处置；审查者未复审）· G7 只为卫生项写契约 |
| 本档不覆盖 | 无默认省略 |
| 下一动作 | 不需裁定即可开工：WP-P4、C6、C7、C8、C9、P3（§9.4/9.5）；操作者：换代理节点（09-22 判读），答 Q1–Q6 |

---

## 1. Gate Summary

**[R 修订]** 冻结稿把 G1–G5 全写 PASS，其中 G2、G4 的「未通过项」一栏里明明列着未通过项——审查 KILL-13(c) 指出这与 09-26 校准行记的是同一个错。修订版：

| Gate | 状态 | 追溯到本门公理的 Claim IDs | 未通过项 | 决策上限 | 下一动作 |
| --- | --- | --- | --- | --- | --- |
| G0 Interaction / Kill | PASS | — | 无 FATAL；K4 WARNING → §3.5；K5 WARNING → §8.2；**[R 修订]** K7「全部改动可由 git 回滚」删去——`env.sh` 在仓库外（KILL-01）；K8 的依据改为已写出的 §9 | — | — |
| G1 Problem / Axiom | **PARTIAL** | C-PR01、C-PR02、C-PR05（A1） | F-D 曾把症状（增长事件计数）写成根因（税），KILL-02；Success Definition 部分按方案反推，KILL-17；两者 §3.4/§3.5 已重写，但 Reframing 的 F-A 仍是「未判」而非「已排除」 | PIVOT | Q6 |
| G2 Evidence / Reality | **PARTIAL** | C-PR01、C-PR03、C-PR04（A2） | 数字全部复现；读法错四处（触碰构成、2,154s、19.71% 的窗口、凭据位置）、引用错五处（C-01、02、04、05、06）、过期一处（GAP-PR04）——全部已更正并加 E-PR36–42；C-PR01 吞吐一半 UNKNOWN | PIVOT（H3） | GAP-PR06、GAP-PR08 |
| G3 Relative Value | **PARTIAL** | C-PR01、C-PR03（A3） | O-PR4 与本文 WP 已按同一单位（毛触碰行数、同一验收协议）重定价（§4.2 [R 修订]）；卫生项 Strong，其余 Unproven（H5） | PIVOT | — |
| G4 Strategic / Economic | **PARTIAL** | C-PR05（A4） | 机会成本改按操作者注意力（问题数、PR 数、日报新增节数）定价（§4.3 [R 修订]）；19.71% 是 23 小时的读数，整周口径 32.5% / 36.2% / 21.5%（E-PR41）；Strategic Fit High → Medium | — | — |
| G5 System / Solution | **PARTIAL** | C-PR02、C-PR03、C-PR06（A5） | WP-P1、WP-C2、D-PR03 不进契约；其余 WP 的包、行数、ratchet 影响、生效时点齐 | — | — |
| G6 Adversarial Survival | **FAIL → 已处置**（§7） | C-PR01、C-PR02（A6） | 17 条 Kill：CLOSED 11（撤方案、改范围、新证据）、MITIGATED 4、ACCEPTED 2（流程缺口，记进校准行）；**审查者未复审本修订版**，所以本行不写 PASS | PIVOT（H7 处置后不再单独压顶；H1 无 OPEN P0） | 下一份分析对照校准行 |
| G7 Delivery / Learning | **PASS（限卫生项）** | C-PR02、C-PR04、C-PR06（A7） | 只为六项卫生项与六个问题写契约；WP-P1 与 F-D 臂不写（审查 G7 意见） | — | §9–§10 |

命中硬门禁：**H2**（审查时存在未处理的强反证——09-06 D-P4、`cycles.jsonl`、CEILING 逐提交构成；本版已处理，决定词按 PIVOT 记）、**H3**（A-PR01 UNKNOWN；C-PR01 吞吐一半 UNKNOWN）、**H5**（卫生项以外 Unproven）。H1：冻结稿 2 条 P0 均以撤方案/改范围 CLOSED（§7），OPEN P0 = 0。H7：L 级 G6 曾 FAIL；处置后审查者未复审，故上限保守记 PIVOT 而非回到 Weak GO。**H8 未命中**：资金边界已知——demo 场地、`guard.py` 拒 mainnet、凭据来自 `~/.zshrc` 的 `export BEIDOU_*` 行（~~`env.sh`~~，本机没有这个文件，E-PR37）。

---

## P1. Phase 1 · Reality Check

**Prerequisites**：无。**Produces**：Pre-PR1–5、四问表、来源与偏差、Early Kill、G0、Context Intake Q1–Q5。**Downstream**：§3 框定、§8 Scope、§9 契约。

输入类型：**方案型**（「重构方案」）+ **愿景型**（「面向生产」）+ **执行型**（「调用 Opus 5.5 审查」是流程要求，不是分析对象）。方案先移除：问题是「系统离生产还差什么，每次改动要付什么」，不是「怎么重排代码」。

### P1.1 需求四问

| 问题 | 当前答案 | Evidence IDs | 状态 | 缺口 |
| --- | --- | --- | --- | --- |
| 谁的需求，谁承担损失？ | 单一操作者。损失三类：**时间**（54 天 2,205 次提交，约 41 次/天，几乎全部由 agent 会话产出，操作者读、裁、合）；**错误率**（校准表 13 行，G2 误判 11 行，同一种「读法」错误重复 6 次）；**机会成本**（alpha 投入占比 19.71% 对目标 90%，且这个读数 12 天没人产出） | E-PR05、E-PR19、E-PR32 | 已确认 | — |
| 频率、规模和损失多大？ | 循环每小时一次，17 个标的，demo 权益约 13.4k（42.77% 是 BTC 抵押品）；24.4 天 60 次重启（2.46/天）；每次重启若跨过 bar 收盘就少一根 bar 的退出检查；~~昨日一次重启让周期迟到 2,154 秒~~ **[R 修订，KILL-06]** 那 2,154 秒来自一次安全窗口内的重启，那根 bar 已再平衡，不是代价；**09-15 起 0 根 bar 因重启丢失，7 根因失败周期丢失**（6 次代理 503、1 次 venue 错误，E-PR39） | E-PR06、E-PR07、E-PR39 | 已确认 | ~~GAP-PR03~~ 已关；新缺口 GAP-PR10：失败周期在窗口内重试能救回几根 |
| 用户现在怎么绕过？付出什么代价？谁在做？ | **绕行 = 反复要求审查与重构**：09-03、09-17（×3）、09-18 四份 alpha 分析，09-23 与 09-25 两轮「重构北斗」都被转成定向批次；**agent 的绕行**：每 3 次提交就有 1 次要抬 ratchet 并写理由（31.9%）、给 scratchpad 脚本保 35 个再导出地址、用 worktree 躲并行会话（今天积了 15 个）。上一次：2026-09-25 操作者再给同一份外部清单要求「重构」，会话给了四块带价选项、全部落地、并写明「没做的那一种：按 7 步流水线重排整个包」 | E-PR03、E-PR21、E-PR22、E-PR31 | 已确认 | — |
| 现有方案能否满足大部分需求？ | **demo 无人值守：能**（598 周期、护栏、幂等重启、审计线）。**新 alpha：不能，且不是代码的问题**（功效表：空桶 16 格网格真 Sharpe 1.0 过门 32.7%；tsmom 桶 9.6%）。**真实资金：不能**（`guard.py:16` 只放行 demo/testnet；冲击系数 E5；KILL-Q12 自 09-05 把真实资金排除在范围外） | E-PR06、E-PR11、E-PR18 | 已确认 | 「生产」指哪一层：GAP-PR02 |

### P1.2 需求来源与偏差

| 渠道 | 具体来源 | 背后样本量 | 已知偏差 | 对证据等级与范围的折减 |
| --- | --- | --- | --- | --- |
| 内部想法（操作者） | 本次指令；09-23、09-25 两次「重构」请求；09-17 alpha 审查请求 | n=1，但是唯一用户 | HiPPO 不适用（单人）；~~**近因**：09-27 carry_hedged 判负、两个新数据族 NO-GO 之后再提「alpha 模块尤其」~~ **[R 修订，KILL-03(d)]** 把操作者的强调记成偏差是作者替他作答：「尤其是 alpha 模块」是一条要回答的要求，本版以 Q6 回答；**锚定**：「重构」是方案词 | 操作者对「系统离生产还差什么」的判断是 E2（他每天读日报与告警）；「alpha 模块结构是瓶颈」这句话是作者从「尤其是 alpha」改写出来的命题（Pre-PR1），不是操作者的原话 |
| 仓库自述 | 44 份分析、18,058 行 RESEARCH_LOG、校准表 | 全部 E1 但由 agent 写 | **叙事惯性**：09-18 分析的 RISK-SY02「顺着仓库两周『否决有效』的叙事写」被审查证实发生过；本文引用它们时同引修订段，并另找仓库没修的生产缺口（§2.1 E-PR08、E-PR14、E-PR19、E-PR20） | 结论级 E1；「已否决」类结论只在其网格与构造范围内成立 |

### P1.3 需求预设清单

| Pre ID | 提出者默认成立的前提 | P级 | 依据来源 | 若不成立会改变什么 | 处理 |
| --- | --- | --- | --- | --- | --- |
| Pre-PR1 | 重排 `beidou_alpha` 的结构能提高 alpha 产出或生产可用性 | **P0** | 「重构方案」「尤其是 alpha 模块」——**[R 修订]** 这是作者对操作者原话的改写，不是原话 | 方案主体从 alpha 包移到它周围；alpha 只补仪器 | 登记 A-PR00；~~§3.4 用三条证据判 F-A 不成立~~ **[R 修订，KILL-03]** 三条证据测的是相邻命题（import 方向、行为级 bug、已测候选的死因），F-A 改记「未判」；D-PR04 改为交操作者的 Q6 |
| Pre-PR2 | 「面向生产」有一个大家都懂的定义 | **P0** | 「真正面向生产」 | 生产=demo 无人值守 / mainnet 小额 / mainnet 目标资金，三种答案给出三种 Scope | 登记 A-PR01；Q1（Q-CRITICAL）；D-PR02 给三层定义与默认 |
| Pre-PR3 | 系统当前不是生产级 | P1 | 同上 | 若 demo 层已算生产，方案只剩变更成本与 mainnet 准入 | 分层回答：demo 层是，真实资金层不是（C-PR02） |
| Pre-PR4 | 一份方案能一次覆盖全系统 | P2 | 「全面分析」 | Scope 无法收敛（K5） | §8.2 Scope Firewall；每个 Won't 写重开条件 |
| Pre-PR5 | Opus 5.5 审查 = 独立审查 | P2 | 「调用 OPS5.5 模型」 | 若审查者拿到作者推理，独立性只剩「不同模型」 | §7 只给冻结文件与原始证据路径；独立性声明写明 |

### P1.4 Early Kill

| ID | 判定 | 依据 |
| --- | --- | --- |
| K1 FATAL | 不成立 | 用户、场景、损失可观察（P1.1） |
| K2 BLOCKER | 不成立 | 34 条证据全 E1（§2.1） |
| K3 WARNING | 不成立 | §6.1 比较了 No-Build 到 System-Build 五档 |
| K4 BLOCKER | **WARNING** | 「面向生产」无指标 → §3.5 先写 Success Definition 再谈方案 |
| K5 WARNING | **WARNING** | 「全面」→ §8.2 Scope Firewall |
| K6 WARNING | 不成立 | 每个 WP 都要求净价值 > 它加的行数与维护（§6.1 定价列） |
| K7 FATAL | 不成立 | 全部改动可由 git 回滚；不启用 mainnet；不动资金 |
| K8 BLOCKER | 不成立 | §9 每个 WP 有 Test / Acceptance |

**G0：PASS**。

### P1.5 输入的执行型部分

「调用 Opus 5.5 审查」按 SKILL Phase 7 执行：先落 `<项目>-frozen.md`，子代理 `model: opus`，只给冻结文件与原始证据路径，不给本会话的推理。审查结果原样存档为 `docs/analysis/2026-09-28-production-refactor-adversarial-review.md`（先例：`2026-09-07-exits-tail-adversarial-review.md`）。

### P1.6 Context Intake（五问，未答按 UNKNOWN 推进）

**[R 修订]** 冻结稿的五问里，Q3 的答案在仓库里已有（09-06 D-P4，KILL-05）、Q1 少了操作者最后记录的那个选项（KILL-07）、Q4 被 09-27 的 `allow` 行取消（KILL-14）、Q2 把两件事绑在一问里（KILL-10）；且「未答按默认执行」在 CI 绿即自动合并的流程下等于 agent 替操作者改治理规则（KILL-10）。修订版（原表保留在 git 历史里，不重印）：

| Q ID | 问题 | 关联 | 若答案为 A → 结论变化 | 若答案为 B → 结论变化 | 决策影响 |
| --- | --- | --- | --- | --- | --- |
| **Q1 · Q-CRITICAL** | 「面向生产」指哪一层：**A = 只做 L-A**（demo 无人值守做完；memory 记着你 09-14 前后的范围「production/mainnet explicitly out of scope for now」）；B = L-A + mainnet 小额校准的准入**设计**（不启用，且不动 `guard.py`）；C = 现在就要 mainnet 目标资金 | Pre-PR2、A-PR01、D-PR02、WP-P6 | A：WP-P6 与 Phase 3 出 Scope；「生产」的读数只剩 M-PR02/03/06/07 | B：WP-P6 只写文档、引 09-05 系统质量分析附录 D 的解除条件，`guard.py` 一字不动；C：PIVOT 到另一份分析（H8 边界全部重问） | H |
| Q2a | ratchet 抬顶**理由**可以从 `test_source_budget.py` 的注释搬到 `docs/SOURCE_BUDGET_LOG.md`（常量旁只留编号引用；CLAUDE.md 那条规则相应改写）吗？——它买的只是文件从 4,395 行缩到约 200 行与更小的冲突面，**不改变触碰频率**（KILL-02(b)） | A-PR02、WP-C1 | 做（一次 PR、逐字搬迁、你合并） | 不做 | L |
| Q2b | headroom 要不要从「惯例约 40 行」改成写死的政策？政策会让 87% 的历史抬幅不再逐次写理由（审查者 R-03c），也就是**放松**这道控制。若要，数字你定；本文不给默认 | WP-C2、C-PR03 | 定一个数并接受损失（增长事件不再逐次留记录） | 维持惯例；M-PR01 的触碰频率不变 | M |
| Q3 | 09-06 你答 Q1=B（没有远端介质）、Q3=A，D-P4 取「同机 + 第二通道 + 只告警」，宿主死亡的残余风险 ACCEPTED。两条新事实：① 本机 `~/.zshrc` 只导出一个 webhook 变量（`BEIDOU_ALERTS_WEBHOOK_URL`，没有 `_2`），所以 O-X1 的「第二通道」若未在别处配置就不存在；② 你接受的文字是「告警 ≤ 2 bar」，而宿主死亡时同机的巡检也死，告警是 0。**要不要重开 D-P4？** 若重开，A = 第三方 dead-man（URL 是一枚能压制告警的令牌，与凭据同级；ping 要从巡检末尾无条件发；变量放在凭据实际所在的位置——今天是 `~/.zshrc`，**不新建 `env.sh`**）；B = 只补第二通道 | GAP-PR01、D-P4、KILL-01、KILL-05 | 重开 → 按 09-05 KILL-R2 的失败形态逐条写规格后再定价（本文不写） | 不重开 → 记一次「2026-09-28 复核，维持 ACCEPTED」 | H |
| Q5 | 非 alpha 源码 09-21 → 09-28 每天长约 400–1,400 行（E-PR38），任何静态预算都会在几天内被越过；ratchet 因此每 3 次提交被抬一次。这个增长率是不是你要的？A = 是，把 `PLAN_BUDGET` 改成「只记录、不断言」并写下理由（关闭 09-04 的开放决定）；B = 不是，给一个增长率上限或一个缩减目标与日期，本文另写方案 | C-PR05、D-PR03、E-PR38 | 改一次测试，理由同 commit，**你合并** | 另一份方案 | M |
| Q6 | 「尤其是 alpha 模块」要的是哪一件：A = 开工 09-17 裁定「先 M6 后下沉」的第二半（研究逻辑从 `beidou_cli` 下沉到 `beidou_alpha`，估 +1,000–1,400 行 alpha、要抬顶并带证据；scratchpad 的 42 个私有 import 是它的下游）；B = 先跑 GAP-PR06（validate 每格耗时剖析，零 ledger）看吞吐瓶颈在哪再定；C = 不动 alpha，接受本文 D-PR04 | C-PR01、D-PR04、GAP-PR06 | A：另一份带价方案 | B：一次剖析后回来；C：D-PR04 成立 | M |

**未答不执行任何默认。** 六项卫生项不在此列——它们不改任何一道控制，按 §8 直接开工。改 CLAUDE.md、`PLAN_BUDGET`、CEILING 政策、`guard.py`、凭据位置、重启、looks 计入口径的 PR 一律由操作者合并（§7 HC 表），不走 CI 绿即自动合并。

---

## 2. Phase 2 · Evidence Ledger

**Prerequisites**：G0 PASS。**Produces**：E-PR01–E-PR34、C-PR01–07、A-PR00–07、GAP-PR01–07。**Downstream**：§3 框定、§4 相对价值、§5 系统分析、§6 Option。

取证预算与来源：全部 34 条来自仓库、实盘状态文件、报告与本机命令（用户材料，按 SKILL 3.1 不计外部查证）；外部查证 0 次。代码检索约 60 次，其中约一半由四个只读子代理分担（live / cli+governance+data / tests / 既有分析），子代理的数字凡进入本文的都由主会话用同一命令复核过一遍（`wc -l`、`git log`、`grep -c`）。

### 2.1 Evidence Ledger

记录式，字段：类型 · 来源/位置 · 日期 · 范围 · 摘要 · 支持/反证 · 等级 · 置信度 · 敏感性。Owner 全部为操作者（单人仓库），不逐条重复。

**E-PR01** · CODE · `find <pkg> -name '*.py' -print0 | xargs -0 cat | wc -l`（2026-09-28，主 checkout `40cbe17c`）· 全仓 · 源码七包 43,842 行：alpha 10,936（42 文件）、live 15,318（42）、cli 8,623（25）、data 3,642（15）、governance 4,287（16）、exchange 747（6）、shared 289（4）；tests 60,226 行（317 文件）；scratchpad 82 个 .py 共 13,782 行且**入库**（`.gitignore:55`「scratchpad scripts ARE committed」）· 支持 C-PR03/C-PR05 · E1 · High · PUBLIC。

**E-PR02** · CODE · `tests/architecture/test_source_budget.py:88`、`:1-13`、`:4374-4383` · 2026-09-04 起 · 全仓 · `PLAN_BUDGET = {"beidou_live": 2_000, "non_alpha_total": 6_000, "alpha_share_tree": 0.60, "alpha_share_effort": 0.90}`；文件头：「Decided 2026-09-04: the operator carries the breach for now and revisits it as long-term work」；`test_the_plans_budget_is_recorded_as_breached_rather_than_quietly_redefined` 断言非 alpha > 6,000、live > 2,000、alpha 占比 < 0.60 **仍然成立**。今天：非 alpha 32,906（5.5×）、live 15,318（7.7×）、alpha 占比 24.9% · 支持 C-PR05 · E1 · High · PUBLIC。

**E-PR03** · CODE+GIT · `test_source_budget.py:2007`（`CEILING = {`）与七个条目；`wc -l` 4,383 行；`git log --since=2026-08-28 --no-merges --format=%h | wc -l` = 861，加 `-- tests/architecture/test_source_budget.py` = 275 · 近 30 天 · CEILING：live 15,358 / cli 8,656 / data 3,677 / exchange 747 / shared 289 / governance 4,310 / alpha 10,960 → headroom 40 / 33 / 35 / **0** / **0** / 23 / 24；**31.9% 的非合并提交触碰这个文件**，按改动次数它是仓库第二高频文件（仅次于 RESEARCH_LOG 的 315 次）· 支持 C-PR03 · E1 · High · PUBLIC。

**E-PR04** · EXPERIMENT · 本机 tokenize 脚本（按 token 类型分行：代码 / 注释+docstring / 空行）· 2026-09-28 · 七包 · alpha：代码 6,052、注释+docstring 3,226（**29.5%**）、空行 1,658；live 27.6%；governance 25.2%；data 24.1%；cli 18.0%；exchange 18.6%；shared 10.7% · 支持 C-PR01（alpha 的「大」有三成是设计记录，不是逻辑）· E1 · High · PUBLIC。

**E-PR05** · GIT · `git rev-list --count HEAD` = 2,205（2026-08-05 → 09-28）；近 30 个活跃日 1,231 次提交；`gh pr list --state merged` 191 个（#1 合于 2026-08-06T19:42Z），其中 182 个在 08-28 之后 · 支持 P1.1（时间成本）· E1 · High · PUBLIC。

**E-PR06** · DATA · `.beidou/live/state.json`、`heartbeat.json`（只读，2026-09-28 01:xx 本地）· 实盘 · `started_at 2026-09-03T07:58:44Z`、`restarts 60`、`restarted_at 2026-09-27T16:35:47Z`、`cycles 598`；心跳 `2026-09-27T17:00:31Z` `phase OK`、`construction 2ee491c13971`、`registry 7f8adb754962`、`universe_size 17`、`history_bars 1442`、`probes {flow_short: OK, main: OK}`；权益 13,436.6。重启率 60 ÷ 24.4 天 = **2.46/天** · 支持 C-PR02 · E1 · High · INTERNAL（demo 账户，只引汇总数）。

**E-PR07** · REPORT · `reports/daily/2026-09-27.md`「Restart cost」「Execution fidelity」「Collateral」· 昨日 · `missed_rebalances 0`、`restarts 3`、`worst_restart_late_seconds 2154.00`；滑点 5.1±2.1 bps（97 笔）对限 4.0 bps **带外**；换手条款「读不出（归档还没有覆盖这个构造的第一根 bar）」；registry digest 循环/磁盘一致；抵押品占权益 42.77% · 支持 C-PR02（运维读数在、但重启仍有代价）· E1 · High · INTERNAL。

**E-PR08** · EXPERIMENT · 主 checkout `40cbe17c` 四道门，2026-09-28 · `ruff format --check` 472 文件 ✓；`ruff check` ✓；`mypy` 150 文件 ✓；`pytest -m "not network"`：**1 failed, 2812 passed in 154.84s**。失败：`tests/live/test_bar_sanity_is_seen_and_never_traded_on.py::test_the_fixtures_are_the_archive_verbatim[BNXUSDT_2023-02-22.json]`——归档切片 552 行对夹具 48 行；`.beidou/data/klines/BNXUSDT/1h.parquet` mtime 2026-09-25 22:19（09-25 `data repair` 之后）；该测试 `:108-110` 带 `skipif` 归档不存在（CI 与 worktree 都跳过）。**本地专属、自 09-25 起红、CI 绿** · 支持 C-PR04 · E1 · High · PUBLIC。

**E-PR09** · CONFIG · `config/alpha_registry.yaml:71-80`、`:369-372`、`:405-426`、`:439-534` · 当前 · `ensemble.method mean`、`turnover_penalty 0.0`；`books.flow_short.fraction 0.333333`；tsmom `enabled true` `verdict WEAK_PASS`；flow `enabled true` `verdict REJECT` + `accepted_despite REJECT`（D-029）；其余五条 `enabled false` `evidence null`；`universe: []`（09-15 取消钉住）。文件 48 KB，值之外几乎全是审计注释 · 支持 C-PR01（在跑的是 1 主书 + 1 probe）· E1 · High · PUBLIC。

**E-PR10** · CODE · `beidou_alpha/model.py` `AlphaModel.from_registry`（`crowded` 分支）、`beidou_alpha/registry.py` `_unimplemented_turnover_penalty`、`beidou_alpha/signals/__init__.py` `register()` · 当前 · 一本书启用多于一条策略即 `ValueError`（「the ensemble that would combine them re-introduces the magnitude this construction was validated without … until then a book runs one strategy」）；`turnover_penalty` 非零即拒；运行期只许注册 `mined_*`。**ensemble 合成路径在实盘不可达**：架构今天是「一书一策略」+ 至多两个 probe（`Policy.max_concurrent_probes 2`）。`docs/ARCHITECTURE.md` 未找到这条限制的显式记录（grep「one strategy / 一个策略 / a book runs one / 每本书」零命中）· 支持 C-PR01（多策略平台不是代码缺陷而是未验证的构造）· E1 · High · PUBLIC。

**E-PR11** · LOG · `docs/RESEARCH_LOG.md:13678-13748`「F4 盘点」· 2026-09-19 · 功效表（`beidou research power`，se 0.4390）：桶 N≤4 门 1.0 → 真 Sharpe 1.0 过门 50%；N=16 → 32.7%；N=303（tsmom 桶）→ 9.6%；se 由 45,351 根样本外 bar 决定，减半要约 20 年；结论「没有第四条是『再找一个候选』」；七个手写族 1 活 1 probe 其余 REFUTED；mining 2,073 行最好候选三次 REJECT；pairs 19,772 行 REFUTED · 支持 C-PR01 · E1 · High · PUBLIC。

**E-PR12** · LOG · `docs/RESEARCH_LOG.md:17870-18058`「两个新信息族零 ledger 看过」· 2026-09-27 · 八个写法无一三条全过；B1 横截面 IC t 3.2–4.95 但与 tsmom 日相关 0.509 / 0.706；pit 成员表每行 15–20（`membership.parquet` 2,063 × 889，中位 18）；「中位 ≥ 50」宽度门写错；「这 8 次看已经花掉了……要算进 N」——**但没有任何机器可读的记录承接它**（不在 `trials.jsonl`，不在 `reopen.yaml`）· 支持 C-PR06 · E1 · High · PUBLIC。

**E-PR13** · LOG · `docs/RESEARCH_LOG.md:17717` · 2026-09-27 · carry_hedged REFUTED：样本外 Sharpe 0.68 对门 1.03，三分之一 CPCV 路径为负 · 支持 P1.2 近因偏差的存在 · E1 · High · PUBLIC。

**E-PR14** · DATA+CODE · `governance/verdicts.jsonl`（含一行未提交，`git diff` 可见）、`governance/governance_state.json`、`deploy/run_governance_gate.sh:29-31`、`beidou_governance/family_gate.py:1-30` · 2026-09-19 / 09-25 · family_gate 对 tsmom 两次 `refuse`：「OOS 1.2306 vs 1.5238 at N=183」「vs 1.5251 at N=185」；1.2306 是 `tsmom-validation-20260919T081914Z` 的 headline `walk_forward.oos_sharpe`（RESEARCH_LOG:13995-14030）；`governance_state.json` 里 tsmom 仍 `state: main`；脚本注释：「`advance` 没有排进任何 job，要人跑 `--commit`」。09-27 registry 改指 k=0.175 的 WEAK_PASS 报告（#163）之后，gate 任务有没有跑、读了什么：无新行，幂等键含 reasons，**判不出**（GAP-PR04）· 支持 C-PR07 · E1 · Medium（后半 UNKNOWN）· PUBLIC。

**E-PR15** · FILES+GIT · `ls reports/research | wc -l` = 268（tsmom-validation 84、overlay 32、flow-validation 18、mine-shortlist 16…）；`wc -l reports/research/trials.jsonl` = 22,205；`git ls-files reports/research | wc -l` = 337（ledger 与报告**入库**）；`reports/daily` 50、`weekly` 2、`forward-board` 22 个文件（gitignored）· 支持 §5 As-Is · E1 · High · PUBLIC。

**E-PR16** · CODE · 日期开关：`deploy/run_live.sh:47` `BRIDGE_UNTIL="2026-10-13"`（注释：#163 之后不再起作用）；`tests/live/test_the_construction_is_frozen_until_the_holdout_matures.py:72` `FREEZE_ENDS = "2026-09-27T07:54:00+00:00"`（已到期、测试自动变惰）；`beidou_governance/policy.py` `SINGLE_WINDOW_MINE_OPENING_ENDS = "2026-10-03T00:00:00+00:00"` 且 `STANDING_MINE_ROUNDS = 5 == max_mine_rounds_per_window`（#147 已解除 10-03 绊线）；`governance/reopen.yaml` 两条 `date_after`；`governance/window_changes.yaml` `earliest_window: 2026-10-03`；registry probe `review_after_days`。09-25 的 10-13 准备文档要靠人把这些从四类文件里拼出来（「三个开关同时翻转」「9 条测试红是下界」）· 支持 C-PR02（日期开关散落）· E1 · High · PUBLIC。

**E-PR17** · CODE+TEST · `tests/architecture/test_import_rules.py:1-60`；`grep -rn -E '^(from|import) beidou_' beidou_alpha | grep -v beidou_alpha` 零命中；39 个其他包的文件 import `beidou_alpha` · 当前 · 依赖方向规则成立：alpha → numpy/pandas/stdlib；governance → alpha/shared；live → alpha/data/exchange/governance/shared；cli → 任何 · 反证 F-A（结构上 alpha 已是最干净的一层）· E1 · High · PUBLIC。

**E-PR18** · CODE · `beidou_exchange/guard.py:16` `ALLOWED_HOSTS = frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})`；`beidou_alpha/backtest.py` `ImpactModel` docstring「The coefficient is an assumption, not a measurement, and it cannot be calibrated here … declared E5 for this venue … KILL-Q12 has held real capital out of scope since 2026-09-05」· 当前 · mainnet 在写请求离开进程前被拒；成本模型对真实资金规模没有校准 · 支持 C-PR02 · E1 · High · PUBLIC。

**E-PR19** · REPORT · `reports/weekly/2026-09-16.md:59-66`；`grep -n weekly deploy/*.sh deploy/*.plist` 无排程 · 2026-09-16 · 「Effort share (target 90% on alpha)」：`alpha_share 19.71%`，`lines {"alpha": 0, "infrastructure": 7693, "research": 1888}`；此后 12 天没有周报 · 支持 C-PR07、G4 机会成本 · E1 · High · INTERNAL。

**E-PR20** · DEPLOY · `deploy/*.plist` 七个（live / check / data / forward-board / governance-gate / paper-l3 / shadow），`~/Library/LaunchAgents` 同名七个；`deploy/run_live.sh`（读 `env.sh`、bridge、`exec beidou live run --armed`）；`com.beidou.live.plist` `KeepAlive {SuccessfulExit false}`、`ThrottleInterval 60`；`deploy/run_check.sh` 每小时 :10 跑 `live status --check` / `live verify --check` / `report daily --check` / `data pool lag --check`，失败推 webhook——**全部在同一台主机上**；`docs/RUNBOOK.md`「止盈止损从不在交易所挂单」「循环起不来，持仓就没有退出检查」· 支持 C-PR02 · E1 · High · PUBLIC。

**E-PR21** · DOC · `docs/analysis/2026-09-17-alpha-module-deep-analysis.md` §0、§2、§8、§14 · 2026-09-17 · 「`beidou_alpha` 全部 38 个文件 9,187 行逐行读过……无行为级 bug」；Final PIVOT；层 0 六项当日合入（#40/#41/#42/#44/#45/#48）；前向板建成（#58/#67）；冻结稿被独立审查推翻 P0×1 / P1×9，全部以撤方案或新证据关闭 · 支持 C-PR01 · E1 · High · PUBLIC。

**E-PR22** · DOC · `docs/analysis/2026-09-25-external-checklist-round-two.md:24-28`、`:55-58` · 2026-09-25 · 「没做的那一种：按清单的 7 步流水线重排整个包。北斗的包本来就按这条流水线排……重排要改 51k 行测试的 import、29 个 scratchpad 复现脚本与文档里的行号引用，却补不上任何一项清单能力」；#135 验收：81 个定义逐字相同、实盘快照上 81 个产物逐字节相同 · 支持 D-PR04、§9 验收形态 · E1 · High · PUBLIC。

**E-PR23** · DOC+CODE · PR #55（2026-09-17 合入）、`beidou_cli/research_cmd.py`（今天 153 行，`grep -c '^def '` = 0）、`tests/cli/test_nine_commands_nine_modules.py` · M6：3,457 → 142 行，35 个再导出地址钉住，「29 个复现脚本一个名字没断」· 支持 C-PR03（scratchpad 是一个隐藏的 API 面）· E1 · High · PUBLIC。

**E-PR24** · CODE · `tests/architecture/` 九个文件：`suite_duration.py`、`test_alerts_are_chinese.py`、`test_every_launchd_plist_is_valid_xml.py`、`test_every_module_is_reachable_from_an_entry_point.py`、`test_import_rules.py`、`test_secret_scanning_is_alive.py`、`test_source_budget.py`、`test_suite_duration.py`、`test_tests_never_touch_the_real_app_support.py` · 当前 · 架构约束已经是测试，不是文档 · 支持 §5 As-Is · E1 · High · PUBLIC。

**E-PR25** · CI · `.github/workflows/ci.yml:1-60` · 当前 · 四道门互不遮挡（`if: !cancelled()`）、按 `requirements.lock` 安装、Secrets 门 `fetch-depth: 0`；触发 `push: [main]` + `pull_request`；`CLAUDE.md`：branch protection 与 auto-merge 09-20 起开着，required check `verify` · 支持 §5 · E1 · High · PUBLIC。

**E-PR26** · CODE · `beidou_governance/policy.py` · `POLICY_VERSION = "0.3.6"`；`Policy` 十一条规则；`PROVENANCE` 标 E5 的四条：R1、R4、R5、R7；`record_digest_every_cycle True` · 支持 §5 约束（阈值在代码里、有 digest）· E1 · High · PUBLIC。

**E-PR27** · CODE · `beidou_live/construction.py:60` `CONSTRUCTION_PAYLOAD_VERSION = 11`、`:72` `CONSTRUCTION_ALIASES`；冻结测试 docstring「2026-09-03 起的 11.25 个 armed 日里 construction 换了 10 次，最长一段 4.7 天」· 构造指纹有版本与别名机制；任何重构若动到指纹字段集就要走这套 · 支持 RISK-PR01 · E1 · High · PUBLIC。

**E-PR28** · CODE · `beidou_alpha/signals/base.py`（`needs_metrics` / `needs_spot` / `uses_funding`）、`beidou_live/engine.py:2108-2170`（`metrics_refusal`、`spot_refusal`）、`beidou_live/composition.py:27-95` · 当前 · 研究读 T+1 metrics 归档，实盘 metrics 源只有 30 天 REST 窗口，需要 metrics 的信号在覆盖不够时被启动门拒绝；spot 需要 `Verification` · 支持 C-PR06（parity 距离存在且有机制，缺的是日常读数）· E1 · High · PUBLIC。

**E-PR29** · DOC · `docs/RUNBOOK.md`「改了 registry / profile 之后」「排障」「D-041 bridge 到期」· 重启纪律：整点后 5 分钟到下一整点前 10 分钟、先跑两个构造测试；「重启是幂等的」；「`governance` 17 个子命令」；shadow soak 的停靠逻辑 09-26 才修对 · 支持 C-PR02 · E1 · High · PUBLIC。

**E-PR30** · PLAN · `~/.claude/plans/nifty-gliding-petal.md`（README 引用的 V5 重建方案，2026-09-03）· 目标：「~15k 行」「alpha ≥60% 源码 / ≥90% 后续精力」「<30s 测试」「4 条 import 规则替代 2.7k 行架构测试」；C-006「重构本身再次膨胀成治理工程」是最大交付风险，当时 SUPPORTED，处置是「用里程碑与预算约束」· 支持 C-PR05 · E1 · High · PUBLIC（本地文件，非仓库）。

**E-PR31** · GIT · `git worktree list | wc -l` = 15；`git branch --merged origin/main` 去掉 main 与当前 = 32；分析时主 checkout `40cbe17c` 落后 `origin/main c926946d` 两个提交（#196，重启 #60）；开着的 PR #195 · 2026-09-28 · 并行会话的残留与 09-16 清理前同形（当时 4 个 worktree、70 个分支）· 支持 C-PR03 · E1 · High · PUBLIC。

**E-PR32** · DOC · `docs/analysis/analysis-calibration.md`（13 行）· 「误判最重的 Gate」11/13 是 G2；「缺的不是数据是读法」出现 6 次；「结构缺失」8 次（Phase 记录头 / MoSCoW / Checkpoint）；Success Definition 按方案反推 4 次 · 约束本文写法（§10.4 校准行）· E1 · High · PUBLIC。

**E-PR33** · CODE · `beidou_live/composition.py`（183 行，`load_panel` / `build_model` 组合根）、`beidou_live/engine.py:1-10`（cycle 阶段顺序）、`:132` `LiveConfig`、`:221` `LiveEngine`（68 个 def / 2,501 行）· 当前 · 组合发生在 live 与 cli，alpha 无 I/O——V5 的分层意图在代码里成立 · 支持 D-PR04 · E1 · High · PUBLIC。

**E-PR34** · 子代理只读盘点（live / cli+governance+data / tests）· 2026-09-28 · 见 §5.2 表内逐项引用；进入本文的数字均由主会话复核（审查者 R-15 又抽核七项，全部一致）。**[R 修订，KILL-16(b)]** 子代理原始报告只在会话记录里，不在盘上；可复核位置改为 §5.2 每格给出的命令 · E1 · High · PUBLIC。

**[R 修订] 审查后新增的证据（E-PR36–E-PR42，2026-09-28 12:00–13:00 本地）**

**E-PR36** · GIT（定稿前 `git fetch`）· 冻结稿写于 `origin/main c926946d`；定稿时为 `37e56ff7`，多出 #195（D-018 回撤门 1pp → 2.5pp，操作者签字；改的是 `BOOK_RULE`，`POLICY_VERSION` 仍 0.3.6）、#197（第一次夜间 `data metrics`）、#198（M-011 只对读 metrics 列的候选生效）、#199（LS 叶四个正形状「只在赌方向」，`reopen.yaml` 的 `ls-leaf` 改判 REFUTED；删掉一条 10-03 的 `date_after`）。随之移动：`beidou_governance` 4,287 → 4,357（顶 4,310 → 4,397，headroom 23 → 40）；`beidou_cli` 8,623 → 8,630（headroom 26）；ratchet 文件 4,383 → 4,395 行；scratchpad 82 → 84 个脚本、13,782 → 14,542 行（多出 612 行的零 ledger look 脚本 `ls_direction_look.py`）。本文分支已合入 `37e56ff7` · 支持 KILL-14 的处置 · E1 · High · PUBLIC。

**E-PR37** · ENV（只读目录列表与 `~/.zshrc` 的变量名，不读值）· `~/Library/Application Support/beidou/` 里**没有 `env.sh`**（只有日志、`alert-dedup.json`、lock、`proxy-probe.*`）；`~/.zshrc` 导出的 `BEIDOU_*` 变量名：`BINANCE_API_KEY`、`BINANCE_API_SECRET`、`ENV`、`ALERTS_WEBHOOK_URL`、`FRED_API_KEY`——**没有 `ALERTS_WEBHOOK_URL_2`**。六个 deploy 脚本的规则是「`env.sh` 存在就只 source 它，否则 eval `~/.zshrc` 的 `^export BEIDOU_` 行」（`deploy/run_live.sh:11-18`）。所以：凭据此刻来自 `~/.zshrc`，不是 CLAUDE.md 与冻结稿写的 `env.sh`；新建一个不含凭据的 `env.sh` 会让下一次重启以 78 退出（memory 早已记着这一条）；DL-L3 的第二告警通道在本机未配置（若 launchd 全局环境另有变量，此判读不成立——未读值，故写明）· 支持 KILL-01、KILL-05(c) 的处置；C-PR02 的「监控同宿」一半 · E1 · High · RESTRICTED（只记变量名与文件有无）。

**E-PR38** · GIT · 275 次触碰 ratchet 文件的非合并提交里 **267 次**改了 CEILING 的数值（`git show <h> -- tests/architecture/test_source_budget.py | grep -E '^[+-]\s+"beidou_[a-z]+": [0-9_]+,'`；审查者 R-03 读 265，差在解析口径）；非 alpha 源码行数：09-14 `a2215dd8` 25,240 → 09-21 `3a4bf6fa` 26,293（+150/天）→ 09-25 `5fbe4163` 31,724（+1,358/天）→ 09-28 32,906（+394/天）；审查者 R-17 按另一组日期读近 7 天约 945 行/天 · 支持 KILL-02 的处置：触碰 = 增长事件；+5% 预算（1,644 行）按这些速度撑 1–11 天 · E1 · High · PUBLIC。

**E-PR39** · DATA（`.beidou/live/cycles.jsonl`，只取 `phase`、`bar`、note）· SKIPPED 行 40 条，其中 27 条注明「this bar was already rebalanced」；13 条未注明里 12 条落在 09-06–09-14，1 条 09-23 是周期失败丢的 bar；ERROR 行 10 条，9 条含 503、1 条 VenueError；**09-15 起 ERROR 7 条**（09-15、09-16 ×2、09-21 ×2、09-23、09-26）。结论：09-15 起 0 根 bar 因重启时机丢失，7 根因失败周期丢失（与审查者 R-19/R-20 一致）· 支持 KILL-06 的处置；M-PR03 重写 · E1 · High · INTERNAL（只引计数）。

**E-PR40** · DATA+DOC · `governance/verdicts.jsonl` 未提交的第二行：2026-09-27T18:30:06Z `allow`「OOS 1.8329 vs 1.5739 at N=343」——在位者对当前引用报告过门，两次 `refuse` 是对 09-19 报告的历史（GAP-PR04 关闭）；`docs/analysis/2026-09-06-remediation-execution-plan.md:204,363-371`：D-P4「同机 + 第二通道 + 只告警；主机死后不自动平仓，残余风险由操作者 ACCEPTED（Q1=B、Q3=A，2026-09-06）」，DL-X2（远端租约 + 心跳推送）由 Q1=B 取消（`docs/RESEARCH_LOG.md:2764`）；09-05 系统质量分析 KILL-R2 列过朴素看门狗的失败形态 · 支持 KILL-05、KILL-14 的处置 · E1 · High · PUBLIC。

**E-PR41** · 审查者测量（`beidou-production-refactor-adversarial-review.md` R-12、R-18、R-03b、R-03c；命令见该文；**作者未复算 R-18/R-03b/R-03c**，复算了 R-12 的默认值）· R-12：`report weekly` 默认 `--commits 40`（`beidou_cli/live_cmd.py:1005`），09-16 那份的 40 个提交跨约 23 小时，所以 19.71% 不是一周的读数；同规则整周：32.5% / 36.2% / 21.5%（09-09、09-14、09-21 起三周）；R-18：V5 计划 M-003 的第三个量「改一个 signal 触碰的非 alpha 文件数」，09-10 起 5 个信号提交里 4 个为 0——支持 C-PR01 的「耦合」一半；R-03b：按改动行数 ratchet 文件只占近三周改动的 1.8%–3.2%；R-03c：30 天 420 次分包抬顶里 367 次（87%）不超过「max(40, 2%×顶)」· E1（独立第二人在同一仓库上的测量）· Medium（未复算的三项）· PUBLIC。

**E-PR42** · DOC · `grep 真实资金 CLAUDE.md` 只命中「真实资金账户的权益与持仓」不入库那一行——冻结稿引作「规则将带进真实资金，CLAUDE.md」的句子不在 CLAUDE.md（出自 09-17 分析的 Interaction 行）；memory `beidou-v5-alpha-first-rebuild.md:13`：demo first，「production/mainnet explicitly out of scope for now」；09-05 系统质量分析已写过 mainnet 的解除条件（附录 D 的 P0 全关、冲击模型下重推 `vol_target`、构造冻结后 30 天干净窗口、上线断言四项；KILL-R13 的处置是「只在操作者重开 mainnet 时启动」）· 支持 KILL-07 的处置：Q1 加「只 L-A」并设为 A · E1 · High · PUBLIC。

### 2.2 Claim Register 与 Axiom Trace

**C-PR01** · P0 · 「`beidou_alpha` 的代码结构不是 alpha 产出的瓶颈；重排或重写它不会提高可上线策略的吞吐，也不会提高系统的生产可用性」。
Axiom Trace：A1：承受损失的是操作者的时间——每一份「重排 alpha」的方案都要他读、裁、等，而 09-25 已经否决过一次同形方案（E-PR22）；A2：结论强度 SUPPORTED 不超过证据强度——三处 E1 独立指向同一方向：逐行审查无行为级 bug（E-PR21）、依赖方向零违规（E-PR17）、失败的候选全部死在统计门与数据上而不是代码上（E-PR11、E-PR12，`governance/reopen.yaml` 每条 condition 写的都是「新的信息不是新的网格」）；A3：相对于「重排」，更便宜的 80% 方案是「不动 alpha，补两件仪器」（§4.2）；A6：Falsifier = 任一候选有过门证据却因 alpha 代码形状（接口、性能、可达性）进不了 registry 或实盘——RESEARCH_LOG 与 reopen.yaml 无此记录；或 `AlphaModel.targets()` 单周期耗时逼近再平衡窗口（今天一次每小时调用，无读数显示接近）。
支持：E-PR04、E-PR10、E-PR11、E-PR12、E-PR17、E-PR21、E-PR22。反证：E-PR10「一书一策略」是能力限制——但它是被验证的构造选择（D-024），接通它是研究不是重构。~~状态 **SUPPORTED** · High~~ **[R 修订，KILL-03]** 状态 **PARTIAL** · Medium：三条证据测的是相邻命题（E-PR17 只测 alpha → 其它包的 import 方向；E-PR21 测行为级 bug；E-PR11/12 以候选已被测为条件），直接测吞吐的 GAP-PR06 未跑；「耦合」一半新增 E-PR41 R-18 支持；E-PR21 的引用漏了 09-17 的残项（下沉「部分达成」、M-AM06 未归零，`2026-09-17-alpha-module-deep-analysis.md:498`），E-PR22 的「已否决」改为「09-25 执行记录写明没做，依据是清单覆盖」· 决策影响：D-PR04 由作者决定改为 Q6。

**C-PR02** · P0 · 「系统在『demo 无人值守』层面已具备生产形态；在『真实资金』层面不是，差距不在 alpha：宿主单点且监控同宿、退出只在软件里而场地上无保护单、mainnet 被 guard 结构拒绝、冲击成本系数 E5、部署=重启且重启有价（2.46/天）」。
Axiom Trace：A1：损失是「宿主一停，持仓无人管，且没人知道」——今天的告警链与被告警的循环在同一台笔记本上（E-PR20）；A5：二阶复杂度——每次重启都可能少一根 bar 的退出检查，M-010 的窗口不因重启清零但因构造变更清零，所以「部署」与「构造变更」被绑在同一个动作上；A7：可交付与可验证——重启安全化、日期登记、dead-man 都能各自测（§9）；A6：Falsifier = 操作者已有一条不在本机的告警路径（Q3 答「已有」）→ 本命题的「无人知道」一半被推翻，WP-P1 撤；或场地上其实挂着保护单（`grep STOP_MARKET beidou_live beidou_exchange` 零命中，RUNBOOK 明写不挂）。
支持：E-PR06、E-PR07、E-PR16、E-PR18、E-PR20、E-PR29。~~反证：无。状态 **PARTIAL**（宿主外监控 UNKNOWN，GAP-PR01）~~ **[R 修订，KILL-05/06]** 反证：E-PR39——09-15 起 0 根 bar 因重启丢失，「重启有价」一项没有读数支持，丢 bar 的真实路径是失败周期（7 根）；E-PR40——「宿主外无人知」不是 UNKNOWN 而是**已知且 09-06 由操作者具名 ACCEPTED**（D-P4）。状态仍 **PARTIAL**，理由换了：成立的是「场地无保护单、guard 拒 mainnet、冲击系数 E5」三项（都在操作者尚未重开的 mainnet 范围内）与「宿主离线告警为 0」（新事实 E-PR37：第二通道未配置）；不成立的是「部署=重启有价」· Medium · 决策影响：WP-P1 → Q3（D-P4 重开问题）；WP-P2 出 Scope；新增 GAP-PR10（失败周期重试）与操作者动作「换代理节点」。

**C-PR03** · P1 · 「变更成本最大的可测项是 ratchet 文件：31.9% 的非合并提交触碰它、4,383 行、七包 headroom 0–40 行；其次是本地专属测试与 CI 的分歧、scratchpad 的隐藏 API 面、并行残留」。
Axiom Trace：A2：31.9% 与 4,383 是 `git log` 与 `wc -l` 的直接读数（E-PR03），不是印象；A3：对照「什么都不做」——该文件近 30 天改了 275 次，趋势没有自己变缓；A5：每次抬顶的注释是设计记录（仓库自己的裁定，memory 与 CLAUDE.md 都写着），所以搬迁不是删除，是换位置；A6：Falsifier = 搬迁后 30 天内 M-PR01 仍 > 20% → 税在 ceiling 政策本身而不在文字，下一杠杆是 headroom 政策（WP-C2）。
支持：E-PR03、E-PR08、E-PR23、E-PR31。反证：「ratchet 是并行会话的合并探测器」（`test_source_budget.py` 第七次抬顶注释）——这是它的收益，搬迁保留 CEILING 行的冲突面所以不丢。~~状态 **SUPPORTED** · High~~ **[R 修订，KILL-02]** 状态 **PARTIAL** · Medium：E-PR38 显示 267/275 次触碰改的是 CEILING 数值——「触碰」就是「包长过了顶」这一事件，不是独立于增长的税；按改动行数 ratchet 文件只占近三周的 1.8%–3.2%（E-PR41 R-03b）；「最大可测项」只在「触碰频率」一种单位下成立。真正没被裁定的是非 alpha 的增长率（E-PR38）。「触碰有独立成本」要新证据：该文件上的合并冲突次数、被它挡住的 PR 时长（GAP-PR08）· 决策影响：WP-C1/C2/D-PR03 退出执行授权，改为 Q2a/Q2b/Q5；WP-C3 出 Scope（KILL-15）。

**C-PR04** · P1 · 「四道门在操作者机器与 CI 上不一致：存在只在有归档的机器上跑、自 09-25 起为红而 CI 绿的测试」。
Axiom Trace：A2：n=1 的直接观察（E-PR08）；普遍性（有几条这样的测试）待 GAP-PR05；A7：修法可测——标记 + 夜间 job + 告警；A6：Falsifier = 那条测试在夹具重切后通过且归档专属测试只有这一条 → 一次性修复即可，不建机制。
支持：E-PR08。反证：无。状态 **PARTIAL** · Medium · 决策影响：WP-P4 是「机制」还是「一次修复」。

**C-PR05** · P1 · 「V5 计划自己的 KILL（C-006）已按它自己的度量成真——非 alpha 5.5×、live 7.7×、alpha 占比 24.9%、测试 60k 行——而这个缺口自 09-04 起是操作者『暂时承担』的开放决定，没有一份文件重定过价」。
Axiom Trace：A4：有限资源——alpha 投入占比 19.71%（E-PR19）说明工程项一直在挤 alpha，而缺口测试每天断言「已越界」却不推动任何决定；A5：系统性成本——ratchet 存在的理由就是这个缺口（`test_source_budget.py:1-13`），所以缺口不定价，ratchet 的税就没有上限；A6：Falsifier = 找到一份 09-04 之后的裁定把 `PLAN_BUDGET` 重定过价——子代理 D 在 RESEARCH_LOG 与 44 份分析里 grep 无（表 B 第 B5/B11 行），且测试仍在断言。
支持：E-PR02、E-PR03、E-PR19、E-PR30。反证：抬顶注释显示大多数增长买的是实盘事故暴露出的仪器（income 窗口、外来成交、杠杆重发、band 阻塞……），不是膨胀——所以本文的处置是**重定价**而不是**删代码**（D-PR03）。状态 **SUPPORTED** · High · 决策影响：D-PR03、Q5。

**C-PR06** · P1 · 「新数据族从『研究判定 PASS』到『能上线』之间还差一段没有日常仪器的 parity 距离：metrics/spot 的实盘覆盖 bars 对信号 lookback 没有读数；零 ledger 的 looks 没有机器记录，将来的 N 会漏算」。
Axiom Trace：A7：机制存在（`metrics_refusal`、`spot_refusal`、`data metrics` 夜间任务），缺的是把「今天覆盖到哪」印出来；A6：Falsifier = 日报已有「覆盖 bars ÷ lookback」一节（grep `report_data.py` 只有 M-011 的值 parity，不是覆盖对 lookback）；looks 已有记录文件（无）。A2：SUPPORTED 不超过证据——两处都是 grep 零命中，且 09-27 那节自己写「要算进 N」却没写进哪里。
支持：E-PR12、E-PR28。反证：无。状态 **SUPPORTED** · High · 决策影响：WP-A1/A2。

**C-PR07** · P2 · 「治理链上『有规则无执行者』的形状仍在：family gate 两次 refuse 无后果、`advance --commit` 无 job；周报（alpha 投入占比仪器）无 job、12 天未产出」。
Axiom Trace：A7：两处都是「文件在 deploy/ 或 CLI 里、不是 job」——`run_check.sh` 自己记过这类错（「a file in deploy/ is not a job」）；A6：Falsifier = 操作者裁定两者有意手动（Q4 答 B 且认可周报手动）→ 这是选择不是缺口，本文只加一行日报告警。
支持：E-PR14、E-PR19。反证：`advance` 手动可能是有意的（09-23 裁定只说「降回 probe」，没说谁按）。**[R 修订，KILL-14]** E-PR40：09-27T18:30Z 的 `allow` 行让两次 `refuse` 成为历史，「两次 refuse 无后果」这一半失效；剩下的是「`advance` 无 job」与「周报无 job、12 天未产出」两条观察。状态 **PARTIAL** · Medium · 决策影响：Q4 撤；WP-P5 只剩「周报排 job」一小项（Could）。

### 2.3 Assumption Register

记录式，字段：假设 · 类型 · 当前依据 · 影响 · 风险 · 最小验证 · 通过阈值 · 未通过动作 · Gap · 决策上限。

**A-PR00**（Pre-PR1 的登记）· 「重排 alpha 不提高产出」是本文的**结论**而非假设——已由 C-PR01 承接；这里只登记以满足 5.4 规则 2。影响 H · 风险 L（三处 E1 同向）· 无需实验。

**A-PR01**（Pre-PR2）· 「面向生产」= L-A（demo 无人值守做完）+ L-B（mainnet 小额校准的准入**设计**，不启用）· 价值 · 依据：CLAUDE.md「规则将带进真实资金」、KILL-Q12 把真实资金排除、操作者从未在仓库里写下生产的定义 · 影响 **H** · 风险 **M**（高×中）· 最小验证：Q1 · 通过阈值：操作者选 A · 未通过动作：Q1=B → 本文 PIVOT 为只做 Phase 0–1，mainnet 程序另开分析 · GAP-PR02 · 决策上限 Weak GO。

**A-PR02** · 操作者接受 ratchet 理由外移到 `docs/SOURCE_BUDGET_LOG.md`（改 CLAUDE.md 那条规则的字面）· 运营 · 依据：memory 记着「抬顶理由段是最完整的设计记录」，说明操作者在意的是**记录存在**而非**记录位置** · 影响 M · 风险 M · 验证 Q2 · 阈值：答 A · 未通过：WP-C1 缩为 WP-C2 · 无 Gap · 上限不变。

**A-PR03** · 第三方 dead-man 服务（只收空 ping）可接受 · 合规/运营 · 依据：仓库已用外部 webhook（Lark）推告警，出站已被接受；入站式「不响就报警」是它的镜像 · 影响 **H** · 风险 **M**（高×中）· 验证 Q3 · 阈值：答可接受或已有等价物 · 未通过：WP-P1 退到操作者自备第二设备，仓库只留脚本 · GAP-PR01 · 上限不变。

**A-PR04** · bar 安全重启不改构造 · 可行性 · 依据：`restart_reason` 与 MISSED 行已存在（#132），RUNBOOK 已写窗口与两条测试——WP-P2 只是把人做的顺序写成命令 · 影响 M · 风险 L · 验证：干跑一次 `beidou live restart --safe --dry-run`，前后 `construction` 与 `registry` 摘要相同 · 阈值：逐字相同 · 未通过：WP-P2 撤回 · 无 Gap。

**A-PR05** · 「补评估漏掉的 bar 的退出」在 D-012 语义内（仍是在闭合 bar 上评估，只是晚评估）· 可行性/价值 · 依据：`exit_step` 是纯函数，exit_states 持久化在 `state.json`，历史闭合 bar 在归档里 · 影响 M · 风险 **H**（改实盘行为）→ **高×高，配实验**：零 ledger 离线重放 60 次重启各自漏掉的 bar，对当时的 `exit_states` 数一数本该触发几次退出 · 阈值：≥1 次 → 值得预登记；0 次 → Could 并记录 · 未通过：只做 WP-P2 的第一半（安全窗口 + 两测试）· GAP-PR03 · 上限：这一半 Weak GO。

**A-PR06** · 归档专属测试不止 BNX 一条 · 可行性 · 依据：`tests/fixtures/bar_sanity/` 有多个切片、`tests/data` 读真实表（RUNBOOK：`test_the_membership_table_says_how_far_it_trails.py` 在真实表上重算）· 影响 M · 风险 M · 验证：子代理 C 的 skipif/归档引用清单 · 阈值：≥3 条 → WP-P4 做成 marker + 夜间 job；<3 → 一次性修 · GAP-PR05。

**A-PR07** · `research look` 的记录规则（looks 何时、按什么口径计入 N）需要治理裁定 · 治理 · 依据：R0 的口径由操作者签（policy.py PROVENANCE）· 影响 M · 风险 M · 验证：WP-A2 的预登记里写两种读法与默认（只记录、不计入 N，直到操作者裁） · 阈值：操作者签 · 未通过：命令只落地记录，不改任何 N。

高×高象限：A-PR01、A-PR03、A-PR05，三条各配了 Q 或实验并进 §2.5。**[R 修订，KILL-16(g)]** A-PR01 与 A-PR03 正文自标「高×中」，这里按汇总口径记高×高——以本行为准。审查后：A-PR01 默认改为「只 L-A」（E-PR42）；A-PR03 撤回，改为 Q3 的 D-P4 重开问题（E-PR40）；A-PR05 由 E-PR39 回答——09-15 起 0 根 bar 因重启丢失，「补评估漏 bar 的退出」没有价值，WP-P2 第二半出 Scope；A-PR07 的默认改为按 09-27 原文「计入 N」（KILL-09）。

### 2.4 Sizing Card

N/A：频率、规模、损失全部有直接读数（E-PR03、E-PR05、E-PR06、E-PR07），不需要估区间。

### 2.5 Gap Plan

记录式，字段：影响 · 不确定性 · 决策影响 · 最小方法 · 数据/来源 · Owner · 截止或条件 · 通过阈值 · 失败动作 · 未完成时决策上限。

**GAP-PR01** 宿主外有没有告警路径 · C-PR02、WP-P1 · 不确定性 H · 决策影响 H · 方法：Q3 · 来源：操作者 · Owner 操作者 · 条件：答复即闭 · 阈值：yes/no · 失败动作：默认「无」→ WP-P1 Must · 上限 Weak GO。

**GAP-PR02** 生产的定义 · A-PR01、D-PR02 · H · H · Q1 · 操作者 · 答复即闭 · 阈值：A/B · 失败动作：默认 A · 上限 Weak GO。

**GAP-PR03** 60 次重启各漏了哪些 bar，在当时的 `exit_states` 下本该触发几次退出 · A-PR05、WP-P2 第二半 · M · M · 方法：零 ledger 离线脚本——从 `cycles.jsonl` 的 `restart` / `MISSED` 行取漏掉的 bar，从归档取闭合价，用 `beidou_alpha.overlays.exits.exit_step` 逐个 symbol-bar 重放；不写 `.beidou/`、不写 ledger · 来源：`.beidou/live/{cycles.jsonl,state.json}`、`.beidou/data/klines` · Owner：下一个执行会话 · 截止：Phase 2 开工前 · 阈值：≥1 → 预登记后实现；0 → Could · 失败动作：只做第一半 · 上限：该半 Weak GO。

**GAP-PR04** family gate 任务 09-26 / 09-27 有没有跑、对 k=0.175 的报告读到什么 · C-PR07、Q4 · M · M · 方法：`beidou governance gate`（只读读数，**不带写副作用的那条路径**——按 `run_governance_gate.sh` 注释，`gate` 会追加 verdict 行，所以要先看 `governance verdicts` 与 launchd 日志 `~/Library/Application Support/beidou/`）· Owner 操作者或下一会话 · 截止：Q4 之前 · 阈值：当前引用报告 PASS → 两次 refuse 是历史；FAIL → 在位者过不了自己的门，Q4 变紧急 · 上限不变。

**GAP-PR05** 归档专属或日期专属测试有几条 · C-PR04、A-PR06 · L · M · 方法：子代理 C 的 grep 清单 + 主会话复核 · 截止：本文 §5.2 · 阈值：见 A-PR06。

**GAP-PR06** 一次 `research validate` 的每格耗时与瓶颈（面板构建 / 特征 / 回测 / 报告）· WP-A4 · M · L · 方法：给一次 `research backtest`（零 ledger）加 `cProfile`，读前十 · Owner 下一会话 · 阈值：面板/特征 > 50% → WP-A4 值得做；否则 Won't · 上限不变。

**GAP-PR07** `engine.py` 2,501 行有没有自然边界（不按行数拆）· WP-C5 · M · L · 方法：子代理 A 的阶段-函数表 + 主会话读 `run_cycle` · 截止：本文 §5.2 · 阈值：存在 ≥2 个只依赖 `state`+`store` 的阶段簇 → 记为 Could 并写边界；否则 Won't。**已答**：子代理 A 的阶段表显示每个阶段都读 `self.state`/`self.store`/`self.config` 三者之一以上，没有只依赖两者的簇 → Won't。

**[R 修订] 审查后关闭的缺口**：GAP-PR01（E-PR40：没有宿主外路径，且 09-06 已 ACCEPTED——不是 UNKNOWN 而是已知）；GAP-PR03（E-PR39：09-15 起 0 根 bar 因重启丢失，重放没有对象）；GAP-PR04（E-PR40：09-27 `allow` 行）；GAP-PR05（子代理 C：7 条 `.beidou/` 读者）。

**[R 修订] 新增缺口**

**GAP-PR08** ratchet 文件上的「触碰」有没有独立于增长的成本 · C-PR03、Q2a/Q2b · 不确定性 M · 决策影响 M · 方法：`git log --merges` 里的冲突提交与 PR 评论里 rebase 该文件的次数；`gh pr list` 里从开到合的时长与该文件是否在 diff 里的相关 · Owner：下一会话 · 阈值：冲突或延时可归因到该文件 ≥ 每周 1 次 → Q2a 值得；否则 Q2a 只是整洁 · 未完成时上限：Q2a 不执行。

**GAP-PR09** BNX 2023-02 夹具（48 行、注明 518 根停牌缺口）与归档（552 行，09-25 `data repair` 补进 504 根）哪一边对 · E-PR08、WP-P4 · M · M · 方法：`beidou data status` 看 BNXUSDT 的缺口记录与 `confirmed_gaps.json`；查币安公告 2023-02 BNX 是否重新计价 · Owner：**操作者**（HC-8）· 阈值：归档对 → 重切夹具并改 `gap_bars` 断言；夹具对 → 归档回滚那段 · 未完成时：不改夹具、不改归档（EC-PR3）。

**GAP-PR10** 失败周期在再平衡窗口内重试能救回几根 bar · C-PR02、M-PR03 · M · M · 方法：读 `cycles.jsonl` 的 7 条 ERROR 行（09-15 起）的时刻与错误类型，对照 `proxy-probe.jsonl`（09-22 已停）里 503 簇的持续时长分布，估「若在 X 秒后重试一次」能成功的比例；零 ledger、只读 · Owner：下一会话 · 阈值：≥ 4/7 可救 → 写预登记（改实盘行为）；否则记录 · 与操作者动作并行：换代理节点是 09-22 判读的唯一便宜解，不由 agent 做。

### Gate Review：G2

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G2 | PASS | E-PR01–34；C-PR01–07 | C-PR02 一半 UNKNOWN（GAP-PR01）；C-PR04 普遍性（GAP-PR05） | Weak GO（H3） | 操作者 / 下一会话 | Q1、Q3；§5.2 填 GAP-PR05/07 |

---

## 3. Phase 3 · Problem Research

**Prerequisites**：G0 PASS、G2 PASS、C-PR01–07。**Produces**：Problem Statement、5W2H、Causal Chain、F-A–F-D 与 D-PR00、M-PR01–06、Stakeholder、Scenario、Edge Cases。**Downstream**：§4 相对价值的对照组、§6 Option 的成立条件、§9 契约的验收对象。

### 3.1 Problem Statement

对于**单一操作者**，当他在**每天读日报、裁定 agent 会话提交的 PR、并准备把这套规则带进真实资金**时，由于**系统的生产边界从未被写成可读数的定义**（宿主单点、监控同宿、退出只在软件里、mainnet 被结构拒绝、部署等于重启）而**每次改动都要付一笔没有上限的税**（三成提交触碰 ratchet 文件、本地专属测试与 CI 分歧、复现脚本占着隐藏 API、并行残留积累），会遇到**「系统看起来在跑，但说不出离生产还差什么、也说不出下一次改动为什么这么贵」**，导致他**反复要求「重构」而得到定向批次或 PIVOT**（09-17 ×3、09-23、09-25）。如果不解决，V5 计划自己写下的 KILL（C-006）就会一直以「暂时承担」的形式存在，alpha 投入占比会继续被工程项压在 20% 左右，而第一次宿主长时间离线时，没有任何东西会通知操作者他的持仓无人看管。

**[R 修订，KILL-02/05/06]** 上面这段有三处按冻结稿的读法写：「部署等于重启」——09-15 起没有一根 bar 因重启丢失，丢的 7 根全部来自失败周期（E-PR39）；「每次改动都要付一笔没有上限的税」——三成提交触碰 ratchet 是因为包每三次提交就长过一次顶，税就是增长本身（E-PR38）；「第一次宿主长时间离线时没有任何东西会通知操作者」——这是真的，但操作者 09-06 已具名接受了这条残余风险（E-PR40），新的是「第二通道未配置」（E-PR37）。修订后的一句话：**对于单一操作者，当他每天读日报、裁 PR 时，由于「生产」从未被写成读数、非 alpha 的增长率从未被裁定、而传输层每两天丢一根 bar 的退出检查没人量过，会遇到「系统在跑但说不出离生产还差什么」，导致他反复要求「重构」而得到定向批次或 PIVOT。**

### 3.2 5W2H

| 维度 | 结论 | 状态 | IDs |
| --- | --- | --- | --- |
| What | 两段可量的距离：生产边界（谁在宿主外看着、重启的代价、日期开关、mainnet 准入）与变更成本（ratchet、本地专属测试、scratchpad API、并行残留）；alpha 模块不在其中 | 已确认 | C-PR01–05 |
| Who | 操作者是用户、付款者、审批者、风险承担者；agent 会话是操作者；受损方见 3.7 | 已确认 | P1.1 |
| Why now | 冻结 09-27 已结束、k=0.175 切换已载入（重启 #59/#60），下一个大动作要么是 alpha 侧的新预登记要么是工程侧；#163 之后 tsmom 的证据清了门，bridge 与 exemption 都不再起作用——这是自 09-03 以来第一个「没有日期在催」的窗口 | 已确认 | E-PR16、E-PR29 |
| Where | `deploy/`、`tests/architecture/`、`tests/` 里读 `.beidou/` 的 7 条、`scratchpad/`、`beidou_live/engine.py` 的启动路径、`beidou_exchange/guard.py`、日报 | 已确认 | E-PR08、E-PR18、E-PR20、E-PR34 |
| When | 每小时一次循环；每天 2.46 次重启；每 3 次提交 1 次抬顶；每天 1 次 gate 任务写 verdicts；每周应出一次周报（12 天没出） | 已确认 | E-PR03、E-PR06、E-PR14、E-PR19 |
| How（现状与替代） | 现状：人工纪律（RUNBOOK 的重启窗口、两条测试、快进主 checkout）+ 抬顶注释 + worktree；替代见 §6.1 | 已确认 | E-PR29 |
| How much | 定价单位：行数（按包，含 ratchet 影响）、执行会话天数、是否改构造、是否要重启、ledger（全部为 0）；上限：Phase 0–1 合计 ≤ 3 个会话日、净增行 ≤ +600（估）、构造与 ledger 不动 | 已确认 | §6.1 |

### 3.3 Causal Chain

| 层级 | 内容 | Evidence IDs | 可干预性 |
| --- | --- | --- | --- |
| 表层现象 | 操作者第五次要「重构」；agent 每 3 次提交抬一次顶；本地一条测试红了 3 天没人知道；周报 12 天没出；两次 gate refuse 无人处理 | E-PR03、E-PR08、E-PR14、E-PR19、E-PR21 | — |
| 直接原因 | 没有「生产」的定义，所以每一份方案都只能回答操作者手里那一个词（alpha、重构、优化）；ratchet 在零 headroom 处工作；归档专属测试没有自己的执行位置；`advance` 与 `report weekly` 有命令没有 job | E-PR02、E-PR03、E-PR08、E-PR19 | 是 |
| 深层原因 | V5 的预算（M-003）在 09-04 被「暂时承担」后再没定价，ratchet 于是从「守预算」变成「守昨天」；「文件在 deploy/ 里 ≠ 是个 job」这个错在仓库里重复出现（forward-board、governance-gate、周报）；「合入不等于生效」把每次部署绑到一次重启 | E-PR02、E-PR14、E-PR20、E-PR29 | 是 |
| 系统性原因 | 单人 + 多个并行 agent 会话，产出 41 次提交/天，而每一处「要人记住」的规则（日期、重启窗口、抬顶、清分支）都靠会话读到 CLAUDE.md 与 RUNBOOK 才执行；仓库自己的诊断在 09-08 就写过：「A "just this once" with no executing check is a promise」 | E-PR05、E-PR16、E-PR31、E-PR32 | 部分 |
| 可干预杠杆 | ① 把「生产」写成三层定义与六个读数（D-PR02、M-PR01–06）；② 把「要人记住」的四件事变成命令或 job（重启窗口→`live restart --safe`，日期→`governance calendar`，归档测试→夜间 job，宿主外→dead-man）；③ 给 ratchet 一个显式的 headroom 政策并把 4,300 行记录搬到记录该在的地方；④ 关闭 09-04 的开放决定（D-PR03） | — | ~~本方案~~ **[R 修订]** ②里「重启窗口」与「宿主外」两项撤（E-PR39/40）；③④改为交操作者的 Q2/Q5；新增的可干预杠杆是**传输层**：换代理节点（09-22 判读，操作者动作）+ 量失败周期窗口内重试的收益（GAP-PR10） |

**[R 修订]** 因果链「直接原因」一层里「部署等于重启」按 E-PR39 改为「传输层每两天丢一根 bar 的退出检查，且没有任何工作项在量它」；「深层原因」里「ratchet 从守预算变成守昨天」改为「非 alpha 的增长率没有被裁定，ratchet 只能逐次记录它」（E-PR38）。

### 3.4 Problem Reframing

| 框定 ID | 一句话框定 | 若为真，方案方向 | 能区分它与其他框定的证据 | 当前支持度 |
| --- | --- | --- | --- | --- |
| F-A | 这是一个 **alpha 代码结构**问题：`beidou_alpha` 的模块划分、接口或体量阻碍了策略产出与上线 | 重排 / 重写 `beidou_alpha` | 有没有一条候选是因为代码形状而不是统计门进不了实盘；alpha 的依赖方向有没有违规；逐行审查有没有行为级 bug | **被排除**：三条证据全部相反（E-PR11、E-PR12、E-PR17、E-PR21）。10 个信号族里 8 个死在门与数据，2 个在跑；alpha 是七包里唯一零 `beidou_*` 依赖的一层；「大」的三成是设计记录（E-PR04） |
| F-B | 这是一个 **alpha 功效与数据 parity** 问题：当前样本与诚实的门下功效不足，新数据族到实盘之间还差一段没有仪器的距离 | 前向板攒年、新信息族、parity 仪器；**没有一项是重构** | 功效表（E-PR11）；09-27 look 的三条判据（E-PR12）；`metrics_refusal` 的机制与缺的日常读数（E-PR28） | **成立**，但它不是本文的主问题——它由 F4 盘点、前向板与预登记流程承接；本文只补两件仪器（WP-A1/A2） |
| F-C | 这是一个 **生产边界**问题：系统在 demo 无人值守上已成形，但「宿主一停谁知道、重启为什么有价、哪天哪个开关翻、mainnet 差什么」没有一个有读数 | dead-man、bar 安全重启、日期登记、mainnet 准入设计 | 七个 job 同宿一台笔记本（E-PR20）；60 次重启与昨日 2,154s 迟到（E-PR06/07）；日期开关散在四类文件（E-PR16）；`guard.py:16`（E-PR18） | **成立**，选定 |
| F-D | 这是一个 **变更成本**问题：每次改动要付的税没有上限，而税的来源是 09-04 那个没定价的缺口 | ratchet 记录搬迁与 headroom 政策、本地专属测试归位、scratchpad 地址契约收口、预算重定价 | 31.9% 触碰率（E-PR03）；BNX 红 3 天（E-PR08）；42 个脚本 import CLI 私有名（E-PR34）；缺口测试仍断言越界（E-PR02） | **成立**，选定 |

**D-PR00**：选 F-C + F-D 为方案主体，F-B 承认为真、只补仪器不重构，F-A 排除。~~放弃 F-A 的理由是证据而非推理：三处 E1 独立同向，且 09-25 已在同一问题上否决过一次同形方案。~~ **[R 修订，KILL-03]** 三条证据测的是相邻命题，F-A 是**由推理排除**的，改记「未判」：耦合一半有 E-PR41 R-18 支持，吞吐一半 UNKNOWN（GAP-PR06）。09-25 那份是「执行记录，不是裁定」，它没做重排的依据是清单覆盖，不是生产或吞吐。F-D 也改写：不是「变更成本 = ratchet 税」，而是「非 alpha 增长率没有裁定」（E-PR38）；F-C 的证据列去掉「重启有价」（E-PR39）。重开条件不变：任一候选**有过门证据**却因 `beidou_alpha` 的接口或性能进不了 registry / 实盘。F-A 的判定交 Q6。

### 3.5 Success Definition

写在任何 Option 之前。基线全部取自 §2.1，阈值与窗口在 §10.1 补全。

| Metric ID | 类型 | 指标 | 基线来源 | 关联 Claim | 为什么它衡量的是问题而非方案 |
| --- | --- | --- | --- | --- | --- |
| M-PR01 | 领先 | 近 30 天非合并提交里触碰 `tests/architecture/test_source_budget.py` 的比例 | E-PR03：275/861 = **31.9%** | C-PR03 | 它量的是「一次普通改动要付的税」，与用哪种方式降税无关；ratchet 政策不变它就不动 |
| M-PR02 | 领先 | 只在操作者机器上执行、且状态与 CI 不一致的测试数（今天：读 `.beidou/` 的 7 条里红 1 条） | E-PR08、E-PR34（子代理 C 第 3 节） | C-PR04 | 它量的是「四道门在两台机器上说的是不是同一句话」，任何修法都得让它归零 |
| M-PR03 | 滞后 | 实盘重启数/天；重启导致的周期迟到秒数（日报 Restart cost 的 `worst_restart_late_seconds`） | E-PR06：**2.46/天**；E-PR07：昨日最差 **2,154s** | C-PR02 | 它量的是「部署与运行的耦合」——不论是靠命令、靠纪律还是靠热加载，耦合松了它才降 |
| M-PR04 | 护栏 | 每个重构 PR 前后 `construction_fingerprint`、`registry_digest`、`policy_digest` 逐字相同；`beidou live verify --check` 差异为 0；#135 协议下产物逐字节相同 | 今天：construction `2ee491c13971`、registry `7f8adb754962`、policy 0.3.6 | C-PR01 | 它保证「重构」这个词在本仓库的唯一合法含义：不动任何一个被交易的数 |
| M-PR05 | 护栏 | `reports/research/trials.jsonl` 的 sha256 与行数（22,205）不变；`Policy` 无字段变化 | E-PR15、E-PR26 | C-PR01 | 工程项不得花 ledger、不得动门 |
| M-PR06 | 滞后 | 每个候选数据族的「实盘覆盖 bars ÷ 最长信号 lookback」在日报里有读数；零 ledger looks 有机器记录 | 今天：**无**（E-PR12、E-PR28） | C-PR06 | 它量的是「研究到实盘的 parity 距离」，与用哪条命令补都无关 |

~~M-PR01–03、M-PR06 都是在看到任何方案之前就能写下的量~~ **[R 修订，KILL-02/06/17]** 上表是冻结稿版本，保留以便对照。审查指出：M-PR01 定义在方案要改的文件上，且它数的是「包长过了顶」的次数，任一放宽 headroom 的方案都能直接压低它；M-PR03 读反——2,154 s 是一次安全窗口内、bar 已再平衡的重启；F-C 的标题损失「宿主离线没人知道」没有指标；18 个工作项里 12 个没有对应指标。修订版：

| Metric ID | 类型 | 指标 | 基线来源 | 关联 Claim | 为什么它衡量的是问题而非方案 |
| --- | --- | --- | --- | --- | --- |
| M-PR01 | 领先 | **非 alpha 源码行数的日增长率**（`git ls-tree` 逐周计），与**整周口径**的 alpha 投入占比（`report weekly` 按整周提交计） | E-PR38：09-14→21 +150/天、09-21→25 +1,358/天、09-25→28 +394/天；E-PR41：32.5% / 36.2% / 21.5% | C-PR05 | 它量的是「非 alpha 在长多快」这个没被裁定的量，任何 ratchet 旋钮都改不了它；裁定（Q5）落地后它要么被接受为常态，要么被一个上限约束 |
| M-PR02 | 领先 | 只在操作者机器上执行、且状态与 CI 不一致的测试数 | E-PR08、子代理 C：7 条 `.beidou/` 读者里红 1 | C-PR04 | 与修法无关；任何修法都得让它归零 |
| M-PR03 | 滞后 | **每月丢掉的 bar 数，按来源分**：失败周期（`cycles.jsonl` ERROR 行）/ 重启时机（未注明「already rebalanced」的 SKIPPED 行） | E-PR39：09-15 起 7 / 0 | C-PR02 | 它量的是「退出检查缺席」这个损失本身，不受安全窗口按构造抬高；换代理节点、窗口内重试、任何别的办法都在同一个数上被读 |
| M-PR04 | 护栏 | 每个 PR 前后 `construction_fingerprint`、`registry_digest`、`policy_digest` 逐字相同；`live verify --check` 0 差异；#135 协议下产物逐字节相同 | construction `2ee491c13971`、registry `7f8adb754962`、policy 0.3.6 | C-PR01 | 「重构」在本仓库的唯一合法含义 |
| M-PR05 | 护栏 | `trials.jsonl` 行数与 sha256 不变；`Policy` 字段不变 | 22,205 行 | C-PR01 | 工程项不得花 ledger、不得动门 |
| M-PR06 | 滞后 | 每个候选数据族的「实盘覆盖 bars ÷ 最长信号 lookback」有日常读数 | 今天：无（E-PR12、E-PR28） | C-PR06 | 量的是研究到实盘的 parity 距离，与用哪条命令补无关 |
| M-PR07 | 滞后 | **宿主离线到操作者知道的最长延迟**（演练测得） | 今天：无上限（同机巡检随宿主一起停，E-PR20/37） | C-PR02 | F-C 的标题损失；D-P4 维持 ACCEPTED 时它保持无上限并被记录，重开时它是验收量 |

工作项到指标的对照（KILL-17）：WP-P4 → M-PR02；WP-P3 → 无独立指标，验收是「翻转前 7 天日报有预告」（AC-PR3b）；WP-C6/C7/C8/C9 → M-PR04（护栏）+ 各自测试存在；WP-A1 → M-PR06；Q3 → M-PR07；Q5 → M-PR01；操作者换代理节点与 GAP-PR10 → M-PR03。M-PR04/05 是这个仓库对「重构」一词的既有定义（#55、#135 的验收形态）。

### 3.6 JTBD

当**我每天早上读日报**，我想要**一眼看到系统离生产还差哪几个读数、以及昨天的改动有没有动到任何被交易的数**，以便**把时间花在裁定上而不是在追问上**。

当**一个 agent 会话要改 `beidou_live`**，我想要**它付的税是一个写明的数（headroom、地址契约、归档测试的位置）而不是一段要读 4,000 行才知道的惯例**，以便**并行会话不在同一个文件上撞车**。

### 3.7 Stakeholder Map

| 角色 | 身份 | 诉求 | 担忧 | 成功标准 | 反对点 |
| --- | --- | --- | --- | --- | --- |
| 操作者 | 用户 / 付款 / 审批 / 风险承担 | 系统能带进真实资金；改动便宜；少读长文 | 又一份「等」；改动动了被交易的数 | M-PR01–06 | 五个裁定要他答 |
| agent 执行会话 | 操作 | 规则可执行而非可记住 | ratchet 冲突、地址断裂 | 每次 PR 的税可预算 | — |
| **受损方：写 scratchpad 复现脚本的过去会话与它们的读者** | 受损方 | 脚本随时能跑 | 归档后要先 `git worktree add <commit>` 才能跑 | 归档 README 钉 commit，命令一行 | 损失：即时可运行性；补偿：钉 commit 的复现配方（memory 已记这条做法） |
| **受损方：把「理由紧挨常量」当阅读入口的人** | 受损方 | 打开 ratchet 文件就读到全部历史 | 记录搬到 `docs/` 后要跳一次 | 同编号、双向引用、测试守住引用存在 | 损失：一次跳转；补偿：4,300 行记录第一次能被 `grep` 与按日期读 |
| **受损方：CI 的「全绿」** | 受损方 | — | 归档专属测试改成夜间 job 后，CI 不再假装覆盖它们 | 日报有它们的状态 | 这是把已经存在的分歧写出来，不是新增分歧 |

### 3.8 Scenario

| Scenario ID | 角色 | 触发 | 前置 | 目标 | 当前路径 | 失败/边界 | 期望结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S-PR1 | 操作者 | 笔记本合盖 / 断网 / 进程被 -9 | 循环持仓中 | 两小时内知道循环停了 | 每小时 `run_check.sh` 在**同一台机器**上跑，机器停它也停；Lark webhook 只在有人 POST 时才响 | 机器不在，链上没有一个环节能发声 | 宿主外的 dead-man 在 2 个整点内推送「循环失联」 |
| S-PR2 | 执行会话 | 一个改 `beidou_live` 的 PR 合入，要生效 | 循环在跑 | 在不少一根 bar 退出检查的前提下重启 | 读 RUNBOOK：算窗口、手跑两条测试、`launchctl kickstart -k`、写 RESEARCH_LOG | 忘了窗口 / 忘了测试 / 跨了 bar 收盘 | 一条命令等窗口、跑测试、重启、记录 |
| S-PR3 | 执行会话 | `data repair` 改了归档 | 本机有归档 | 本地四道门与 CI 说同一句话 | 归档专属测试在本机红、CI 绿、worktree 跳过——没人看到 | 红 3 天无人知道（E-PR08） | 夜间 job 跑归档专属测试并告警；CI 明写不覆盖它们 |
| S-PR4 | 操作者 | 某个日期开关要翻 | — | 提前 7 天知道翻什么 | 09-25 靠一份 63 KB 的分析把四类文件里的日期拼出来 | 漏一处（当时 bash 3.2 那条就是这样被发现的） | `governance calendar` 列出未来 30 天的每一次翻转与读者 |
| S-PR5 | 操作者 | 想知道能不能上 mainnet 小额 | demo 稳定 | 一张准入清单，每项有读数 | 没有；`guard.py` 直接拒绝 | 准入清单不存在，于是「上不上」只能是感觉 | WP-P6 的清单与 profile 骨架（不启用） |

### 3.9 Journey（S-PR2，重启一次实盘循环）

| Step | 用户行为 | 系统行为 | 数据/权限 | 摩擦 | 异常 | IDs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 会话决定「要重启」 | — | — | 要先判断这次重启改不改构造 | 判错 → M-010 窗口清零 | E-PR27 |
| 2 | 手算安全窗口（整点后 5 分钟到下一整点前 10 分钟） | — | 主机时钟 | 人算 | 跨了 bar 收盘 → 那根 bar 没有退出检查 | E-PR29 |
| 3 | 手跑两条构造测试 | 测试读 shipped pair | — | 忘跑 | — | E-PR29 |
| 4 | `launchctl kickstart -k gui/$(id -u)/com.beidou.live` | 进程 -9 退出、launchd 拉起、`run_live.sh` 读 env、bridge、`beidou live run --armed` | 凭据（RESTRICTED，env.sh） | — | 启动门拒绝（证据 / 数据集）→ 循环不在，持仓无退出检查 | E-PR20、E-PR29 |
| 5 | 把 PID、`restarted_at`、`restarts`、测试结果记进 RESEARCH_LOG | `state.json.restarts += 1`；MISSED 行（#132） | — | 手写 | 归因错（并行会话下「我改完循环就重启了」不构成「是我重启的」） | E-PR06、memory |

WP-P2 把 2–5 折成一条命令，第 1 步仍是人的判断。

### 3.10 Edge Cases

| Edge ID | 场景 | 触发条件 | 预期行为 | 风险 | Test/Acceptance 影响 |
| --- | --- | --- | --- | --- | --- |
| EC-PR1 | 安全重启命令在等窗口时 bar 收盘临近 | 当前时刻在整点前 10 分钟内 | 等到下一整点后 5 分钟再重启，打印等待时长 | 会话超时 | T-PR2a |
| EC-PR2 | dead-man ping 因代理 503 失败 | 出站被 1082 代理间歇拒绝（memory） | 一次失败不告警；连续两个整点无 ping 才告警——与 `alerts.py` 的 dedup 窗口同形 | 假阳性 | T-PR1b |
| EC-PR3 | 归档专属测试在夜间 job 里红 | `data repair` 改了夹具对应的切片 | 告警正文写「夹具与归档不一致：切片行数 48 → 552」，不改夹具、不改归档 | 有人改夹具让它绿 | AC-PR4b |
| EC-PR4 | ratchet 记录搬迁与并行会话同日抬顶 | 两个 PR 都改 CEILING | 合并冲突只发生在 CEILING 那一行（保留的合并探测器性质） | 搬迁 PR 被反复 rebase | T-PR-C1c |
| EC-PR5 | `governance calendar` 遇到一处新写法的日期 | 有人在新文件里写 `date_after` 或 `UNTIL` | 测试要求每个日期常量在登记表里；漏登记就红 | 登记表变成又一处要记住的地方 | T-PR3b |
| EC-PR6 | mainnet profile 骨架被误当成可用 | 有人传 `--profile config/live.mainnet.yaml --armed` | `guard.py` 仍拒绝 mainnet host，除非签字文件 + 显式旗标同时存在；本 Phase 不实现放行 | 「设计」被读成「启用」 | T-PR6a |

### Gate Review：G1

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G1 | PASS | C-PR01–07；D-PR00；M-PR01–06 | 移除「重构」后问题独立成立；框定由证据而非推理裁决；Success 在 Option 之前 | Weak GO（H3 沿 G2） | — | §4 |

---

## 4. Phase 4 · Strategic, Relative Value & Economic Fit

**Prerequisites**：G1 PASS、C-PR01–07。**Produces**：Strategic Fit、Relative Value、Business Case、Cost of Delay。**Downstream**：§6 Option 的定价列、§8 MoSCoW。

### 4.1 Strategic Fit：~~High~~ **[R 修订] Medium**

| 维度 | 结论 | IDs |
| --- | --- | --- |
| 战略方向 | 操作者的目标（memory）：持续盈利、24h 无人值守、少操作、alpha ≥ 90% 投入、~~最终带进真实资金~~ **[R 修订，KILL-07]** 同一段 memory 写着「demo is a test environment with real capital deferred to later; production/mainnet explicitly out of scope for now」，V5 计划的诉求里还有「弱化风控」——冻结稿只引了前半。卫生项落在「无人值守」「少操作」两条上，不移动 alpha，也不涉及 mainnet；所以是 Medium，与 09-17 alpha 模块分析给「研究工具卫生」的评级一致 | E-PR19、E-PR30、E-PR42 |
| 核心指标 | M-PR01–06 | §3.5 |
| 当前阶段 | 冻结刚结束、k=0.175 已载入、bridge 与 exemption 都失效——工程侧第一次没有日期在催 | E-PR16 |
| 主线/旁支 | 主线是 alpha；本文是让主线的每次改动更便宜、让系统在主线之外不出事。旁支，但它决定主线的单价 | — |
| 机会成本 | Phase 0–1 约 3 个会话日；同期 alpha 侧可做的事（前向板读数、新预登记）不受影响——两边不碰同一批文件 | §6.1 |
| 能力匹配 | 全部改动在仓库既有形态内（命令、job、测试、文档），无新依赖、无新服务（dead-man 除外，见 Q3） | — |

### 4.2 Relative Value Proof：Phase 0–1 Strong；Phase 2–3 Adequate；「重排 alpha」Unproven

| 替代路径 | 当前可用性 | 核心结果比例 | 一次性/持续成本 | 风险 | 切换/学习/信任成本 | 本方案真实增量 | IDs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 现有绕行：RUNBOOK 纪律 + 抬顶注释 + worktree + 定向批次 | 在用 | 约 60%（系统确实在跑） | 0 一次性；持续：每天 2.46 次重启的人工判断、每 3 次提交 1 次抬顶、每次「重构」请求一份长文 | 宿主离线无人知；本地红没人看 | 0 | dead-man、命令化重启、日期表、归档测试归位：四件「要人记住」的事变成机器的 | E-PR03、E-PR08、E-PR20 |
| 流程/人工：把这些写进 CLAUDE.md 再加几条 | 可用 | 约 30% | 极低 | 「A "just this once" with no executing check is a promise」——仓库自己记过三次同形失败（forward-board、governance-gate、周报） | 0 | 命令与 job 对散文 | E-PR14、E-PR19 |
| 第三方：现成的 uptime / dead-man 服务；现成的 trading 框架 | dead-man 可用；框架不可用（横截面组合层不匹配，V5 方案 Option C 已否决） | dead-man 100%（就是它该做的事）；框架 0% | dead-man 免费档；框架数周迁移 | 数据出站：一个空 ping | 一个账号 | WP-P1 直接采用第三方；框架 Won't | E-PR30 |
| 自建/重排：按 7 步流水线重排包；重写 alpha；拆 engine.py | 可做 | 对 M-PR01–06 增量为 0（重排不改任何一个读数） | 51k 行测试 import、29 个脚本、文档行号（09-25 定价） | 动被交易的数 | 高 | **无**；已否决 | E-PR22 |
| 不行动 | — | 0 | 0 | 缺口继续无上限；下一次宿主离线才发现监控同宿 | — | — | — |

四个必答：

1. **有没有 20% 成本拿 80% 结果的方案？** 有，就是 Phase 0：五项里四项各不超过 150 行、零构造、零 ledger、本周可落，覆盖 M-PR02、M-PR03 的一半、S-PR1、S-PR4。Phase 1–3 是剩下的 20%。
2. **技术优势是否转化为可感知价值？** 操作者可感知的是：日报多两节（日期翻转、parity 距离）、少一件要记的事（重启窗口）、宿主停了手机会响、每次 PR 的税变成一个数。
3. **迁移、学习和信任成本是否抵消增量？** 归档 scratchpad 脚本要多一步 `git worktree add`；ratchet 记录要跳一次；两者都有补偿且可逆（`git mv` 回来）。
4. **什么证据会证明本方案并非相对最优？** ~~①WP-C1 落地 30 天后 M-PR01 仍 > 20%；②GAP-PR03 重放为 0 次；③Q3 答「已有宿主外告警」；④操作者答 Q1=B~~ **[R 修订]** 审查 §8 指出这四条没有一条能在「方案对」与「方案错」之间做区分（①可被 headroom 直接满足；②候选 bar 09-15 起为 0；③答案已在仓库；④是范围问题）。修订版：①卫生项落地后 M-PR02 仍 ≥ 1（归位没有让本地与 CI 说同一句话）；②M-PR03 的「失败周期」一列在换代理节点后 30 天不降（传输层不是丢 bar 的主因，09-22 判读错了）；③GAP-PR06 剖析显示 validate 每格耗时 ≥ 50% 在 `beidou_alpha` 的纯函数里（吞吐瓶颈确在 alpha 代码，Q6 的 A 应上调）；④GAP-PR08 显示 ratchet 文件每周 ≥ 1 次可归因的冲突或延时（「触碰只是增长事件」低估了它）。

**[R 修订，KILL-04] 同一把尺子重定价。** 冻结稿给 O-PR4 记「51k 行测试 import」，给自己的 WP 记净行数，且把 RISK-PR01 记「低」却不给 O-PR4 同样的 #135 协议兜底。按同一单位——**毛触碰行数 + 同一验收协议（D-PR05）+ 同一组问题级指标（M-PR01–07）**：O-PR4「按 7 步流水线重排包」的毛触碰是引用 `beidou_alpha` 的 import 行约 851 条（tests 479 / 其它包 149 / scratchpad 223，审查者 R-04 计数）+ 文档行号，而 #55、#135 都证明用原地址再导出可以把改动面压到接近 0、产物逐字节相同；它对 M-PR01–07 的增量仍是 0，因为这七个量没有一个测代码布局。卫生项的毛触碰：WP-P4 约 50 行、WP-C6 约 40、WP-C7 2、WP-C8 约 80（测试）、WP-C9 约 75、WP-P3 约 265；对 M-PR02/04 有直接增量。冻结稿里「现有绕行约 60%」「流程/人工约 30%」两个比例没有出处，标 E5。

### 4.3 Business Case

| 项目 | 基线/范围 | 结论 | IDs |
| --- | --- | --- | --- |
| 用户/业务收益 | 宿主离线的告警延迟：今天无上限 → ≤ 2 小时；重启迟到：昨日最差 2,154s → 命令化后 0；本地/CI 分歧：≥1 → 0；抬顶触碰：31.9% → 目标 < 15%（Q2=A）或 < 25%（Q2=B） | 收益全部以 M-PR 读数表达，不折算成收益率——本文不改任何交易行为 | §3.5 |
| 一次性成本 | Phase 0：约 1 个会话日、净增 ≤ +350 行（cli +150、deploy +40、tests +160，估）；Phase 1：约 1–1.5 会话日、`tests/architecture` **−4,000 行**、`docs/` +4,300 行、scratchpad 目录搬迁 0 行净变；Phase 2：约 1.5 会话日、cli/live +300、report +80；Phase 3：约 1 会话日设计文 + exchange/live +200 | 合计 ≤ 5 个会话日；每个包的 ratchet 影响写在 §6.1 | §6.1 |
| 持续成本 | 一个第三方账号（免费档）；夜间 job 多跑 7 条测试（约 20s）；`governance calendar` 每日一行 | 低 | — |
| 风险与机会成本 | RISK-PR01（动了被交易的数）由 M-PR04/05 与 #135 协议兜住；机会成本是这 5 个会话日不做 alpha 侧的事——而 alpha 侧此刻的下一步（前向板攒年、新信息族）本来就不由工程会话日决定 | 可接受 | §5.6 |
| ROI / Payback | 不折算金额。按操作者时间：WP-P2 每天省 2–3 次人工判断窗口；WP-C1 每 3 次提交省一次读 4,000 行 | — | — |
| 最小验证投入 | Phase 0 的 WP-P4（本地专属测试归位）：< 60 行，一天内可验（夜间 job 跑一次即知） | — | §9.4 |

**[R 修订，KILL-11] 按操作者注意力重定价。** 冻结稿一边用 19.71% 说「工程项一直在挤 alpha」，一边说本方案机会成本可忽略；而 19.71% 是 40 个提交、约 23 小时的读数（E-PR41）。本方案最稀缺的资源是操作者的注意力（P1.1 第一类损失）。按它定价：修订后的方案要操作者**答 6 个问题、合并 0 个卫生 PR（它们走自动合并）、人工合并 0–4 个治理类 PR（取决于答案）、读日报新增 2 节**（归档专属测试状态、日期翻转预告）；冻结稿版本要他答 5 问、读 16 个 PR、日报新增 3–4 节。同一单位下，卫生项对 alpha 侧的挤占是 0（不碰 `beidou_alpha`，不花 ledger，不占预登记名额）。

### 4.4 Economic Sustainability

N/A：单人系统，无付款方 / 供给方 / 平台方结构；第三方 dead-man 的免费档足够一个每小时一次的 ping。

### 4.5 Cost of Delay

| 时间 | 不做损失 | 是否可接受 | IDs |
| --- | --- | --- | --- |
| 1 周 | 约 17 次重启继续靠人算窗口；本地红继续无人知；2–3 次 PR 抬顶 | 可接受 | E-PR03、E-PR06 |
| 1 个月 | ~~约 74 次重启；M-010 若因某次跨 bar 的重启少一次退出检查就是一次真实回撤事件（概率 09-15 量过：约 1.16%/次）~~ **[R 修订，KILL-06(d)]** 1.16% 出自 RESEARCH_LOG:10912，量的是**周期失败率**（346 个 armed 周期里 4 次，四次都是传输层 ERROR），不是重启；CLAUDE.md 后来把它写进「重启」一节，冻结稿继承了这条漂移。按 E-PR39 的口径：一个月约 **16 根 bar 因失败周期没有退出检查**（7 根 / 13 天），0 根因重启；10-13 bridge 到期那天没有一张日期表 | 勉强可接受 | E-PR39、E-PR16 |
| 1 个季度 | ~~若操作者在此期间上 mainnet 小额，则宿主离线 = 真钱无人看管~~ **[R 修订，KILL-07]** mainnet 在 memory 里明写 out of scope；一个季度里非 alpha 若按近周速度再长（E-PR38），09-04「暂时承担」的缺口会再翻一倍而仍无裁定；宿主离线的残余风险维持 ACCEPTED，但「第二通道未配置」这条新事实（E-PR37）没人处理 | 勉强可接受（冻结稿写「不可接受」写过头了） | C-PR05、E-PR37 |

### Gate Review：G3、G4

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G3 | PASS | C-PR01、C-PR03；§4.2 | 「重排 alpha」Unproven，已出 Scope；Phase 0–1 Strong | — | — | §6 |
| G4 | PASS | C-PR05；§4.1、§4.3 | Strategic Fit High；无经济角色 | — | — | §6 |

---

## 5. Phase 5 · System Analysis

**Prerequisites**：G1–G4 PASS；E-PR34（三个只读子代理的盘点，数字已复核）。**Produces**：As-Is/To-Be、Constraint Map、R0–R6、Engineering Pre-check、RISK-PR01–09、E-PR35。**Downstream**：§6 Option 的定价与 Failure Modes、§9 契约的边界与回滚。

### 5.1 Product Layer

三层同时存在，按最高风险层分析：**平台能力**（实盘循环、启动门、部署、监控）> **组织流程**（PR 流程、ratchet、重启纪律、并行会话）> **规则策略**（alpha、治理阈值——本文不动）。

### 5.2 As-Is / To-Be

| 维度 | As-Is（读数） | To-Be | Gap | IDs |
| --- | --- | --- | --- | --- |
| `beidou_live` 的构成 | 42 文件 15,318 行；`engine.py` 2,501（16.3%）；`report_*` 10 文件 5,506 行（35.9%）；加 `risk_budget` 960、`execution_fidelity` 595、`benchmark` 468、`factor_loadings` 430、`verify` 334、`bar_sanity` 312、`soak` 218、`health` 126，**仪器合计约 8,949 行 = 58.4%**——「实盘循环」这个包一多半是仪器；`engine.py:49` `from beidou_live.reports import collateral_share`，所以 armed 进程的 import 闭包含整个报告层 | 不拆包（09-25 已否决重排）；只把 armed 进程的 import 闭包与报告层分开：`collateral_share` 搬到 `report_common`/`risk_budget`，一条测试断言 `import beidou_live.engine` 之后 `sys.modules` 里没有 `beidou_live.report_*` | 一次 import 时的报告层异常能让循环起不来 | E-PR34（子代理 A §1、§7） |
| `beidou_cli` 的构成 | 25 文件 8,623 行；研究逻辑集中在 5 个非命令模块（`research_book_eval` 628、`research_feature_store` 504、`research_ledger_io` 356、`research_panel` 331、`research_report` 250）；`research_cmd.py` 153 行 0 个 def，再导出 80 个名字（57 私有 + 23 公开），其中 35 个由 `tests/cli/test_nine_commands_nine_modules.py` 钉为同一对象；`report` 组的 3 个命令定义在 `live_cmd.py` | 不下沉（「先 M6 后下沉」的第二半要抬 alpha 的顶约 2,000 行并带证据，本文 Won't）；再导出面随 WP-C3 收缩 | 隐藏 API 面 80 个名字 | E-PR23、E-PR34（子代理 B §1） |
| `scratchpad/` | 82 个 .py、13,782 行、入库；42 个脚本 import `beidou_cli`，41 个 import 至少一个下划线名字，32 个走 `research_cmd` 地址；最常用 `_load` 38、`_membership` 38、`_resolve_symbols` 37；4 条测试以代码方式扫描 scratchpad；CI 零引用，但 `ruff check` 会 lint 它们 | 已产出结论的脚本归档到 `scratchpad/archive/<日期>-<sha>/`，README 钉 commit；地址契约只覆盖仍在用的脚本 | 每次改 CLI 私有名都要顾 42 个脚本 | E-PR34（子代理 B §2） |
| `beidou_governance` | 16 文件 4,287 行（定稿时 4,357，E-PR36）；`Policy` 25 字段、PROVENANCE E5 四条（R1/R4/R5/R7）；`family_gate` 每日 02:30 写 verdicts（~~job 上次退出 1~~ **[R 修订，KILL-14]** 主会话 12:03 读 `launchctl list` 为 0；09-27 已写 `allow` 行，E-PR40）；`advance --commit` 无 job；`replay.py` 985 行 | 不动阈值；日报加「未处理的 refuse 行数 / 上次 advance 时间」；`calendar` 命令读全部日期开关 | 有规则无执行者 | E-PR14、E-PR26、E-PR34（子代理 B §3、§6） |
| `beidou_data` / `.beidou/data` | 15 文件 3,642 行；三个来源（`data.binance.vision`、`fapi`、`api.binance.com` spot）、三种 parquet store；`klines/` 891 个符号目录 1,769 文件、`funding/` 274、`spot_klines/` 378、`metrics/` 206、`metrics_snapshot/` 21；`membership.parquet` 每行成员 15–20 | 不动；WP-A1 只读它出一个「覆盖 ÷ lookback」读数 | parity 距离无读数 | E-PR12、E-PR28、E-PR34（子代理 B §5） |
| 测试套件 | 317 文件 60,226 行、2,369 个 `def test_`（含参数化 2,813 个用例）；测试/源码 1.37（live 1.53、exchange 1.90）；本机 154.8s、CI 顶 600s（120→240→400→600 四次抬顶）；`tests/architecture` 5,494 行里 4,383 行是 ratchet 文件、其中 4,316 行注释（98.5%）；读 `.beidou/` 的测试 **7 条**（本机跑、CI/worktree 跳）、读真实报告 4 条（2 条 CI 也跑）、读 shipped config pair ≤ 60 文件（上界）；`monkeypatch.setattr` 70 次 / 35 文件（`data_cmd` 21）；断源码文本的测试 `inspect.getsource` 12 文件、`read_text` 9、`ast.parse` 10；按日期翻转的只有 2 条且今天都惰性 | 不按行数删测试（09-13 明写）；7 条 `.beidou/` 读者加 `archive` 标记并给它们一个执行位置（夜间 job）；断源码文本的测试不动——它们是这个仓库把「规则」变成「检查」的方式 | 本地/CI 分歧无人看 | E-PR08、E-PR34（子代理 C §1–§5） |
| 部署与运行 | 7 个 plist 全在本机；`launchctl list`（主会话 2026-09-28 读数）：live PID 96836（上次退出 -9，即 `kickstart -k`）、paper-l3 与 shadow 常驻，check 上次退出 1、data / forward-board / governance-gate 上次退出 0（只记读数不归因）；`run_live.sh` 读 `env.sh`、bridge 到 10-13、`--armed`；重启 60 次；启动门在 `live_cmd.py:304-316`（evidence + dataset blocking） | dead-man 在宿主外；重启命令化；日期表 | 监控同宿、重启靠人 | E-PR20、E-PR29、E-PR34（子代理 A §6、B §6） |
| 两侧重复的语义 | band：alpha `apply_no_trade_band` 与 live `plan_rebalance` **有意**各一份（D-033，测试钉住「不是同一规则」），三个开关靠「必须一起翻」的约定 + 测试；participation：两份 + 跟踪测试；drawdown throttle：标量共用、回撤序列各算、无对比测试（且关着）；exits：`exit_step` 共用、live 只做 I/O，但 **`ExitOverlay.apply` 与 `apply_exits` 没有同输入对比测试**；ladder：共用纯函数 + 12 条逐位测试；vol targeting：单实现；staleness：五条规则三个包，按设计分叉并有表测试 | 补一条 exits 同输入测试；其余不动 | 一处缺失的同构测试 | E-PR34（子代理 A §3） |
| 配置面 | `live.demo.yaml` 14 个顶层键、61 个二级键；`LiveConfig` 35 字段（定义在 `engine.py:132`，`config.py` 是加载器）；**E-PR35**：`risk_budget` 块 15 键里只有 `min_liq_distance` 进 `LiveConfig`（`config.py:153`），其余 14 键由 `RiskBudgetParams.from_mapping` 在**报告路径**读（`live_cmd.py:868`），而引擎的 ladder 尺子用 `RiskBudgetParams()` **默认值**（`engine.py:1793`），读的是 `deescalate_at / rollback_at / deescalate_to / rollback_to` 四个字段；今天 YAML 值与默认值逐字段相等，所以无分叉——但这是一组循环不读的旋钮 | 一条测试：`live.demo.yaml` 每个键要么被 `LiveConfig`/引擎读，要么在一张「只供报告」表里声明 | KILL-Q15 的形状（改 YAML 不改循环） | E-PR35 |
| 端口与接线 | `ports.py` 10 个 Protocol；`BinanceUsdmVenue` 结构化实现无继承；live 直接 import `beidou_exchange` 只有 `config.py`（组装根）与两处纯函数；**`live_cmd.py:326` `venue: Any`**，mypy 在接线点不对 `BinanceUsdmVenue` 做 `Venue` 的结构检查 | 把 `Any` 改成 `Venue`，让 mypy 在接线点检查 | 一处类型空洞 | E-PR34（子代理 A §8） |
| 归档专属测试的现状 | BNX 夹具 48 行对归档 552 行：09-25 `data repair` 在 2023-02 的停牌窗口里写进了 504 根 bar；夹具原意「a fixture nobody can compare to its source is a fixture someone could have made up」——它做了自己的工作，只是没人看见 | 由操作者定夹具与归档哪个对（GAP-PR09）；机制上给它执行位置 | 红 3 天 | E-PR08 |

**E-PR35** · CODE+EXPERIMENT · `config/live.demo.yaml` `risk_budget` 块、`beidou_live/config.py:153`、`beidou_cli/live_cmd.py:868`、`beidou_live/engine.py:1793`、本机脚本比对 `RiskBudgetParams()` 与 `from_mapping(yaml)` · 2026-09-28 · 见上表「配置面」· 支持 C-PR02（配置与循环可分叉）· E1 · High · PUBLIC。

### 5.3 Constraint Map

| 类型 | 约束 | 是否可改变 | 对 Option/Scope 的影响 | Owner |
| --- | --- | --- | --- | --- |
| 合规/安全 | 公开仓库；凭据只在 `env.sh`；`guard.py` 只放行 demo/testnet | 前两条不可变；第三条只能由操作者签字改 | WP-P1 的 URL 只进 `env.sh`；WP-P6 只设计不放行 | 操作者 |
| 流程 | PR 流程、四道门走 `.venv/bin/`、CI 全绿自动合并；`--no-verify` 不是日常开关 | 不变 | 每个 WP 一个 PR | 操作者 |
| 流程 | ratchet：抬顶只在写理由的那个 commit 里，理由紧挨常量（CLAUDE.md） | **可变，但只能由操作者改**（Q2） | WP-C1 的形状 | 操作者 |
| 技术 | 依赖方向（`test_import_rules.py`）；alpha 零 `beidou_*` 依赖 | 不变 | WP-A1/A2 落在 cli/live，不落 alpha | — |
| 技术 | 阈值在代码里、有 digest（R10）；construction fingerprint 有版本与别名 | 不变 | 任何 WP 不动 `Policy`；不动指纹字段集 | — |
| 治理 | K-EX14：构造变更只在窗口里；M-010 30 天窗口随构造变更清零 | 不变 | 全部 WP 零构造变更；WP-P2 第二半若做要预登记 | 操作者 |
| 运维 | launchd 跑主 checkout；bash 3.2；到场地的路径经 1082 代理会间歇 503；「合入不等于生效」 | 不变 | 每个 WP 写「何时生效」；dead-man 要容忍单次 ping 失败 | 操作者 |
| 组织 | 多会话并行、共用主工作树；41 次提交/天 | 不变 | WP-C1 选安静窗口、一次 PR；小 PR | — |
| 资源 | 操作者阅读带宽；agent 会话日 | 有限 | Phase 0 ≤ 1 会话日、五个裁定各有默认 | 操作者 |

### 5.4 Impact Radius R0–R6

| 半径 | 检查内容 | 影响 | IDs |
| --- | --- | --- | --- |
| R0 当前功能 | 实盘循环的每根 bar：**零变化**（M-PR04） | 无 | D-PR05 |
| R1 上下游流程 | 重启流程（人 → 命令）；夜间数据任务多跑 7 条测试；每小时巡检多发一个 ping | 低 | WP-P1/P2/P4 |
| R2 横向模块 | `beidou_exchange`：WP-P6 的 allowlist 参数化（不放行）；`beidou_governance`：`calendar`；`beidou_cli`：3 个新命令 | 中（三个包各抬一次顶） | §6.1 定价 |
| R3 运营/后台 | 日报多三节（日期翻转、parity 距离、未处理 refuse）；RUNBOOK 改重启一节 | 低 | WP-P3/P5/A1 |
| R4 数据/日志/指标 | 新增 `reports/research/looks.jsonl`（入库）；`scratchpad/archive/`；`docs/SOURCE_BUDGET_LOG.md`；周报 effort_share 在搬迁周会被 −4,000/+4,300 行扰动 | 中 | RISK-PR08 |
| R5 研发协作 | 并行会话在 ratchet 文件上的冲突面缩小；scratchpad 脚本作者要多一步 | 低 | 3.7 受损方 |
| R6 维护/认知 | 少四件「要人记住」的事；多一张日期表与一份记录文件要维护 | 净减 | — |

### 5.5 Engineering Pre-check

| 维度 | 已知事实 | 未知项/实验 | 风险/失败动作 |
| --- | --- | --- | --- |
| 数据 | 全部 WP 只读 `.beidou/`；`looks.jsonl` 是新的只追加文件，不是 ledger | `looks` 何时计入 N：A-PR07 | 只记录不计入，直到裁定 |
| API | 新命令 3 个（`live restart`、`governance calendar`、`research look`）；无外部 API 变化 | — | — |
| 状态 | `state.json` 不动；`restarts` 由既有路径 +1 | — | — |
| 权限 | dead-man URL 与凭据同级（RESTRICTED，`env.sh`）；WP-P6 的签字文件类比 `governance/ENABLED` | Q3 | 不接受 → 退到自备设备 |
| 安全 | 公开仓库：ping URL、mainnet key 名不入库；`.gitleaks.toml` 是否覆盖新的 URL 形状 | 核 `.gitleaks.toml` 对 `https://hc-ping.com/...` 类 URL 的规则 | 加一条自定义规则 |
| 性能 | 夜间 job 多约 20s；`calendar` 毫秒级；`research look` 一次约 4 分钟（09-27 实测） | — | — |
| 容量 | `cycles.jsonl` 4.3 MB / 25 天，不在本文范围 | — | Won't（换存储） |
| 迁移 | WP-C1：4,300 行 `git mv` 级搬迁，逐字；WP-C3：`git mv` 目录 | 搬迁后 `grep` 引用（RESEARCH_LOG 引用 `test_source_budget.py:行号` 的地方会失效） | 搬迁 PR 附「旧行号 → 新位置」对照表 |
| 依赖 | 无新 Python 依赖；dead-man 用 `curl` | — | — |
| 日志/告警 | dead-man 是第三条通道；与 DL-L3 双 webhook 并存 | — | — |
| 降级 | 每个 WP 关掉就是今天 | — | — |
| 回滚 | 全部 git revert 级；无数据迁移不可逆点 | — | — |

### 5.6 Risk Register

记录式，字段：风险事件 · 关联 · 概率 · 影响 · 预警 · 预防/缓解 · 失败动作 · 人类确认点。

**RISK-PR01** 某个「重构」PR 动了被交易的数（权重、订单、退出）· C-PR01、D-PR05 · 低 · 高 · 预警：PR 前后 `construction` / `registry` / `policy` 三个 digest 任一不同；`live verify --check` 非零 · 预防：每个 PR 跑两条构造测试 + #135 协议（实盘状态快照上产物逐字节相同）· 失败动作：revert · 确认点：操作者合并。

**RISK-PR02** 操作者把本文读成又一次「等」· §0 · 中 · 中 · 预警：本文交付后 7 天内 Phase 0 无一项开工 · 预防：Phase 0 五项各有日期、验收读数、不依赖任何窗口 · 失败动作：把 WP-P4 单独成 PR 先落 · 确认点：无。

**RISK-PR03** ~~dead-man 泄漏宿主元数据或 URL 入库 · 低 · 中 · 预防：URL 在 `env.sh`~~ **[R 修订，KILL-01/05、RT-4]** dead-man 的 URL 是一枚**能压制告警的令牌**（谁拿到它谁就能持续 ping 让告警永不触发），在公开仓库下与凭据同级：不进仓库、不进 agent 会读的文件、不进日志；变量放在凭据实际所在的位置（今天是 `~/.zshrc` 的 `export BEIDOU_*` 行），**不新建 `env.sh`**；`.gitleaks.toml` 加规则时避开 memory 记的两个静默坑（RE2 不支持 lookahead 会 panic；allowlist 的 regex 默认匹配 secret）并配一条会红的金丝雀 · 中 · 高 · 预警：gitleaks 命中；`live.stderr.log`「demo credentials are not in the environment」· 失败动作：换 URL · 确认点：Q3 重开 D-P4 后由操作者建账号并放变量（HC-1、HC-2）。

**RISK-PR10** **[R 修订，KILL-01]** 任何人（含按本文旧版执行的会话）在 `~/Library/Application Support/beidou/` 新建一个不含 Binance 凭据的 `env.sh` · 全部 job · 中 · **高** · 预警：`run_live.sh` 以 78 退出；heartbeat 两个整点不更新；`check.stdout.log` 有 `FAIL` 而无送达记录（两个 webhook 变量为空时 `notify` 只打印一行）· 预防：CLAUDE.md「凭据只有一个位置」那句改为可核的事实（今天在 `~/.zshrc`；若建 `env.sh` 必须先把全部 `BEIDOU_*` 变量迁过去）· 失败动作：删除该 `env.sh` 或补齐变量，再按纪律重启 · 确认点：HC-1（操作者）。

**RISK-PR04** ratchet 搬迁 PR 与并行会话同日抬顶冲突 · WP-C1 · 高 · 低 · 预警：`git fetch` 后 rebase 冲突 · 预防：选安静窗口、一次 PR、搬迁与政策分两个 commit · 失败动作：重新生成搬迁（脚本化，幂等）· 确认点：无。

**RISK-PR05** mainnet 骨架被读成「已放行」· WP-P6 · 低 · 高 · 预警：`guard.py` 收到 mainnet host · 预防：放行需签字文件 + 显式旗标同时存在，本 Phase 不实现放行分支；测试断言拒绝 · 失败动作：删除骨架 · 确认点：操作者签字。

**RISK-PR06** 归档的 scratchpad 脚本失去可复现性 · WP-C3 · 中 · 低 · 预警：有人要复现某段 RESEARCH_LOG 而找不到脚本 · 预防：归档目录 README 钉 commit 与一行 `git worktree add` 配方 · 失败动作：`git mv` 回来 · 确认点：无。

**RISK-PR07** 漏 bar 退出补评估双触发或用了过期数据 · WP-P2 第二半 · 中 · 高 · 预警：GAP-PR03 重放里出现「补评估触发、真实历史未触发」的差异 · 预防：先重放再预登记；不做则只做第一半 · 失败动作：不做 · 确认点：操作者签预登记。

**RISK-PR08** 搬迁周的周报 `effort_share` 被 −4,000/+4,300 行扰动，读成「基础设施投入暴涨」· WP-C1 · 高 · 低 · 预警：周报 `lines.infrastructure` 异常 · 预防：那一周的周报单独标注（KILL-AM-10 先例：搬家行数不冒充任何投入）· 失败动作：无 · 确认点：无。

**RISK-PR09** `data repair` 再次改动夹具对应的切片而夜间 job 未装载 · WP-P4 · 中 · 低 · 预警：日报「归档专属测试」一节缺席 · 预防：job 装载是操作者动作，RUNBOOK 写明；日报在 job 未跑时印「未跑」而不是「通过」· 失败动作：手跑 · 确认点：操作者装载。

### Gate Review：G5

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G5 | PASS | E-PR34、E-PR35；RISK-PR01–09 | 每个 WP 有触碰的包、估行数、ratchet 影响、是否改构造、是否重启、回滚方式 | Weak GO（沿 G2） | — | §6 |

---

## 6. Phase 6 · Solution Reasoning

**Prerequisites**：G1–G5。**Produces**：O-PR0–5、FM-PR1–8、D-PR01–05。**Downstream**：§7 审查对象、§8 Scope、§9 契约。

### 6.1 Option Set

| Option ID | 方案 | 用户价值 | 业务价值 | Evidence | 成本 | 风险 | 可逆性 | 验证速度 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| O-PR0 No-Build | 维持现状：定向批次 + 纪律 | 0 | 0 | — | 0 | 缺口无上限；宿主离线无人知 | — | — | 放弃，但作为每个 WP 的对照 |
| O-PR1 Process | 只写文档：生产定义、预算重定价、把四件「要人记住」的事写进 CLAUDE.md | 低 | 低 | E-PR14/19（三次「文件不是 job」）| 0.5 日 | 「没有执行检查的规则是承诺」 | 完全 | 立即 | 只取其中两条裁定（D-PR02、D-PR03）；其余不采 |
| O-PR2 Small-Build「去税不动数」 | WP-P4、P3、P1、C1、C2、C3、C6、C7、C8、C9 | 每次 PR 的税变成数；四件事机器化 | M-PR01/02 直接下降 | E-PR03/08/34/35 | 约 2.5 日；源码净 +≈400（cli +160、governance +120、live +60、deploy +50）、tests −4,000/+≈500、docs +4,300 | RISK-PR04/06/08 | `git revert` | 每项 ≤ 1 天可验 | **推荐（Phase 0–1）** |
| O-PR3 Full-Build「生产边界」 | WP-P2（重启命令化 + 可选的漏 bar 补评估）、WP-A1、WP-A2、WP-P5 | 重启不再靠人；parity 有读数；looks 有记录 | M-PR03/06 | E-PR06/07/12/28 | 约 2 日；cli +≈420、live +≈110、tests +≈300 | RISK-PR07 | `git revert`；第二半要预登记 | 重放 0.5 日后可决 | **推荐（Phase 2）** |
| O-PR4 System-Build「重排/重写」 | 按 7 步流水线重排包；重写 alpha 为插件框架；拆 engine.py；研究逻辑下沉 alpha | 对 M-PR01–06 增量 0 | 0 | E-PR22（已否决）；09-13「不建议为了行数而拆」 | 51k 行测试 import、29–42 个脚本、文档行号；下沉要抬 alpha 顶约 2,000 行并带证据 | 动被交易的数 | 差 | 慢 | **Won't** |
| O-PR5「mainnet 准入设计」 | WP-P6：准入清单、profile 骨架、guard 参数化（不放行）、成本模型校准方案 | 「上不上」第一次有清单 | 决定下一阶段的方向 | E-PR18 | 约 1 日；exchange +40（顶 0 → 抬）、cli +40、tests +120、docs +200 | RISK-PR05 | 删除骨架 | 一天 | **推荐（Phase 3，Q1=A 时）** |

每个 WP 的定价（估算，单位：行；「顶」指 `test_source_budget.py` 要不要在同一 PR 抬并写理由；alpha 一律 0）：

| WP | Phase | 做什么 | 触碰的包与估行数 | 抬顶 | 改构造 / 要重启 / ledger | 前置裁定 | 何时生效 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WP-P4 归档专属测试归位 | 0 | 7 条读 `.beidou/` 的测试加 `@pytest.mark.archive`；`pyproject` 注册 marker；`run_data.sh` 在夜间同步后跑 `pytest -m archive`，失败走 `run_check.sh` 同一条 `notify`；日报印「归档专属测试：通过 / 失败 / 未跑」 | tests +10、pyproject +1、deploy +12、live（报告）+20 | live 用 headroom（40） | 否 / 否 / 0 | 无 | 主 checkout 快进后的下一个夜间 job |
| WP-P3 日期开关登记表 | 0 | `beidou governance calendar`：读 `run_live.sh` `BRIDGE_UNTIL`、`policy.py` `SINGLE_WINDOW_MINE_OPENING_ENDS`、`reopen.yaml` `date_after` ×3、`window_changes.yaml` `earliest_window`、registry `probe.accepted_on + review_after_days`、冻结测试 `FREEZE_ENDS`，列未来 60 天每次翻转、读者与后果；日报印未来 7 天；一条测试要求这些文件里出现的每个 `20xx-xx-xx` 常量都在登记表里 | governance +120、cli +40、live（报告）+25、tests +80 | governance（23）与 cli（33）要抬 | 否 / 否 / 0 | 无 | 合入即（命令）；日报待快进 |
| WP-P1 宿主外 dead-man | 0 | `run_check.sh` 末尾无论检查结果如何都 `curl` 一次 `$BEIDOU_DEADMAN_URL`（存在时）；连续两个整点无 ping 由服务端推送；URL 进 `env.sh`；RUNBOOK 加一节；测试按文本核「ping 在 notify 之后、且不依赖检查结果」 | deploy +8、docs +15、tests +40、`.gitleaks.toml` +1 规则 | 否 | 否 / 否 / 0 | **Q3** | 操作者建账号并写 `env.sh` 后的下一个 :10 |
| D-PR02 / D-PR03 | 0 | 两条裁定：生产定义三层；`PLAN_BUDGET` 重定价（默认：今天 +5% 为新预算，缺口测试改守新数） | tests ±10、CLAUDE.md +10 | 否 | 否 / 否 / 0 | **Q1、Q5** | 合入即 |
| WP-C1 ratchet 记录搬迁 | 1 | `test_source_budget.py` 的 4,316 行注释逐字搬到 `docs/SOURCE_BUDGET_LOG.md`（按日期分节、保留原编号）；测试里每个 CEILING 条目留一行 `# → SOURCE_BUDGET_LOG.md#<节>`；一条测试断言每个引用在文件里存在；附「旧行号 → 新节」对照表（RESEARCH_LOG 里引用了行号的地方按表改） | tests −4,100、docs +4,300 | 否 | 否 / 否 / 0 | **Q2** | 合入即 |
| WP-C2 headroom 政策显式化 | 1 | `HEADROOM_POLICY = {pkg: max(40, 2% × 当前顶)}` 写进测试；断言 `ceiling − measured ≤ 政策上限`（防囤积）且抬顶仍只在写理由的 commit 里；CLAUDE.md 改一句 | tests +30、CLAUDE.md +5 | 是（一次按政策抬齐：live +306、alpha +219、cli +172、governance +86、data +73、exchange +15、shared +6，理由就是本条） | 否 / 否 / 0 | **Q2**（与 C1 同答） | 合入即 |
| WP-C3 scratchpad 归档 | 1 | 结论已进 RESEARCH_LOG 且不被 4 条扫描测试引用的脚本 `git mv` 到 `scratchpad/archive/<日期>-<sha>/`，每个目录一个 README（跑它的 commit、一行 `git worktree add` 配方）；再导出面按剩余脚本收缩，`test_nine_commands_nine_modules` 的 35 个地址相应减少 | 0 源码；tests ±20；scratchpad 目录搬迁 | 否 | 否 / 否 / 0 | 无 | 合入即 |
| WP-C6 armed 进程的 import 闭包 | 1 | `collateral_share` 从 `reports.py` 搬到 `report_common.py`；`engine.py:49` 改 import；测试：import 引擎后 `sys.modules` 无 `beidou_live.report_*` | live ±0、tests +30 | 否 | 否 / **是**（引擎文件变了，按纪律重启才生效；不改构造）/ 0 | 无 | 下一次按纪律重启 |
| WP-C7 接线点类型 | 1 | `live_cmd.py:326` `venue: Any` → `Venue` | cli ±0 | 否 | 否 / 否 / 0 | 无 | 合入即（mypy 门） |
| WP-C8 exits 同输入测试 | 1 | 同一 Panel、同一 `ExitParams`、同一初始状态下 `ExitOverlay.apply`（逐 symbol）与 `apply_exits`（帧）权重逐位相同 | tests +80 | 否 | 否 / 否 / 0 | 无 | 合入即 |
| WP-C9 配置键读者测试 | 1 | `live.demo.yaml` 的 61 个二级键：要么在 `LiveConfig`/引擎路径被读，要么在 `config.py` 的 `REPORT_ONLY_KEYS` 表里声明（今天 14 个 `risk_budget` 键） | live +15、tests +60 | live 用 headroom | 否 / 否 / 0 | 无 | 合入即 |
| WP-P2 bar 安全重启 | 2 | `beidou live restart --reason "..."`：算安全窗口（整点后 5 分钟到下一整点前 10 分钟）并等待；跑两条构造测试；记录前后 `construction`/`registry` digest；`launchctl kickstart -k`；等新心跳；印出 RESEARCH_LOG 记录块。第二半（漏 bar 退出补评估）**只在 GAP-PR03 重放 ≥1 次后预登记再做** | cli +120、tests +100、docs +10 | cli 抬 | 否 / 它本身就是重启 / 0 | 无（第二半：操作者签预登记） | 合入即 |
| WP-A1 数据族 parity 仪器 | 2 | 日报「数据族 parity」一节：对 metrics 叶（OI、LSR、top-trader）与 spot basis，印实盘 `metrics_snapshot`/spot 覆盖 bars ÷ 读它的信号或挖掘叶的最长 lookback，与 `metrics_refusal` 同口径 | live（报告）+80、tests +60 | live 抬 | 否 / 否 / 0 | 无 | 日报待快进 |
| WP-A2 `research look` | 2 | 把 `scratchpad/new_family_look.py` 升为命令：spec（写法、方向、判据）先落文件并出 sha；宽度门按 pit 成员表中位数计算而不是写死 50；读数 JSON；追加 `reports/research/looks.jsonl`（spec sha、prereg commit、readings、判定）；**不写 ledger、不改 N**；09-27 的 8 个写法作为回归夹具逐位复现 | cli +300、tests +150、reports 新文件 | cli 抬 | 否 / 否 / 0 | A-PR07（记录规则默认「只记录」） | 合入即 |
| WP-P5 治理链执行者读数 | 2 | 日报印「未处理的 family gate refuse 行数、上次 `advance --commit` 时间」；`report weekly` 排进周日 job（新 plist，操作者装载）| live（报告）+30、deploy +1 plist +1 脚本 | live 用 headroom | 否 / 否 / 0 | **Q4** | 装载后 |
| WP-P6 mainnet 准入设计 | 3 | `docs/MAINNET_READINESS.md`（准入清单：M-010 满 30 天、L3 7 天、滑点带内、参与率/冲击曲线在目标资金下、KILL-Q12 重开、密钥与 kill-switch 分离、操作者签字）；`config/live.mainnet.yaml` 骨架（只有键名与 env 变量名）；`guard.py` allowlist 改为 profile 驱动，放行分支**不实现**，测试断言 mainnet host 仍被拒 | exchange +40、cli +40、tests +120、docs +200 | exchange（0）抬 | 否 / 否 / 0 | **Q1=A** | 设计文合入即；放行另一次裁定 |
| WP-C4 配置叙事外移 | 3（Could） | `alpha_registry.yaml` 与 `live.demo.yaml` 的审计注释搬到 `docs/REGISTRY_LOG.md`，YAML 只留值与 `# D-xxx` 引用；`registry_fingerprint` 只哈希解析值，digest 不变 | docs +600、config −550 | 否 | 否 / 否 / 0 | 操作者偏好 | 合入即 |

**[R 修订] 审查后的工作包处置（上表是冻结稿版本，保留以便对照）：**

| WP | 处置 | 依据 |
| --- | --- | --- |
| WP-P4 归档专属测试归位 | **保留，卫生批** | 审查未攻击，前提复核成立（§3.3）；E-PR08 |
| WP-P3 日期开关登记表 | **保留，卫生批**；输入清单按 E-PR36 重算（`reopen.yaml` 少一条 10-03 的 `date_after`）；验收改为「翻转前 7 天日报有预告」 | KILL-14、KILL-17 |
| WP-P1 宿主外 dead-man | **撤出**，改为 Q3（09-06 D-P4 的重开问题，附两条新事实）；规格若重写：变量放凭据实际位置、不新建 `env.sh`、ping 从巡检末尾无条件发、URL 视同凭据、逐条回应 09-05 KILL-R2 | KILL-01、KILL-05 |
| D-PR02 生产定义 | **改为 Q1 三选项**，默认 A「只 L-A」（E-PR42）；文档 `docs/PRODUCTION.md` 只在操作者答后写 | KILL-07 |
| D-PR03 预算重定价 | **改为 Q5**（增长率裁定）；「+5%」撤（1,644 行按近周速度 1–11 天用完，E-PR38）；无默认 | KILL-02(e)、KILL-10 |
| WP-C1 ratchet 记录搬迁 | **改为 Q2a**；它买的是文件大小与冲突面，不是触碰频率（对 M-PR01 旧口径最多约 1 个百分点）；由操作者合并 | KILL-02(b)、KILL-10 |
| WP-C2 headroom 政策 | **改为 Q2b**，无默认数字；表中抬幅按「顶的 2%」算而断言按「headroom ≤ 顶的 2%」写，五个包会超自己的断言（KILL-02(d)）；政策会让 87% 的历史抬幅不再逐次写理由（E-PR41 R-03c）——这是放松一道控制，只能由操作者裁 | KILL-02、KILL-10 |
| WP-C3 scratchpad 归档 | **出 Scope（Won't）**：补偿「钉 commit」钉不住数据（09-25 `data repair` 后 BNX 已回不去；memory 记 p32f/g/h 要截回 09-23 的 store 状态）；#55 保留 35 个地址的理由正是这些脚本 | KILL-15 |
| WP-C6 armed 进程 import 闭包 | **保留，卫生批**；生效方式写成「搭下一次按纪律的重启」，进 RUNBOOK；不改构造 | KILL-12(c) |
| WP-C7 接线点类型 | **保留，卫生批** | — |
| WP-C8 exits 同输入测试 | **保留，卫生批** | — |
| WP-C9 配置键读者测试 | **保留，卫生批**（E-PR35 复核成立） | — |
| WP-P2 bar 安全重启 | **出 Scope**：要解的问题 09-15 起没发生（E-PR39）；命令化会让任何会话都能重启而此前至少一次重启由操作者裁定（09-17 #52）；第二半随 A-PR05 关闭 | KILL-06 |
| WP-A1 数据族 parity 仪器 | **保留，Should**（等卫生批之后，因为它要抬 live 的顶并写理由）；对 M-PR06 | 审查未攻击 |
| WP-A2 `research look` | **等裁定（HC-6）**：默认改为按 09-27 原文「看过的写法计入 N」；在操作者定计入口径之前不落地；冻结后又合入一个 612 行的零 ledger look 脚本（#199），说明需求真实、口径更紧迫 | KILL-09 |
| WP-P5 治理链执行者读数 | 前半**撤**（GAP-PR04 已由 `allow` 行关闭）；后半「周报排 job」保留为 Could（一个 plist，操作者装载） | KILL-14 |
| WP-P6 mainnet 准入设计 | **随 Q1**：A → 出 Scope；B → 只写文档，先引 09-05 的解除条件（E-PR42），`guard.py` 一字不动 | KILL-07、KILL-08 |
| WP-C4 配置叙事外移 | Could，不变 | — |
| 新增：操作者动作「换代理节点」 | 09-22 判读的唯一便宜解；不由 agent 做；对 M-PR03 | E-PR39、memory |
| 新增：GAP-PR06 / GAP-PR08 / GAP-PR10 三项零 ledger 测量 | 由下一会话做，各 ≤ 0.5 日 | §2.5 |

### 6.2 Failure Modes

| Failure ID | Option/WP | 失败模式 | 原因 | 预警 | 预防 | 失败动作 | Risk/Test IDs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FM-PR1 | WP-P1 | dead-man 每小时都响或从不响 | ping 放在 `set -e` 之前的失败分支；代理 503 | 第一周的告警频次 | ping 放在脚本末尾、不依赖检查结果；服务端阈值 2 个整点 | 调阈值 / 撤 | RISK-PR03、T-PR1a/b |
| FM-PR2 | WP-P2 | 命令等窗口时会话超时或被并行会话再次重启 | 窗口最长等 50 分钟 | 命令打印等待时长 | `--wait-max` 上限、超时不重启 | 手动按 RUNBOOK | T-PR2a |
| FM-PR3 | WP-P3 | 登记表本身过期（新日期写在新地方） | 又一处要记住 | 测试要求每个日期常量登记 | 测试用 grep 覆盖 deploy/、governance/、policy.py、tests/live 冻结测试 | 补登记 | T-PR3b、EC-PR5 |
| FM-PR4 | WP-P4 | 夜间 job 红了没人看 | 与 `run_check.sh` 不同通道 | 日报一节缺席 | 走同一条 `notify` | 手跑 | RISK-PR09、AC-PR4b |
| FM-PR5 | WP-C1 | 搬迁改了字 | 手工搬 | 逐字 diff | 脚本化：从 git 原文提取、写入、再 diff 为空 | 重跑脚本 | T-PR-C1a |
| FM-PR6 | WP-C3 | 归档了正在被引用的脚本 | 判据只看名字（09-16 的错） | 4 条扫描测试红 | 判据：四道门全量 + 4 条扫描测试 + `grep -rn scratchpad/<名>` 于 docs/tests 零命中才归档 | `git mv` 回来 | RISK-PR06 |
| FM-PR7 | WP-A2 | `looks.jsonl` 被当作免费窥视通道 | 记录但不计入 N | 每次 look 的 spec sha 与 prereg commit | 记录规则写死「看过的写法以后再提是新假设、要算 N」——与 09-27 那节同句；何时计入由操作者裁 | 关命令 | A-PR07 |
| FM-PR8 | WP-P6 | 骨架被误用于 armed 启动 | 文件在仓库里 | guard 拒绝日志 | 放行分支不实现；测试断言拒绝 | 删骨架 | RISK-PR05、T-PR6a |

### 6.3 Recommendation Decision（D-PR01）

**[R 修订]** 下表是修订版；冻结稿版本推荐 O-PR2 + O-PR3 + O-PR5 四个 Phase，其 Falsifier 四条被审查 §8 判「没有一条能在方案对与方案错之间做区分」，原表不再重印（在 git 历史与审查稿里）。

| 字段 | 内容 |
| --- | --- |
| 推荐 Option | **O-PR2 的卫生子集**（WP-P4、C6、C7、C8、C9、P3）按 Weak GO 的字面含义受控开工 + **六个带价问题**（Q1、Q2a、Q2b、Q3、Q5、Q6）交操作者 + **一项操作者动作**（换代理节点）+ **三项零 ledger 测量**（GAP-PR06/08/10）。O-PR3 与 O-PR5 全部退出执行授权 |
| 胜出理由 | 卫生六项：审查未攻击、前提复核成立、不改任何一道控制、对 M-PR02/M-PR04 有直接读数、每项 ≤ 1 天、可 revert。问题六个：每个都是「放松或改写一道控制」，只能由控制的所有者裁；本文把价钱与两侧后果写清，不给可执行的默认 |
| 放弃 Options | O-PR0（问题真实：五处缺口复核成立）；O-PR1 单独；**O-PR3**（WP-P1 建在读错的凭据位置与一条已 ACCEPTED 的决定上；WP-P2 要解的问题 09-15 起未发生；WP-A2 默认与原文相反）；**O-PR4**（同尺子重定价后对 M-PR01–07 增量仍为 0，且 F-A 未判——交 Q6）；**O-PR5**（mainnet out of scope；09-05 已有解除条件；不动 `guard.py`） |
| 关键成立条件 | 卫生项：每个 PR 过 D-PR05；WP-C6 搭下一次按纪律的重启。问题：未答不执行 |
| Falsifier（方案自己的） | ①卫生项落地后 M-PR02 仍 ≥ 1；②换代理节点 30 天后 M-PR03「失败周期」列不降（传输层不是主因）；③GAP-PR06 显示 validate 每格 ≥ 50% 时间在 `beidou_alpha`（吞吐瓶颈在 alpha 代码，F-A 要重开）；④GAP-PR08 显示 ratchet 文件每周 ≥ 1 次可归因冲突或延时（「触碰只是增长事件」低估了它）。每条都能把作者送回一个具体的判断，而不是「换杠杆」 |
| 灰度/降级/回滚 | 每项一个 PR、独立 revert；job 类由操作者装载；治理类 PR 由操作者合并 |
| 接受的损失（谁/为什么/补偿） | 操作者：要答六个问题（补偿：每个带价、答一个解锁一块）；CI 的「全绿」：不再假装覆盖归档专属测试（补偿：日报有它们的状态）；**撤回的补偿**：scratchpad 作者的「钉 commit」补偿不存在（KILL-15），所以 WP-C3 不做 |
| 最可能的偏差与对策 | **锚定提出者方案**：若在起作用，我会替操作者把「尤其是 alpha」改写成一个便于驳倒的命题——**这正是冻结稿做的事**（Pre-PR1，KILL-03(d)），本版以 Q6 把问题交回；**确认仓库叙事**：若在起作用，我会把已 ACCEPTED 的决定写成新发现——**冻结稿做了**（WP-P1 对 D-P4，KILL-05），本版引 D-P4 并只提新事实；**近因**：BNX 的普遍性靠 7 条计数；**沉没成本**：4,300 行记录不删——但搬不搬由 Q2a 定，不由作者定 |
| Evidence IDs | E-PR01–42 |

### 6.4 Decision Log

| Decision ID | 决策 | 备选 | 选择理由 | 放弃理由 | Evidence IDs | 可逆性 | 重开条件 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D-PR00 | 框定 F-C + F-D；F-B 只补仪器；F-A 排除 | F-A | 三条 E1 同向 | 无候选死于代码形状 | E-PR11/12/17/21 | 高 | 任一候选有过门证据却因 alpha 接口进不了实盘 |
| D-PR01 | O-PR2 + O-PR3 + O-PR5 分四个 Phase | O-PR4 | 每项对着一个读数、一天可验 | 重排增量 0 且已否决 | E-PR22 | 高（逐 PR revert） | Falsifier 任一成立 |
| D-PR02 | 「生产」三层定义：L-A demo 无人值守做完；L-B mainnet 小额校准准入设计；L-C mainnet 目标资金。**默认 L-A + L-B 设计** | 只 L-A；直接 L-C | 与 CLAUDE.md「规则将带进真实资金」一致，且不启用任何真钱动作 | L-C 要重问 H8 全部边界 | E-PR18 | 高 | Q1 |
| D-PR03 | `PLAN_BUDGET` 重定价：默认按今天 +5% 为新预算（non_alpha ≤ 34,550、live ≤ 16,080、alpha 占比只报趋势不设目标），理由写进同一 commit；缺口测试改守新数 | 保留缺口断言 + 写缩减目标 | 「暂时承担」24 天后需要一个能关闭的数；ratchet 的税才有上限 | 缩减目标要删代码，而抬顶记录显示增长多买的是事故暴露的仪器 | E-PR02、E-PR30 | 高 | Q5=B |
| D-PR04 | alpha 模块不重排、不重写；只补 WP-A1/A2 两件仪器（都不在 alpha 包内） | 重排 / 下沉研究逻辑 | C-PR01 | 下沉要抬 alpha 顶约 2,000 行并带证据，且 09-17 裁「先 M6 后下沉」的第二半没有新理由 | E-PR21/22/23 | 高 | D-PR00 的重开条件 |
| D-PR05 | 每个 WP 的验证协议：两条构造测试 + 三个 digest 前后逐字 + #135 协议（快照上产物逐字节）+ `live verify --check` 0 差异 + 四道门全量（不加 `-x`、不加第二个 `-q`）| 只跑四道门 | #55/#135 已建立的形态；09-16 的教训「真正的判据只有一个——把四道门全量跑一遍」 | — | E-PR22/23 | — | — |
| **[R 修订]** D-PR02 | 「生产」的定义改为 Q1 三选项，**默认 A「只 L-A」**（memory：production/mainnet explicitly out of scope for now）；`docs/PRODUCTION.md` 只在操作者答后写 | 冻结稿默认 L-A + L-B 设计 | E-PR42 | 冻结稿的理由「CLAUDE.md 那句话」不存在（KILL-07） | E-PR42 | 高 | Q1 |
| **[R 修订]** D-PR03 | 预算重定价改为 Q5（增长率裁定），无默认；`PLAN_BUDGET` 与缺口测试在操作者答前不动 | +5% | — | +5% 按近周速度 1–11 天用完（KILL-02(e)） | E-PR38 | 高 | Q5 |
| **[R 修订]** D-PR04 | alpha 不重排从「作者决定」改为 Q6（含「先 M6 后下沉」第二半的价钱与 GAP-PR06 剖析选项） | 作者决定 | — | C-PR01 → PARTIAL（KILL-03） | E-PR41 | 高 | Q6 |
| **[R 修订]** D-PR06 | 治理类改动（CLAUDE.md、`PLAN_BUDGET`、CEILING 政策、`guard.py`、凭据位置、重启、looks 计入口径）**不走 CI 绿即自动合并**，由操作者合并；未答不执行默认 | 默认 + 自动合并 | agent 不得替操作者改约束 agent 自己的规则 | — | KILL-10 | — | 操作者另裁 |
| **[R 修订]** D-PR07 | 丢 bar 的可干预路径改为传输层：操作者换代理节点（09-22 判读）；仓库侧先量 GAP-PR10 再定要不要预登记「窗口内重试」 | 重启命令化 | E-PR39：09-15 起 7 / 0 | WP-P2 要解的问题未发生 | E-PR39 | 高 | GAP-PR10 |

### Gate Review：G5 复核

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G5 | PASS | D-PR01–05；FM-PR1–8 | 不确定性高的两半（WP-P2 第二半、WP-P6 放行）都选了可逆、先量再做 | Weak GO | — | §7 冻结审查 |

---

## 7. Phase 7 · Adversarial Review

**Prerequisites**：冻结稿 §0–§6（677 行，sha256 `1a3e51c1…e863c`）。**Produces**：独立性声明、最强反方论点、Kill Register（17 条）与处置、G6、Final Kill Decision。**Downstream**：§0/§1/§2.2/§3.4/§3.5/§4/§6 的 [R 修订]、§8 Scope、§9 契约范围、§10.4 校准行。

审查稿全文（546 行）原样存档为 `docs/analysis/2026-09-28-production-refactor-adversarial-review.md`。本节只摘要与处置，不重述其论证；每条处置都指向审查稿里的编号。

### 7.1 独立性声明（摘自审查稿 §1）

| 项目 | 内容 |
| --- | --- |
| 冻结输入及版本 | `beidou-production-refactor-frozen.md`，677 行，mtime 2026-09-28T03:08:43+08:00 |
| 审查者与原作者关系 | **部分独立**：不同模型（Opus 5.5 对 Fable 5.1）、不同上下文、未读作者对话与草稿；同一操作者、同一仓库、同一台机器；系统注入的 memory 索引与作者共享（一处共同锚点）；派发 prompt 由作者写出并点名了几项检查，审查者只当清单不当结论 |
| 可访问证据范围 | 仓库全部文件（只读）；`.beidou/live/{state,heartbeat}.json` 与 `cycles.jsonl`（只取运维字段）；日报、周报、`docs/analysis/`、RESEARCH_LOG；`launchctl list`；`~/Library/Application Support/beidou/` 的目录列表（未打开文件）；V5 计划；跑了一次 `pytest -m "not network"`（1 failed / 2812 passed / 155.85s，跑前跑后 `git status` 相同） |
| 已知限制 | 文首 16 行已写结论，锚定不能完全消除；未跑 ruff/mypy；未逐项核 E-PR05、13、15、16、24–29、31–33；子代理原始报告不在盘上 |
| 不得执行的外部动作 | 不改冻结稿、不写仓库与 `.beidou/`、不跑 validate/mine/book/live run、不重启、不读凭据值、不建账号、不提替代方案——全部遵守 |

### 7.2 最强反方论点（审查稿 §2，预写于读 §4–§6 之前）

> 方案挑的是「可动的杠杆」而不是「挡住生产的约束」：按作者自己的证据，离真实资金最近的阻塞是 F-B，作者承认它「成立」却按推理把它移出主问题；推荐的 WP 是在一台没有经证实 edge 的系统上再加一层基础设施，正是 V5 C-006 的失败模式；而领先指标 M-PR01 定义在方案要改的那个文件上，所以方案可以在不让系统离生产更近一步的情况下宣告成功。

回看 §4–§6 后审查者加重了它：两条主臂的头号工作项都建在读错或过期的读数上（WP-P1 对 D-P4；WP-P2 对 09-15 起为 0 的重启丢 bar）。作者的处置：这条反方论点**成立**，本版据此把「四个 Phase 的工作包」改成「一批不改控制的卫生项 + 六个交操作者的问题」；M-PR01 按问题重写（非 alpha 增长率）。它没有推翻的是：五处小缺口复核成立（E-PR35、`venue: Any`、import 闭包、exits 同输入、BNX 本地红）——审查者 §3.3 明写不攻击它们。

### 7.3 复核记录（审查稿 §3）——数字全对，错在读法与引用

审查者抽核 20 处数字（R-01–R-16）**全部复现**，E-PR34 子代理的七项抽核也一致。读法错四处：触碰 ratchet 的构成（R-03）、2,154 s 的含义（R-06）、19.71% 的窗口（R-12）、凭据的位置（R-13）。引用错五处：E-PR21 引「无行为级 bug」漏残项（C-01）；E-PR22「已否决」引过头（C-02）；「规则将带进真实资金，CLAUDE.md」出处不存在（C-04）；「监控同宿是仓库没写过的」不成立（C-05）；「1.16%/次」是周期失败率不是重启（C-06）。审查者新增三项测量（R-17 增长率、R-18 V5 M-003 耦合量、R-19/20 丢 bar 来源），作者复算了 R-17、R-19/20 与 R-03 的口径（E-PR38、E-PR39），R-18 与 R-03b/c 未复算、按审查者测量引用（E-PR41）。与校准表「缺的不是数据是读法」同形——这是第七份同形记录。

### 7.4 Kill Register 与处置

处置状态只用附录 D.6 的词：CLOSED（新证据或方案/范围已变并可验证）、MITIGATED（风险降低但仍在）、ACCEPTED（具名 Owner 接受残余）。**没有一条以「已讨论」关闭。** 审查者未复审本修订版。

| Kill | 级 | 攻击命题（一句） | 处置 | 依据 / 落点 |
| --- | --- | --- | --- | --- |
| KILL-01 | P0 | WP-P1 让操作者新建只含 URL 的 `env.sh`，会让下一次重启以 78 退出且告警静默——本机没有 `env.sh`，凭据在 `~/.zshrc` | **CLOSED（撤方案 + 新证据）** | WP-P1 撤出 Scope，改为 Q3；E-PR37 核实文件不存在与变量名；§1 H8 理由、§5.3、§5.5 改写；K7 删「全部可 git 回滚」；RISK-PR10 新增；memory 已有「新建 env.sh 会停掉实盘循环」——作者读了 memory 仍写错，记进校准行 |
| KILL-02 | P0 | F-D 臂算术不成立：267/275 次触碰改的是 CEILING 值（M-PR01 数增长事件）；WP-C1 动不了它；WP-C2 靠放宽控制压低它且抬幅超自己的断言；D-PR03 的 +5% 几天用完 | **CLOSED（撤方案 + 改范围 + 新证据）** | WP-C1/C2/D-PR03 退出执行授权，改为 Q2a/Q2b/Q5；C-PR03 → PARTIAL；M-PR01 按问题重写为非 alpha 增长率（E-PR38）；F-D 改写为「增长率没有裁定」；GAP-PR08 登记「触碰有没有独立成本」 |
| KILL-03 | P1 | C-PR01 的 SUPPORTED/High 高于证据；F-A 由推理排除；Pre-PR1 是作者替操作者写的命题；「09-25 已否决」引过头；E-PR21 漏残项 | **MITIGATED** | C-PR01 → PARTIAL/Medium（耦合一半加 E-PR41 R-18，吞吐一半 UNKNOWN，GAP-PR06 保留）；D-PR00 的 F-A 改「未判」；D-PR04 改为 Q6，附 09-17 残项；E-PR22 措辞改「执行记录写明没做」；P1.2 的「近因」折减撤回。仍在：GAP-PR06 未跑 |
| KILL-04 | P1 | O-PR4 与作者 WP 不在同一把尺子上定价；「60%/30%」无出处 | **MITIGATED** | §4.2 加同尺子重定价（毛触碰行数 + D-PR05 + M-PR01–07）；两个比例标 E5；G3 → PARTIAL。仍在：GAP-PR06 的读数会改变 O-PR4 的价值 |
| KILL-05 | P1 | WP-P1 重开 09-06 具名 ACCEPTED 的 D-P4 却不引用；「仓库没写过」不成立；KILL-R2 的失败形态一条没回应 | **CLOSED（改范围 + 新证据）** | E-PR40 引 D-P4 原文与 RESEARCH_LOG:2764；GAP-PR01 关闭（不是 UNKNOWN，是已知且 ACCEPTED）；Q3 改写为重开问题并只提两条新事实（E-PR37：第二通道未配置；接受的文字是「告警 ≤ 2 bar」而宿主死亡时告警为 0）；§6.3「仓库没写过」删去并在偏差自检里点名 |
| KILL-06 | P1 | M-PR03 读反（2,154 s 是安全窗口内的重启）；WP-P2 要解的重启丢 bar 09-15 起为 0；真正丢 bar 的 7 次失败周期不在方案里；1.16% 是周期失败率 | **CLOSED（撤方案 + 新证据）** | E-PR39 复算 SKIPPED/ERROR 行；WP-P2 出 Scope；M-PR03 重写为「每月丢 bar 数按来源分」；§3.3 因果链、§4.5 改写；D-PR07：换代理节点（操作者）+ GAP-PR10 |
| KILL-07 | P1 | D-PR02 与 Q1 替操作者作答；memory 的「out of scope」与 V5 的「弱化风控」被省略；CLAUDE.md 出处不存在；Q1 缺「只 L-A」；09-05 已有 mainnet 解除条件 | **CLOSED（改范围 + 新证据）** | E-PR42；Q1 三选项、默认 A「只 L-A」；D-PR02 理由换成可核出处；WP-P6 随 Q1 且先引 09-05 解除条件；§4.1 补引被省略的两句，Strategic Fit → Medium |
| KILL-08 | P1 | WP-P6 把 `guard.py` 的结构性拒绝改成配置驱动 | **CLOSED（改范围）** | `guard.py` 一字不动；任何改它的 PR 列 HC-3 并排除自动合并（D-PR06） |
| KILL-09 | P1 | WP-A2 默认「不计入 N」与所引原文相反，把零 ledger 的看做成更便宜的常设通道 | **CLOSED（改范围）** | 默认改为按 09-27 原文「计入 N」；在操作者定计入口径（HC-6）之前不落地；#199 新合入的 612 行 look 脚本记为需求真实的证据（E-PR36） |
| KILL-10 | P1 | 默认值 + CI 绿自动合并 = agent 替操作者改治理规则；Q2 两个分支都执行 WP-C2 | **CLOSED（改范围）** | D-PR06：治理类改动不走自动合并、未答不执行默认；Q2 拆成 Q2a/Q2b；D-PR03 写明动的是哪个占比这一点随 Q5 一起交操作者 |
| KILL-11 | P1 | G4 机会成本两处用法矛盾；最稀缺资源（操作者注意力）没定价；19.71% 是 23 小时的读数 | **MITIGATED** | §4.3 按注意力（问题数、PR 数、日报新增节数）重定价；E-PR41 注明窗口并给整周读数；G4 → PARTIAL。仍在：注意力的单位没有阈值 |
| KILL-12 | P1 | Weak GO 被当成执行许可；Phase 2–3 的 Adequate 无支撑（H5）；「零重启」与 WP-C6 矛盾 | **CLOSED（改范围）** | 决定词改 PIVOT；只有六项卫生项按「受控小范围」开工；Phase 2–3 记 Unproven；WP-C6 生效方式写成「搭下一次按纪律的重启」 |
| KILL-13 | P1 | L 级首轮不暂停、取证超预算、Gate 带未通过项写 PASS——与 09-26 校准行同形 | **ACCEPTED（流程缺口，记录）** | 无法回溯；§1 的 G2/G4 改 PARTIAL；§10.4 校准行照实记「重复」；Owner 作者 |
| KILL-14 | P2 | 冻结稿在冻结那一刻已过期（`allow` 行早 38 分钟；`origin/main` +4 PR；§5.2 两处 launchctl 读数矛盾） | **CLOSED（新证据）** | E-PR36、E-PR40；GAP-PR04 关闭；Q4 撤；§5.2 读数统一为主会话 12:03 的 `launchctl list`；本文分支已合入 `37e56ff7` |
| KILL-15 | P2 | WP-C3 的受损方补偿不存在：钉 commit 钉不住 `.beidou/data` | **CLOSED（撤方案）** | WP-C3 → Won't；§3.7 受损方第一行的补偿标「撤回」 |
| KILL-16 | P2 | 引用完整性八处（GAP-PR09 未定义、E-PR34 无位置、K5/K8 引用未写出的节、证据计数 34/35、CEILING 行号、Claim/Risk 缺 Owner、A-PR01/03 象限、A-PR04 旗标） | **CLOSED（逐条补齐）** | GAP-PR09 已定义；E-PR34 位置改为 §5.2 命令；K5/K8 的依据现在在 §8/§9；证据计数改「E-PR01–42」；CEILING 行号按当前文件（`:1954`，定稿时 `:2019` 附近，以 `grep -n 'CEILING = {'` 为准）；Claim/Risk 的 Owner 统一声明为操作者（§2.1 首段）；A-PR01/03 象限以 §2.3 汇总行为准；A-PR04 随 WP-P2 出 Scope |
| KILL-17 | P1 | Success Definition 部分反推（F-D 的方案方向写在 §3.4 表里）；18 个工作项 12 个无指标 | **MITIGATED** | §3.5 重写并加 M-PR07；工作项到指标的对照表补上；§6.3 胜出理由改写；G1 → PARTIAL。仍在：WP-P3 没有独立指标，只有验收 |

汇总：CLOSED 11、MITIGATED 4、ACCEPTED 1（KILL-13）、另 1 条（KILL-16）按逐条补齐记 CLOSED——OPEN P0 = 0，OPEN P1 = 0。

### 7.5 Pre-Mortem / Inversion（审查稿 §5）的采纳

审查者的七条 Pre-Mortem 里，PMF-1（ratchet 税降了、增长没停）、PMF-2（dead-man 上线后循环在下一次重启停下且告警静默）、PMF-3（Falsifier 按构造触发）、PMF-4（scratchpad 归档的补偿不存在）、PMF-6（命令化的 look 抬高窥视次数）、PMF-7（治理默认值被自动合并）六条各自对应的方案项已撤或改为问题；PMF-5（操作者读成「又一次等」）仍是本版的最大风险（RISK-PR02），补偿是 Q6 把 alpha 的问题交回而不是替他答。Inversion 表七种失败做法「六种当前存在」——本版逐条对照：指标定义在方案文件上（已改）、把已接受的风险写成新缺口（已改）、部署步骤依赖没核的环境事实（已改并核）、用降摩擦修「摩擦本身是控制」（三处改为问题）、默认值配自动合并（D-PR06）、冻结在旧状态上（已 fetch 并合入）、首轮不暂停（无法回溯，ACCEPTED 进校准行）。

### 7.6 人类确认点（审查稿 §7，全部采纳）

| ID | 动作 | 确认人 | 本版落点 |
| --- | --- | --- | --- |
| HC-1 | 新建或改写 `env.sh`；移动 Binance key | 操作者 | Q3；RISK-PR10；CLAUDE.md「凭据只有一个位置」那句待操作者改成可核事实 |
| HC-2 | 建第三方 dead-man 账号、存放 ping URL | 操作者 | Q3（重开 D-P4 之后才有这一步） |
| HC-3 | 任何改动 `guard.py` | 操作者 | WP-P6 不动它；D-PR06 |
| HC-4 | 生产重启（含 WP-C6 搭车生效） | 操作者 | WP-C6 的 PR 描述写「搭下一次按纪律的重启」，不自行重启 |
| HC-5 | 改 CLAUDE.md、`PLAN_BUDGET`、CEILING 政策、一次性抬顶 | 操作者 | Q2a/Q2b/Q5；D-PR06 |
| HC-6 | looks 是否、何时计入 N | 操作者 | WP-A2 等裁定 |
| HC-7 | 是否重开 09-06 的 D-P4 | 操作者 | Q3 |
| HC-8 | BNX 夹具与归档哪边对 | 操作者 | GAP-PR09；WP-P4 只给它执行位置，不改任一侧 |
| HC-9 | 「生产」的定义 | 操作者 | Q1 |

### Gate Review：G6

| Gate | 状态 | Evidence/Claim IDs | 未通过项 | 决策上限 | Owner | 下一动作 |
| --- | --- | --- | --- | --- | --- | --- |
| G6 | **FAIL → 已处置（审查者未复审）** | 17 条 Kill；E-PR36–42 | 审查时 P0 ×2 OPEN；处置后 OPEN P0/P1 = 0，MITIGATED 4、ACCEPTED 1；Falsifier 四条按审查 §8 重写 | **PIVOT**（H2 已处理；H3、H5 仍命中；H7 因未复审保守计入） | 作者 / 操作者 | 下一份分析对照 §10.4 校准行 |

### Final Kill Decision（审查稿 §9 原判 + 作者处置）

| 问题 | 审查者结论 | 作者处置后 |
| --- | --- | --- |
| 最强反方论点 | 方案可以一边让系统离生产更远，一边达成它自己的全部成功指标 | 成立；方案改形（§6.3 [R 修订]） |
| 未关闭 P0 / P1 | P0 ×2、P1 ×12、P2 ×3，全部 OPEN | OPEN P0/P1 = 0；MITIGATED 4（KILL-03/04/11/17）；ACCEPTED 1（KILL-13） |
| 被推翻或 UNKNOWN 的 P0 Claim | C-PR01 → PARTIAL；C-PR02 仍 PARTIAL 但理由换了 | 同意，已改（§2.2） |
| 需要的新 Evidence | 冲突次数（GAP-PR08）；GAP-PR06 剖析；凭据来源核对；Q1 的回答；D-P4 与 looks 的裁定；四个 PR 的 diff 重读 | 凭据来源已核（E-PR37）；四个 PR 已读并合入（E-PR36）；其余进 Gap Plan 与 Q 表 |
| 必须改变的 Option / Scope / Contract | WP-P1 撤；F-D 臂退出授权；M-PR01/03 与 Falsifier 重写；D-PR04 交操作者；Q1 加「只 L-A」；WP-P6 不动 `guard.py`；WP-A2 等裁定；治理类 PR 人工合并；§4 同尺子重定价；Phase 2–3 记 Unproven | **全部照做**（§6.1 处置表） |
| 最终建议 | **PIVOT**（H2；同时 H1、H7、H3、H4、H5；H8 未命中） | **PIVOT**。问题成立、方案改形；H2 处理后仍命中 H3（A-PR01、C-PR01 吞吐一半 UNKNOWN）与 H5（卫生项以外 Unproven）；H1 处置后为 0；H7 因审查者未复审保守计入 |

---

## 8. Phase 8 · Scope & Priority

**Prerequisites**：G1–G6 按 §7 处置。**Produces**：MoSCoW、Scope Firewall、Decision Compression。**Downstream**：§9 只为 In Scope 项写契约。

### 8.1 MoSCoW 与 In/Out Scope

| 分类 | 内容 | 理由 | IDs |
| --- | --- | --- | --- |
| **Must**（卫生批，零裁定，本周） | WP-P4 归档专属测试归位；WP-C6 armed 进程 import 闭包；WP-C7 接线点类型；WP-C8 exits 同输入测试；WP-C9 配置键读者测试；WP-P3 日期开关登记表 | 审查未攻击、前提复核成立；不改任何一道控制；零构造、零 ledger、零 Policy；每项一个 PR、可 revert | C-PR02、C-PR04、E-PR35 |
| **Must**（交操作者，未答不执行） | Q1 生产定义（默认 A「只 L-A」）；Q2a ratchet 记录搬迁；Q2b headroom 政策；Q3 是否重开 D-P4；Q5 非 alpha 增长率裁定；Q6 「尤其是 alpha」要的是哪一件 | 每个都是「放松或改写一道控制」，只能由控制的所有者裁 | D-PR02/03/04/06 |
| **Must**（操作者动作） | 换代理节点（09-22 判读的唯一便宜解） | 09-15 起丢的 7 根 bar 全来自传输层 | D-PR07、E-PR39 |
| **Should**（零 ledger 测量，下一会话） | GAP-PR06 validate 每格耗时剖析；GAP-PR08 ratchet 文件的冲突成本；GAP-PR10 失败周期窗口内重试的收益 | 三条各自决定一个 Q 或一条预登记要不要开 | §2.5 |
| **Should**（等卫生批之后） | WP-A1 数据族 parity 仪器（抬 live 顶并写理由） | 对 M-PR06；审查未攻击 | C-PR06 |
| **Could** | WP-P5 后半（周报排 job）；WP-C4 配置叙事外移；WP-P6 文档（仅 Q1=B 且不动 `guard.py`）；WP-A2（仅 HC-6 裁定后，默认计入 N） | 价值取决于裁定 | — |
| **Won't**（本方案） | WP-P1（改为 Q3）；WP-P2（问题未发生）；WP-C1/C2/D-PR03 作为执行项（改为 Q）；WP-C3（补偿不存在）；按 7 步流水线重排包；重写 `beidou_alpha`（下沉随 Q6）；拆 `engine.py`；扩挖掘语法；接通 ensemble；动任何 `Policy` 阈值；挪日期常量；改 `guard.py`；启用 mainnet；场地侧保护单（D-012）；换存储；加主机；删测试 | 见 §8.2 | D-PR04、E-PR22 |

In Scope 的共同边界：不动 `beidou_alpha` 一行；不写 `trials.jsonl`；不改任何被三个 digest 哈希的值；不新建 `env.sh`；不重启（WP-C6 搭操作者的下一次按纪律重启）。

### 8.2 Scope Firewall

| Out-of-Scope 项 | 原因 | 未来允许条件 | 加入后挤出项 | IDs |
| --- | --- | --- | --- | --- |
| 宿主外 dead-man（原 WP-P1） | 09-06 D-P4 具名 ACCEPTED；规格建在错的凭据位置上 | 操作者答 Q3=重开；规格按 09-05 KILL-R2 逐条回应；变量放凭据实际位置 | 一次操作者注意力 | KILL-01/05 |
| bar 安全重启命令（原 WP-P2） | 09-15 起 0 根 bar 因重启丢失；命令化会扩大「谁能重启」 | 再出现一次因时机丢 bar 的重启（M-PR03 重启列 > 0） | — | KILL-06 |
| ratchet 记录搬迁 / headroom 政策 / 预算重定价 | 三者都是放松或改写控制 | Q2a / Q2b / Q5 的回答 | — | KILL-02/10 |
| scratchpad 归档 | 补偿不存在（数据回不去） | 有人给出「代码 + 数据」都能钉住的复现配方 | — | KILL-15 |
| 重排包 / 重写 alpha / 下沉研究逻辑 | 同尺子定价后对 M-PR01–07 增量 0；F-A 未判 | Q6=A，或 GAP-PR06 显示瓶颈在 alpha 纯函数 | Q6 的另一份方案 | KILL-03/04 |
| 拆 `engine.py` | 09-13「不建议为了行数而拆」；GAP-PR07 未见自然边界 | 有人拿出 ≥2 个只依赖 `state`+`store` 的阶段簇 | — | 09-13 `:562-564` |
| 扩挖掘语法 / 接通 ensemble | 研究决定，不是重构 | 预登记 + R2 新搜索空间 / 主书第二条过门策略 | — | E-PR10 |
| 动 `Policy` / 日期常量 / `guard.py` | R10；挪日期 = 延长 bridge 类裁定；`guard.py` 是 mainnet 的结构边界 | 操作者裁定 + `POLICY_VERSION`；HC-3 | — | KILL-08 |
| 启用 mainnet / 场地侧保护单 / 换存储 / 加主机 | memory：out of scope for now；D-012；单人 demo 阶段 | Q1=C 的另一份分析 | — | E-PR42 |
| 删测试 | 60k 行不是问题，本地/CI 分歧才是 | 有测试被证明不测任何东西 | — | 09-16 |

### 8.3 Decision Compression

| 字段 | 冻结内容 |
| --- | --- |
| 一句话问题 | 「生产」没有定义、没有读数；非 alpha 的增长率没有裁定；传输层每两天丢一根 bar 的退出检查没人量；本地专属测试红了三天 CI 看不见——而 alpha 模块不在这几件事里 |
| 核心用户与场景 | 单一操作者，每天读日报、裁 PR |
| 推荐方案及相对优势 | 六项不改控制的卫生项直接开工；六个改控制的问题带价交操作者；一项操作者动作（换代理节点）；三项零 ledger 测量。相对「四个 Phase 的工作包」：不替操作者改任何一道规则，不建在过期或读错的读数上 |
| In Scope / Out of Scope | §8.1 / §8.2 |
| 关键 Claim / Kill / Risk | C-PR01 PARTIAL、C-PR02 PARTIAL、C-PR03 PARTIAL、C-PR04/05/06 SUPPORTED；KILL-01/02 CLOSED by 撤方案；RISK-PR02（又一次「等」）、RISK-PR10（不含凭据的 `env.sh`） |
| 成功 Metric | M-PR01–07（§3.5 修订版、§10.1） |

### Gate Review：G3–G6 复核

| Gate | 状态 | 说明 |
| --- | --- | --- |
| G3 | PARTIAL | 卫生项 Strong；其余 Unproven（H5） |
| G4 | PARTIAL | 按注意力定价；Strategic Fit Medium |
| G5 | PARTIAL | WP-P1/C2/D-PR03 不进契约 |
| G6 | 已处置（未复审） | §7 |

---

## 9. Phase 9 · Delivery Contract

**Prerequisites**：§8 Scope；§7。**Produces**：PRD / Development / Test / Acceptance Contract（限卫生批 + 问题）、Source Trace Matrix、G7。**Downstream**：执行会话按本节开 PR；操作者按 AC 验收、按 HC 表答问题。

### 9.1 交付物是什么

两类：**要建的东西**（六项卫生项，四类 Contract 全写）与**分析本身**（六个问题、一项操作者动作、三项测量——按分析型交付规则只写 Test / Acceptance，对象是每条「下一动作」与交人的每个问题）。本文自身不执行任何一项。

### 9.2 PRD Contract

| PRD 区块 | 来自 | 本文位置 |
| --- | --- | --- |
| 背景与目标 | Problem + Evidence | §3.1（含 [R 修订]）、§3.5 修订表；非目标 = §8.2 |
| 需求范围 | Scope Firewall + Decision | §8.1、§8.2；重开条件 §8.2、§6.4 |
| 用户故事 | Scenario/JTBD | §3.6；S-PR3（归档测试）、S-PR4（日期表）；S-PR1/S-PR2/S-PR5 随 WP-P1/P2/P6 出 Scope |
| 规则与流程 | Journey + Decision | §6.1 处置表；D-PR05 验证协议；D-PR06 人工合并规则 |
| 异常与边界 | Edge/Failure/Risk | EC-PR3（归档变动）、EC-PR5（日期新写法）；FM-PR3/4/5；RISK-PR01/09/10 |
| 成功指标 | Success Definition + Validation Plan | §3.5 修订表、§10.1 |
| 风险依赖 | Impact/Constraint/Kill/Risk | §5.3、§5.4、§5.6、§7.4；受损方与补偿 §3.7（WP-C3 补偿撤回） |
| 待决事项 | Claim/Assumption/Gate | Q1、Q2a、Q2b、Q3、Q5、Q6（§P1.6 修订表）；GAP-PR06/08/09/10 |
| 来源追踪 | Source Trace | §9.6 |

### 9.3 Development Contract

责任模型（C.1）：Business/Product Owner = 操作者；Engineering Owner = 执行 WP 的 agent 会话；Test/Quality Owner = 同一会话 + CI；Approval Owner = 操作者（六个问题、HC-1–9、装载 job）；Runtime Owner = 操作者；Learning Owner = 操作者（读日报）。Agent 不能替代 Approval Owner——**这是 D-PR06 的全部内容**。

共同条款（六项卫生项）：不改 `beidou_alpha`；不改 `Policy`；不改任何被三个 digest 哈希的值；不写 ledger；不写 `.beidou/`；不新建 `env.sh`；不重启。新文件只有测试与 `beidou_governance/calendar.py`；全部 PUBLIC。每项一个 PR，走 CLAUDE.md 流程（四道门全量 → PR → CI → auto-merge 允许，因为它们不改控制）；PR 描述写「何时生效」；回滚 `git revert`；验收 §9.4 的 T-ID 全绿 + §9.5 的 AC-ID。

记录式，只写各项特有条款：

**WP-P4** · 边界：7 条读 `.beidou/` 的测试（`tests/live/test_bar_sanity_is_seen_and_never_traded_on.py`、`tests/data/test_dataset_gate.py`、`tests/live/test_a_gap_is_asked_for_once_and_never_filled_in.py`、`tests/live/test_the_market_benchmark_cannot_be_built_with_hindsight.py`、`tests/live/test_a_restart_does_not_take_over_a_bar_a_failed_cycle_lost.py`、`tests/governance/test_the_record_reaches_the_state_exactly_once.py`、冻结测试——执行前按 `grep -rn '\.beidou' tests/` 再核一遍）加 `@pytest.mark.archive`；`pyproject.toml` markers +1；`deploy/run_data.sh` 在夜间同步后跑 `.venv/bin/python -m pytest -m archive`，失败走 `run_check.sh` 同一条 `notify`（读 `~/.zshrc` 的 webhook 变量，E-PR37）；`beidou_live/report_data.py` +20 印「归档专属测试：通过 / 失败 / 未跑」· 默认门 `-m "not network"` 不变（marker 叠加不替代）· ratchet：live 用 headroom（40）· 生效：主 checkout 快进后的下一个夜间 job（操作者）· **不改夹具、不改归档**（HC-8）。

**WP-C6** · 边界：`collateral_share` 从 `beidou_live/reports.py` 搬到 `report_common.py`（逐字）；`engine.py:49` 改 import；`tests/live/test_the_armed_process_does_not_import_the_report_layer.py`（新，+30）· 生效：**搭下一次按纪律的重启**（HC-4），PR 描述与 RUNBOOK 写明；两条构造测试必绿 · 不改构造。

**WP-C7** · 边界：`beidou_cli/live_cmd.py:326` `venue: Any` → `Venue`（±2 行）· mypy 门即验收；若 mypy 红，那是一条真发现（Protocol 未被满足），另开 PR 修实现，不改回 `Any`。

**WP-C8** · 边界：`tests/live/test_the_live_overlay_is_the_backtest_overlay_on_one_symbol.py`（新，+80）：同一合成 Panel、同一 `ExitParams`、同一初始 `exit_states` 下，`ExitOverlay.apply` 逐 symbol 与 `apply_exits` 帧的权重按 `_bit_for_bit` 相同 · 零源码改动。

**WP-C9** · 边界：`beidou_live/config.py` 加 `REPORT_ONLY_KEYS`（今天 14 个 `risk_budget` 键，+15）；`tests/live/test_every_profile_key_has_a_reader.py`（新，+60）：`live.demo.yaml` 的 61 个二级键要么被 `LiveConfig`/引擎路径读，要么在表里 · ratchet：live 用 headroom · 不改任何值。

**WP-P3** · 边界：`beidou_governance/calendar.py`（新，+120）、`beidou_cli/governance_cmd.py`（+40）、`beidou_live/report_governance.py`（+25）、`tests/governance/test_every_dated_switch_is_on_the_calendar.py`（新，+80）· 输入（按 `37e56ff7` 重算）：`deploy/run_live.sh` `BRIDGE_UNTIL`（标「#163 后惰性」）、`policy.py` `SINGLE_WINDOW_MINE_OPENING_ENDS`、`governance/reopen.yaml` 的 `date_after`（两条 10-13）、`governance/window_changes.yaml` `earliest_window`、registry `probe.accepted_on + review_after_days`、冻结测试 `FREEZE_ENDS`（已到期）· API：`beidou governance calendar [--days 60] [--json]`；日报印未来 7 天 · ratchet：governance +120（headroom 40 → 抬并写理由）、cli +40（headroom 26 → 抬并写理由）· 不写任何文件。

分析型交付（问题、动作、测量）的 Development Contract：N/A——它们由操作者或下一会话执行，本文不建东西；各自的 Test / Acceptance 在 §9.4/§9.5 末。

### 9.4 Test Contract

| Test ID | Delivery | 类型 | 前置 | 操作 | 预期 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| T-PR4a | WP-P4 | 单元 | 无归档的环境 | `pytest -m archive` | 全部 skipped，理由「the archive is not in this checkout」 | CI 日志 |
| T-PR4b | WP-P4 | 集成（本机） | 有归档 | `pytest -m archive` | 7 条各有确定结果；BNX 那条按当前归档状态红或绿 | 夜间 job 日志 |
| T-PR4c | WP-P4 | 契约 | — | 默认门选中用例数 | 不因 marker 减少 | pytest 汇总行 |
| T-PR-C6a | WP-C6 | 单元 | 干净解释器 | `import beidou_live.engine` | `sys.modules` 无 `beidou_live.report_*`；两条构造测试绿 | pytest |
| T-PR-C7a | WP-C7 | 类型 | — | `mypy` | 通过；红即真发现 | mypy |
| T-PR-C8a | WP-C8 | 单元 | 合成 Panel | 两条路径 | 权重 `_bit_for_bit` 相同 | pytest |
| T-PR-C9a | WP-C9 | 架构 | — | 在 `live.demo.yaml` 加一个无读者的键 | 红；加进 `REPORT_ONLY_KEYS` 后绿 | pytest |
| T-PR3a | WP-P3 | 单元（钉时钟 2026-10-06） | — | `governance calendar --days 30` | 列出 10-13 的 `BRIDGE_UNTIL`（惰性）与两条 `date_after` | 输出 |
| T-PR3b | WP-P3 | 架构 | — | 在 `deploy/`、`beidou_governance/`、`governance/*.yaml`、冻结测试注入未登记的 `2026-11-01` | 红，指出文件与行 | pytest |
| T-PR-D5 | 全部 | 协议 | 每个 PR | D-PR05 五项 | 两条构造测试绿；三 digest 逐字；#135 快照产物逐字节；`live verify --check` 0；四道门全量 | PR 描述 |

分析型交付的 Test 项：

| Test ID | 对象 | 前置 | 操作 | 客观预期 |
| --- | --- | --- | --- | --- |
| T-PR-Q1 | Q1 | 本文 | 操作者选 A/B/C | 答案写进 `docs/PRODUCTION.md`（此前不建文件）；B 时 WP-P6 文档先引 09-05 解除条件，`guard.py` 的 diff 为空 |
| T-PR-Q2 | Q2a/Q2b | 本文 | 答 | A 才开 PR，且由操作者合并；PR 的 CEILING 变更与断言一致（KILL-02(d)） |
| T-PR-Q3 | Q3 | 本文 | 答 | 重开 → 规格逐条对照 09-05 KILL-R2 的条目；变量在凭据实际位置；`test -f "$HOME/Library/Application Support/beidou/env.sh"` 在部署前后同值 |
| T-PR-Q5 | Q5 | 本文 | 答 | A → `PLAN_BUDGET` 改「只记录」，理由同 commit，操作者合并；B → 另一份方案 |
| T-PR-Q6 | Q6 | 本文 | 答 | A → 另一份带价方案；B → GAP-PR06 剖析先跑；C → D-PR04 成立并记 RESEARCH_LOG |
| T-PR-G06 | GAP-PR06 | 一次 `research backtest`（零 ledger） | `cProfile` | 前十函数与各占比；`trials.jsonl` sha256 不变 |
| T-PR-G08 | GAP-PR08 | git 历史 | 统计 | ratchet 文件上可归因的冲突/延时次数每周 |
| T-PR-G10 | GAP-PR10 | `cycles.jsonl` 7 条 ERROR + `proxy-probe.jsonl` | 只读估算 | 「X 秒后重试一次」可救回的比例；不写 `.beidou/` |
| T-PR-OP | 换代理节点 | 操作者 | 换 | 30 天后 M-PR03「失败周期」列对比 09-15 起的 7/13 天 |

### 9.5 Acceptance Contract

| Acceptance ID | Delivery | 层级 | 前置 | 操作/观察 | 客观预期 | 证据 | Owner | 失败动作 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AC-PR4a | WP-P4 | Functional | 合入 + 主 checkout 快进 | 次日日报 | 「归档专属测试」一节三态之一 | 日报 | 操作者 | 查 job 装载 |
| AC-PR4b | WP-P4 | Scenario（S-PR3） | 归档变动 | 24h 内 | 告警写明哪条测试、切片行数差；不改夹具、不改归档 | 告警 | 操作者 | 手跑 |
| AC-PR-C6a | WP-C6 | Functional | 搭一次按纪律的重启 | 心跳 | `construction` 与 `registry` 前后逐字相同；`import` 测试绿 | heartbeat | 操作者 | revert |
| AC-PR-C7a | WP-C7 | Functional | 合入 | mypy 门 | 绿 | CI | 会话 | 修实现 |
| AC-PR-C8a | WP-C8 | Functional | 合入 | 测试 | 绿 | CI | 会话 | 修实现或记录差异 |
| AC-PR-C9a | WP-C9 | Functional | 合入 | 测试 | 绿；表里恰好 14 个 `risk_budget` 键 | CI | 会话 | — |
| AC-PR3a | WP-P3 | Functional | 合入 | `governance calendar` | 今天列出 10-13 的三处翻转与读者 | 输出 | 会话 | 修 |
| AC-PR3b | WP-P3 | Scenario（S-PR4） | 日报快进后 | 10-06 起 | 日报「未来 7 天日期翻转」一节出现 10-13 | 日报 | 操作者 | — |
| AC-PR-H | 卫生批 | Hypothesis（C-PR04） | 7 天 | M-PR02 | 0 | 日报 | 操作者 | Falsifier ① |

分析型交付的 Acceptance 项：

| Acceptance ID | 问题/动作 | 前置 | 操作 | 客观预期 | 失败动作 |
| --- | --- | --- | --- | --- | --- |
| AC-PR-Q1 | Q1 | — | 操作者答 | 三选项之一写进 `docs/PRODUCTION.md` 与 §8.1 | 无默认执行 |
| AC-PR-Q2 | Q2a/Q2b | — | 答 | 治理类 PR 由操作者合并 | 不开 PR |
| AC-PR-Q3 | Q3 | — | 答 | 重开 → 新规格（另一次审查）；不重开 → RESEARCH_LOG 记「2026-09-28 复核，维持 ACCEPTED」 | 无默认执行 |
| AC-PR-Q5 | Q5 | E-PR38 | 答 | A → 一次 commit；B → 另一份方案 | 无默认执行 |
| AC-PR-Q6 | Q6 | — | 答 | A/B/C 各自的下一步落到 RESEARCH_LOG | 无默认执行 |
| AC-PR-OP | 换代理节点 | 操作者 | 换 | M-PR03「失败周期」列 30 天 < 7/13 天的比率 | 09-22 判读错了 → 重开代理路径分析 |
| AC-PR-G | GAP-PR06/08/10 | 下一会话 | 跑 | 读数进 RESEARCH_LOG；ledger sha256 不变 | — |

### 9.6 Source Trace Matrix

| Delivery ID | Problem/Scenario | Evidence | Claim/Assumption | Decision | Gate/Kill | Scope | Test/Acceptance | Metric |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DL-PR-P4 | §3.1、S-PR3 | E-PR08、子代理 C（7 条） | C-PR04、A-PR06 | D-PR01 | 审查未攻击 | Must | T-PR4a–c、AC-PR4a/b、AC-PR-H | M-PR02 |
| DL-PR-C6 | §5.2 import 闭包 | E-PR34（A §1）、`engine.py:49` | C-PR02 | D-PR01 | KILL-12(c) 处置 | Must | T-PR-C6a、AC-PR-C6a | M-PR04 |
| DL-PR-C7 | §5.2 端口 | E-PR34（A §8） | C-PR02 | D-PR01 | 审查未攻击 | Must | T-PR-C7a、AC-PR-C7a | M-PR04 |
| DL-PR-C8 | §5.2 重复语义 | E-PR34（A §3） | C-PR02 | D-PR01 | 审查未攻击 | Must | T-PR-C8a、AC-PR-C8a | M-PR04 |
| DL-PR-C9 | §5.2 配置面 | E-PR35 | C-PR02 | D-PR01 | 审查 §3.3 复核成立 | Must | T-PR-C9a、AC-PR-C9a | M-PR04 |
| DL-PR-P3 | S-PR4 | E-PR16、E-PR36 | C-PR02 | D-PR01 | KILL-14/17 处置 | Must | T-PR3a/b、AC-PR3a/b | （验收即指标） |
| DL-PR-Q1 | Pre-PR2 | E-PR42 | A-PR01 | D-PR02 | KILL-07 | Must（问） | T-PR-Q1、AC-PR-Q1 | — |
| DL-PR-Q2 | §3.3 | E-PR38、E-PR41 | C-PR03、A-PR02 | D-PR06 | KILL-02/10 | Must（问） | T-PR-Q2、AC-PR-Q2 | M-PR01 |
| DL-PR-Q3 | S-PR1 | E-PR37、E-PR40 | C-PR02 | D-P4（09-06） | KILL-01/05 | Must（问） | T-PR-Q3、AC-PR-Q3 | M-PR07 |
| DL-PR-Q5 | §3.1 | E-PR02、E-PR38 | C-PR05 | D-PR03 | KILL-02(e) | Must（问） | T-PR-Q5、AC-PR-Q5 | M-PR01 |
| DL-PR-Q6 | Pre-PR1 | E-PR21、E-PR41 | C-PR01 | D-PR04 | KILL-03 | Must（问） | T-PR-Q6、AC-PR-Q6 | — |
| DL-PR-OP | §3.3 传输层 | E-PR39、memory 09-22 | C-PR02 | D-PR07 | KILL-06 | Must（操作者动作） | T-PR-OP、AC-PR-OP | M-PR03 |
| DL-PR-G | §2.5 | — | GAP-PR06/08/10 | — | KILL-03/02/06 | Should | T-PR-G06/08/10、AC-PR-G | — |
| DL-PR-A1 | §3.4 F-B | E-PR12、E-PR28 | C-PR06 | D-PR01 | 审查未攻击 | Should | （契约随其 PR 写） | M-PR06 |

### Gate Review：G7

| Gate | 状态 | 未通过项 | 决策上限 |
| --- | --- | --- | --- |
| G7 | PASS（限卫生批与问题） | 每个 Must 有 Source Trace、Test、Acceptance、Owner、回滚、生效时点、人类确认点；WP-P1 与 F-D 臂按审查意见不写契约 | PIVOT |

---

## 10. Phase 10 · Learning Loop

**Prerequisites**：§3.5 修订表、§9。**Produces**：Validation Plan、Experiment Design、Post-Launch Review（空表）、校准行。**Downstream**：`docs/analysis/analysis-calibration.md`。

### 10.1 Validation Plan

| Metric ID | Claim | 指标 | 基线 | 成功阈值 | 护栏 | 窗口 | 停止条件 | 失败动作 | Owner |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M-PR01 | C-PR05 | 非 alpha 行/天；整周 alpha 投入占比 | 09-21→28：约 400–1,400 行/天；32.5% / 36.2% / 21.5% | Q5 答 A：只记录；答 B：按操作者给的上限 | 记录不设阈值即不告警 | 每周 | — | 回到 Q5 | 操作者 |
| M-PR02 | C-PR04 | 只在本机跑且与 CI 不同的测试数 | 7 条里红 1 | 0 且 7 条每夜有结果 | 默认门用例数不减 | WP-P4 合入后 7 天 | job 连续 3 天未跑 | 装载步骤 | 操作者 |
| M-PR03 | C-PR02 | 每月丢 bar：失败周期 / 重启时机 | 09-15 起 7 / 0（13 天） | 换节点后 30 天失败周期列 < 7/13 天的比率；重启列保持 0 | — | 30 天 | 重启列 > 0 | 重开 WP-P2 的问题 | 操作者 |
| M-PR04 | C-PR01 | 三 digest；`live verify --check` | `2ee491c13971` / `7f8adb754962` / 0.3.6 | 逐字相同；0 | — | 每个 PR | 任一不同 | revert | 会话 |
| M-PR05 | C-PR01 | `trials.jsonl` 行数与 sha256 | 22,205 | 不变 | — | 每个 PR | 变了 | revert | 会话 |
| M-PR06 | C-PR06 | parity 读数存在 | 无 | WP-A1 后日报每族一行 | ledger 不变 | 14 天 | 缺席 | 修 | 操作者 |
| M-PR07 | C-PR02 | 宿主离线到知道的最长延迟 | 无上限 | Q3 不重开：记录为 ACCEPTED；重开：演练 ≤ 2 个整点 | — | Q3 后 | — | — | 操作者 |

### 10.2 Experiment Design

| Experiment ID | 要证伪的 Claim | 最小方法 | 对象/样本 | 变量/对照 | 指标 | 通过阈值 | 停止条件 | 局限 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EXP-PR1（=GAP-PR06） | C-PR01 吞吐一半 | `cProfile` 一次 `research backtest` | pit 面板 | — | 前十函数占比 | ≥ 50% 在 `beidou_alpha` → F-A 重开 | 写任何报告到 tracked 目录即停（用 `--out` 指向 scratchpad） | 一次运行 |
| EXP-PR2（=GAP-PR08） | C-PR03「触碰有独立成本」 | git 历史统计 | 30 天 | — | 冲突/延时次数每周 | ≥ 1 → Q2a 值得 | — | 归因靠人读 PR 评论 |
| EXP-PR3（=GAP-PR10） | C-PR02「传输层可救」 | 7 条 ERROR + 探针数据估算 | 09-15 起 | — | 可救比例 | ≥ 4/7 → 预登记窗口内重试 | 不写 `.beidou/` | 探针数据停在 09-22 |
| EXP-PR4 | C-PR04 普遍性 | 夜间 `pytest -m archive` 7 天 | 7 条 | — | 红的条数与原因 | 全部有结果 | — | 归档每天变 |

### 10.3 Post-Launch Review（执行后回填）

| 原 Claim | 实际结果/Evidence ID | 支持/推翻 | 偏差原因 | Decision/Scope 更新 | Pattern |
| --- | --- | --- | --- | --- | --- |
| C-PR01 | — | — | — | — | — |
| C-PR02 | — | — | — | — | — |
| C-PR03 | — | — | — | — | — |
| C-PR04 | — | — | — | — | — |
| C-PR05 | — | — | — | — | — |
| C-PR06 | — | — | — | — | — |

### 10.4 分析校准

追加到 `docs/analysis/analysis-calibration.md` 的一行（随本 PR 写入）：

| 日期 | 项目 | 分析当时判断/优选方案错在哪 | 误判最重的 Gate | 当时缺的证据 | 结构缺失 | 下次改的流程 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-28 | 面向生产的重构方案（`2026-09-28-production-refactor-deep-analysis.md`；Phase 7 由 Opus 5.5 子代理执行，审查稿 `…-adversarial-review.md`） | 冻结稿 Weak GO → 审查 PIVOT，P0 ×2：① 把 CLAUDE.md 的规范句「凭据只有一个位置 env.sh」当成现状写进部署步骤，而本机没有 `env.sh`、凭据在 `~/.zshrc`——**memory 里明写着「新建 env.sh 会停掉实盘循环」，读了仍写错**；② 把「31.9% 提交触碰 ratchet」读成「税」，而 267/275 次改的是 CEILING 值，触碰就是增长事件，于是推荐了一个只能靠放宽控制才能压低的指标。P1 里最硬的：把 09-06 操作者具名 ACCEPTED 的 D-P4 写成「仓库没写过」；把一次安全窗口内的重启迟到 2,154 s 读成代价，而 09-15 起丢的 7 根 bar 全来自失败周期、0 来自重启；「规则将带进真实资金，CLAUDE.md」出处不存在；替操作者定「生产」的层级且漏掉 memory 的「out of scope」；把操作者的「尤其是 alpha」改写成便于驳倒的命题再驳倒 | **G2**（数字全对、读法错四处、引用错五处、过期一处——第七次「缺的不是数据是读法」）；G1（F-D 把症状写成根因；Success Definition 定义在方案要改的文件上） | 不缺数据：`test -f env.sh`、`git show` 逐提交看 CEILING 行、`cycles.jsonl` 的 SKIPPED/ERROR 行、`grep D-P4 docs/analysis`、`grep 真实资金 CLAUDE.md`——五条命令各不到一秒 | Phase 1 未暂停（自治运行，指令链以最终方案为终点；与 09-26 行同形，**第二次**）；代码检索约 60 次（用户材料，但远超 L 级每段 12 次的精神，第二次）；G2/G4 列了未通过项仍写 PASS（第二次）；其余产物齐（§11） | 三条：①部署步骤里引用的每个文件路径与环境事实，写之前 `test -f` / `ls`，不引 CLAUDE.md 的规范句当现状；②任何「X% 的提交触碰 Y」类读数要先拆构成（`git show` 逐提交看改了哪些行）再命名它；③写「仓库没写过 / 从未」之前 `grep docs/analysis docs/RESEARCH_LOG`——这条 09-17 已写过一次，本次仍犯，说明它该进 skill 的 Phase 2 最低动作而不是校准表 |

另建议操作者用 Q1–Q6 的答案生成 `deep-analysis-constitution.md`（战略方向、硬约束、敏感边界、决策人、默认证据位置）——本文 P1.6 的六问里至少 Q1、Q3、Q5 的前提下一份分析不必再问。

---

## 11. 产物自检表

每行只能是 `PASS` / `COMPRESSED（理由）` / `N/A（证据）`。审查者第一遍核的是冻结稿版本（§7.1 引用的审查稿 §0）；本表是修订版的自检。

| 产物 | 定义处 | 状态 |
| --- | --- | --- |
| Phase 级头部（Reading Check + Interaction 行） | SKILL 5 | PASS（文首，含 [R 修订]） |
| 需求四问 / 来源与偏差 / 需求预设清单（六列）/ Early Kill | SKILL 5.4–5.5, B.2 | PASS（§P1.1–P1.4，含 [R 修订]） |
| Context Intake（或「材料已覆盖」） | SKILL 3.3 | PASS（§P1.6 修订版六问；首轮未暂停记 KILL-13 ACCEPTED） |
| Evidence Ledger 全字段，每条可复核位置 | A.4 | PASS（E-PR01–42；E-PR34 位置改为 §5.2 命令；Owner 统一声明） |
| Claim Register 全字段；P0/P1 的 Axiom Trace 是回答不是标签 | SKILL 4, A.5 | PASS（§2.2，含状态修订） |
| G1–G7 每门有 ≥1 条追溯命题，或 N/A 带理由 | SKILL 4, 6 | PASS（§1 修订表） |
| Assumption Register 全字段；高×高有实验并对齐 Gap Plan | A.5 | PASS（§2.3 + [R 修订] 汇总行） |
| Gap Plan | A.6 | PASS（§2.5：GAP-PR01–10，四条已关） |
| Problem Statement / 5W2H / Causal Chain | B.4 | PASS（§3.1–3.3，含 [R 修订]） |
| Problem Reframing ≥2 + `D-xxx` | B.4 | PASS（§3.4，D-PR00 [R 修订]） |
| Success Definition 在 Option 之前，`M-xxx` 三类 | B.4 | PASS（§3.5 修订表 M-PR01–07；冻结稿版本保留对照） |
| JTBD / Stakeholder（含受损方）/ Scenario / Journey / Edge Cases | B.4 | PASS（§3.6–3.10；WP-C3 补偿标撤回） |
| Strategic Fit / Relative Value（含四必答）/ Business Case / Cost of Delay | B.5 | PASS（§4.1–4.5，含 [R 修订]） |
| As-Is/To-Be / Constraint Map / Impact Radius / Engineering Pre-check | B.6 | PASS（§5.2–5.5） |
| Risk Register（Memo 引用的 RISK-xxx 在此定义） | A.5 | PASS（RISK-PR01–10；Memo 引用的 RISK-PR02、RISK-PR10 在此） |
| Option Set / Failure Modes / Recommendation 全字段 / Decision Log | B.7 | PASS（§6.1 + 处置表、§6.2、§6.3 修订版、§6.4 含 D-PR06/07） |
| 每个 Phase 的 Prerequisites / Produces / Gate / Downstream | B.1 | PASS |
| Phase 7 全部产物；L 级由独立子代理执行 | D | PASS（§7；Opus 5.5 子代理；审查稿全文存档） |
| MoSCoW / Scope Firewall / Decision Compression | B.8 | PASS（§8） |
| 四类 Contract（要建的东西：全写；分析型：Test/Acceptance） | C.2–C.5 | PASS（§9.2–9.5） |
| Source Trace Matrix | C.7 | PASS（§9.6） |
| Validation Plan / Experiment Design / 分析校准行（含结构缺失列） | C.9 | PASS（§10.1–10.4） |
| Checkpoint 文件 `<项目>-checkpoint.md` | SKILL 11 | PASS（`2026-09-28-production-refactor-checkpoint.md`，定稿时更新） |
| Final Decision 7 词之一 + 命中的 H-ID | SKILL 9 | PASS（§13：PIVOT，H2 已处理，H3、H5） |

## 12. Checkpoint

`docs/analysis/2026-09-28-production-refactor-checkpoint.md`（定稿版）：等级 L / Yellow；Phase 1–10 COMPLETE；已冻结 D-PR00–07；G0 PASS · G1–G5 PARTIAL · G6 已处置（未复审）· G7 PASS（限卫生批）；开放：A-PR01（Q1）、C-PR01 吞吐一半（GAP-PR06）、GAP-PR08/09/10；待决 Q1、Q2a、Q2b、Q3、Q5、Q6；取证：外部 0 次、代码检索约 60 次 + 审查者 125 次工具调用；下一动作：卫生批六个 PR、操作者答六问并换代理节点、下一会话跑三项测量。

## 13. Final Decision

**PIVOT**。命中 **H2**（审查时存在未处理的强反证：09-06 D-P4 的具名 ACCEPTED、`cycles.jsonl` 的重启与 ERROR 行、CEILING 的逐提交构成——本版已处理，决定词按处理前的事实记）、**H3**（A-PR01「生产的定义」UNKNOWN；C-PR01 吞吐一半 UNKNOWN）、**H5**（六项卫生项以外相对价值 Unproven）；H1 处置后 OPEN P0 = 0；H7 因审查者未复审本修订版保守计入；H8 未命中（凭据边界已知：在 `~/.zshrc`，不在 `env.sh`）。一句话：问题成立——「生产」没有定义与读数、非 alpha 的增长率没有裁定、传输层每两天丢一根 bar 的退出检查没人量、本地专属测试红了三天 CI 看不见；方案改形——六项不改任何控制的卫生项按 Weak GO 的字面含义受控开工，六个改控制的问题带价交操作者且未答不执行，一项操作者动作（换代理节点），三项零 ledger 测量；**alpha 模块不重排，但这一条不再是作者的决定，是交回操作者的 Q6**。
